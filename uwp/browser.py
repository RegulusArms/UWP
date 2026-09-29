"""Thumbnail file browser for adding media to the library.

GTK3's own file chooser only has a list view, so this is a small browser
with real image/video thumbnails and an adjustable icon size.
"""
import os

from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango

from . import media

MIN_SIZE, MAX_SIZE, DEFAULT_SIZE = 64, 320, 160
BATCH = 150          # tiles added per main-loop iteration (keeps UI live)


class Tile(Gtk.FlowBoxChild):
    def __init__(self, path, is_dir):
        super().__init__()
        self.path = path
        self.is_dir = is_dir
        self.kind = None if is_dir else media.kind(path)
        self.name = os.path.basename(path) or path
        self.thumb = None                 # pixbuf scaled to <= MAX_SIZE
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_border_width(4)
        overlay = Gtk.Overlay()
        self.image = Gtk.Image()
        overlay.add(self.image)
        if self.kind == 'video':
            badge = Gtk.Label(label='▶ VIDEO')
            badge.get_style_context().add_class('uwp-badge')
            badge.set_halign(Gtk.Align.START)
            badge.set_valign(Gtk.Align.START)
            badge.set_margin_start(4)
            badge.set_margin_top(4)
            overlay.add_overlay(badge)
        box.add(overlay)
        self.label = Gtk.Label(label=self.name, lines=2, wrap=True,
                               wrap_mode=Pango.WrapMode.WORD_CHAR,
                               ellipsize=Pango.EllipsizeMode.MIDDLE,
                               justify=Gtk.Justification.CENTER)
        box.add(self.label)
        self.add(box)
        self.set_tooltip_text(path)

    def set_thumb(self, pixbuf, size):
        pw, ph = pixbuf.get_width(), pixbuf.get_height()
        k = min(1.0, MAX_SIZE / max(pw, ph))
        self.thumb = pixbuf if k >= 1 else pixbuf.scale_simple(
            max(1, int(pw * k)), max(1, int(ph * k)),
            GdkPixbuf.InterpType.BILINEAR)
        self.apply_size(size)

    def apply_size(self, size):
        h = int(size * 0.66)
        self.image.set_size_request(size, h)
        self.label.set_max_width_chars(max(8, size // 8))
        if self.thumb is not None:
            pw, ph = self.thumb.get_width(), self.thumb.get_height()
            k = min(size / pw, h / ph)
            self.image.set_from_pixbuf(self.thumb.scale_simple(
                max(1, int(pw * k)), max(1, int(ph * k)),
                GdkPixbuf.InterpType.BILINEAR))
        else:
            icon = ('folder' if self.is_dir else
                    'video-x-generic' if self.kind == 'video'
                    else 'image-x-generic')
            self.image.set_from_icon_name(icon, Gtk.IconSize.DIALOG)
            self.image.set_pixel_size(int(h * 0.8))


class MediaBrowser(Gtk.Dialog):
    """Returns a list of chosen files/folders via run_browser()."""

    def __init__(self, parent, start_dir, size, library):
        super().__init__(title='Add to library', transient_for=parent,
                         modal=True)
        self.set_default_size(1100, 760)
        self.size = size
        self.library = set(library)
        self.cwd = None
        self.history = []
        self.result = []
        self.thumbs = media.Thumbnailer(cache=False)
        self._fill_id = 0
        self._resize_id = 0
        self.tiles = {}

        self.add_button('Cancel', Gtk.ResponseType.CANCEL)
        self.folder_btn = self.add_button('Add this folder', 1)
        self.folder_btn.set_tooltip_text(
            'Add the folder you are in; new files in it show up automatically')
        self.add_btn = self.add_button('Add selected', Gtk.ResponseType.OK)
        self.add_btn.get_style_context().add_class('suggested-action')
        self.set_default_response(Gtk.ResponseType.OK)

        area = self.get_content_area()
        paned = Gtk.Paned()
        area.pack_start(paned, True, True, 0)

        side = Gtk.PlacesSidebar()
        side.set_show_recent(False)
        side.set_show_trash(False)
        side.set_show_other_locations(False)
        side.connect('open-location',
                     lambda _s, loc, _f: loc.get_path() and self.navigate(
                         loc.get_path()))
        side.set_size_request(180, -1)
        self.sidebar = side
        paned.pack1(side, False, False)

        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        main.set_border_width(8)
        paned.pack2(main, True, False)

        bar = Gtk.Box(spacing=6)
        self.back_btn = Gtk.Button.new_from_icon_name('go-previous-symbolic',
                                                      Gtk.IconSize.BUTTON)
        self.back_btn.set_tooltip_text('Back')
        self.back_btn.connect('clicked', lambda _b: self.go_back())
        up = Gtk.Button.new_from_icon_name('go-up-symbolic',
                                           Gtk.IconSize.BUTTON)
        up.set_tooltip_text('Parent folder (Alt+Up)')
        up.connect('clicked', lambda _b: self.go_up())
        self.path_entry = Gtk.Entry(hexpand=True)
        self.path_entry.connect('activate', self._on_path_entry)
        bar.add(self.back_btn)
        bar.add(up)
        bar.pack_start(self.path_entry, True, True, 0)
        main.add(bar)

        bar2 = Gtk.Box(spacing=6)
        self.kind_combo = Gtk.ComboBoxText()
        for k, label in (('all', 'Images & videos'), ('image', 'Images'),
                         ('video', 'Videos')):
            self.kind_combo.append(k, label)
        self.kind_combo.set_active_id('all')
        self.kind_combo.connect('changed',
                                lambda _c: self.flow.invalidate_filter())
        self.search = Gtk.SearchEntry(placeholder_text='Filter by name')
        self.search.connect('search-changed',
                            lambda _e: self.flow.invalidate_filter())
        bar2.add(self.kind_combo)
        bar2.add(self.search)
        self.count_label = Gtk.Label()
        self.count_label.get_style_context().add_class('dim-label')
        bar2.add(self.count_label)
        self.size_adj = Gtk.Adjustment(value=size, lower=MIN_SIZE,
                                       upper=MAX_SIZE, step_increment=16,
                                       page_increment=32)
        self.size_adj.connect('value-changed', self._on_size)
        scale = Gtk.Scale(adjustment=self.size_adj, draw_value=False)
        scale.set_size_request(180, -1)
        scale.set_tooltip_text('Thumbnail size (Ctrl+scroll, Ctrl +/-)')
        small = Gtk.Image.new_from_icon_name('zoom-out-symbolic',
                                             Gtk.IconSize.BUTTON)
        big = Gtk.Image.new_from_icon_name('zoom-in-symbolic',
                                           Gtk.IconSize.BUTTON)
        bar2.pack_end(big, False, False, 0)
        bar2.pack_end(scale, False, False, 0)
        bar2.pack_end(small, False, False, 0)
        main.add(bar2)

        sw = Gtk.ScrolledWindow(vexpand=True)
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.connect('scroll-event', self._on_scroll)
        self.flow = Gtk.FlowBox(valign=Gtk.Align.START, homogeneous=True,
                                max_children_per_line=60,
                                selection_mode=Gtk.SelectionMode.MULTIPLE,
                                activate_on_single_click=False)
        self.flow.set_filter_func(self._filter)
        self.flow.connect('child-activated', self._on_activated)
        self.flow.connect('selected-children-changed',
                          lambda _f: self._update_buttons())
        sw.add(self.flow)
        main.pack_start(sw, True, True, 0)

        self.connect('key-press-event', self._on_key)
        self.connect('destroy', lambda _w: self._cleanup())
        self.show_all()
        self.navigate(start_dir if os.path.isdir(start_dir or '')
                      else GLib.get_user_special_dir(
                          GLib.UserDirectory.DIRECTORY_PICTURES)
                      or os.path.expanduser('~'), remember=False)

    # ---- navigation ----------------------------------------------------
    def navigate(self, path, remember=True):
        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.isdir(path):
            self.path_entry.get_style_context().add_class('error')
            return
        self.path_entry.get_style_context().remove_class('error')
        if remember and self.cwd and self.cwd != path:
            self.history.append(self.cwd)
        self.cwd = path
        self.path_entry.set_text(path)
        self.sidebar.set_location(Gio.File.new_for_path(path))
        self.back_btn.set_sensitive(bool(self.history))
        self._load(path)

    def go_back(self):
        if self.history:
            self.navigate(self.history.pop(), remember=False)

    def go_up(self):
        parent = os.path.dirname(self.cwd)
        if parent and parent != self.cwd:
            self.navigate(parent)

    def _on_path_entry(self, entry):
        self.navigate(entry.get_text())

    def _load(self, path):
        if self._fill_id:
            GLib.source_remove(self._fill_id)
            self._fill_id = 0
        self.thumbs.cancel_pending()
        for child in self.flow.get_children():
            self.flow.remove(child)
        self.tiles = {}
        dirs, files = [], []
        try:
            with os.scandir(path) as it:
                for e in it:
                    if e.name.startswith('.'):
                        continue
                    try:
                        if e.is_dir():
                            dirs.append(e.path)
                        elif e.is_file() and media.kind(e.path):
                            files.append(e.path)
                    except OSError:
                        pass
        except OSError as e:
            self.count_label.set_text(f'Cannot open: {e.strerror}')
            return
        key = lambda p: os.path.basename(p).lower()
        pending = ([(p, True) for p in sorted(dirs, key=key)] +
                   [(p, False) for p in sorted(files, key=key)])
        self.count_label.set_text(
            f'{len(dirs)} folders, {len(files)} images/videos')
        self._fill_id = GLib.idle_add(self._fill, pending)
        self._update_buttons()

    def _fill(self, pending):
        for path, is_dir in pending[:BATCH]:
            tile = Tile(path, is_dir)
            tile.apply_size(self.size)
            tile.show_all()
            self.flow.add(tile)
            self.tiles[path] = tile
            if not is_dir:
                self.thumbs.request(path, self._on_thumb)
        del pending[:BATCH]
        if pending:
            return True
        self._fill_id = 0
        return False

    def _on_thumb(self, path, result):
        tile = self.tiles.get(path)
        if tile and result:
            tile.set_thumb(result[0], self.size)

    # ---- sizing --------------------------------------------------------
    def _on_size(self, adj):
        self.size = int(adj.get_value())
        if not self._resize_id:
            self._resize_id = GLib.timeout_add(80, self._apply_size)

    def _apply_size(self):
        self._resize_id = 0
        for tile in self.tiles.values():
            tile.apply_size(self.size)
        return False

    def _on_scroll(self, _w, ev):
        if not ev.state & Gdk.ModifierType.CONTROL_MASK:
            return False
        ok, _dx, dy = ev.get_scroll_deltas()
        if not ok:
            dy = {Gdk.ScrollDirection.UP: -1,
                  Gdk.ScrollDirection.DOWN: 1}.get(ev.direction, 0)
        self.size_adj.set_value(self.size_adj.get_value() - 16 * dy)
        return True

    # ---- filtering / selection ----------------------------------------
    def _filter(self, tile):
        k = self.kind_combo.get_active_id()
        if not tile.is_dir and k != 'all' and tile.kind != k:
            return False
        q = self.search.get_text().strip().lower()
        return not q or q in tile.name.lower()

    def _update_buttons(self):
        n = len(self.flow.get_selected_children())
        self.add_btn.set_sensitive(n > 0)
        self.add_btn.set_label(f'Add selected ({n})' if n else 'Add selected')
        self.folder_btn.set_sensitive(bool(self.cwd) and
                                      self.cwd not in self.library)

    def _on_activated(self, _flow, tile):
        if tile.is_dir:
            self.navigate(tile.path)
        else:
            self.result = [tile.path]
            self.response(Gtk.ResponseType.OK)

    def _on_key(self, _w, ev):
        ctrl = ev.state & Gdk.ModifierType.CONTROL_MASK
        alt = ev.state & Gdk.ModifierType.MOD1_MASK
        if ctrl and ev.keyval in (Gdk.KEY_plus, Gdk.KEY_equal,
                                  Gdk.KEY_KP_Add):
            self.size_adj.set_value(self.size_adj.get_value() + 32)
        elif ctrl and ev.keyval in (Gdk.KEY_minus, Gdk.KEY_KP_Subtract):
            self.size_adj.set_value(self.size_adj.get_value() - 32)
        elif ctrl and ev.keyval == Gdk.KEY_l:
            self.path_entry.grab_focus()
        elif alt and ev.keyval == Gdk.KEY_Up:
            self.go_up()
        elif alt and ev.keyval == Gdk.KEY_Left:
            self.go_back()
        else:
            return False
        return True

    def selection(self):
        return [t.path for t in self.flow.get_selected_children()]

    def _cleanup(self):
        if self._fill_id:
            GLib.source_remove(self._fill_id)
            self._fill_id = 0
        self.thumbs.shutdown()


def run_browser(parent, start_dir, size, library):
    """Show the browser. Returns (paths, last_dir, size)."""
    d = MediaBrowser(parent, start_dir, size, library)
    resp = d.run()
    if resp == Gtk.ResponseType.OK:
        paths = d.result or d.selection()
    elif resp == 1:
        paths = [d.cwd]
    else:
        paths = []
    last_dir, size = d.cwd, d.size
    d.destroy()
    return paths, last_dir, size
