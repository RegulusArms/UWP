"""Settings window. Edits a copy of the config; the desktop only changes on
OK, or temporarily while "Live preview" is on (the library itself is saved
immediately)."""
import os
import subprocess

from gi.repository import Gdk, GLib, GObject, Gtk, Pango

from . import config, keybind, media, renderer, transform

ACCENT = (0.21, 0.52, 0.89)
TILE_W, TILE_H = 176, 110


def accel_label(accel):
    if not accel:
        return 'Not set'
    key, mods = Gtk.accelerator_parse(accel)
    return Gtk.accelerator_get_label(key, mods) if key else accel


class MonitorLayout(Gtk.DrawingArea):
    """Scaled picture of the monitor arrangement with live previews.
    Click selects (Ctrl+click adds), drag pans, scroll zooms,
    Shift+scroll rotates."""

    def __init__(self, gui):
        super().__init__()
        self.gui = gui
        self.rects = {}          # key -> (x, y, w, h) in widget px
        self.drag = None
        self.set_size_request(420, 260)
        self.set_hexpand(True)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK |
                        Gdk.EventMask.BUTTON_RELEASE_MASK |
                        Gdk.EventMask.POINTER_MOTION_MASK |
                        Gdk.EventMask.SCROLL_MASK |
                        Gdk.EventMask.SMOOTH_SCROLL_MASK)
        self.connect('draw', self._draw)
        self.connect('button-press-event', self._press)
        self.connect('button-release-event', self._release)
        self.connect('motion-notify-event', self._motion)
        self.connect('scroll-event', self._scroll)

    def _layout(self, aw, ah):
        mons = self.gui.monitors
        if not mons:
            return
        x0 = min(m['x'] for m in mons)
        y0 = min(m['y'] for m in mons)
        bw = max(m['x'] + m['w'] for m in mons) - x0
        bh = max(m['y'] + m['h'] for m in mons) - y0
        pad = 16
        k = min((aw - 2 * pad) / bw, (ah - 2 * pad) / bh)
        ox = (aw - bw * k) / 2
        oy = (ah - bh * k) / 2
        self.rects = {m['key']: (ox + (m['x'] - x0) * k + 2,
                                 oy + (m['y'] - y0) * k + 2,
                                 m['w'] * k - 4, m['h'] * k - 4)
                      for m in mons}

    def _draw(self, _w, cr):
        aw, ah = self.get_allocated_width(), self.get_allocated_height()
        self._layout(aw, ah)
        profile = self.gui.profile()
        for m in self.gui.monitors:
            key = m['key']
            x, y, w, h = self.rects[key]
            cr.save()
            cr.rectangle(x, y, w, h)
            cr.clip()
            cr.set_source_rgb(0.08, 0.08, 0.09)
            cr.paint()
            wp = self.gui.shown_wp(key)
            if wp:
                t = self.gui.thumb(wp['path'])
                if t:
                    cr.translate(x, y)
                    transform.cairo_paint(cr, t[0], wp, w, h, t[1], t[2],
                                          px_scale=w / m['w'])
            cr.restore()
            self._label(cr, x, y, w, h, key, profile['monitors'].get(key),
                        wp)
            sel = key in self.gui.selected
            cr.set_line_width(4 if sel else 1)
            if sel:
                cr.set_source_rgb(*ACCENT)
            else:
                cr.set_source_rgba(0.6, 0.6, 0.6, 0.8)
            cr.rectangle(x, y, w, h)
            cr.stroke()
        return True

    @staticmethod
    def _label(cr, x, y, w, h, key, entry, wp):
        text = key
        if transform.is_slideshow(entry):
            n = len(entry.get('items', []))
            text += f'  (slideshow, {n} item{"" if n == 1 else "s"})'
        elif wp is None:
            text += '  (no wallpaper)'
        elif media.kind(wp['path']) == 'video':
            speed, start, end = transform.playback(wp)
            extra = ['video']
            if speed != 1:
                extra.append(f'{speed:g}\u00d7')
            if start or end:
                extra.append('loop ' + fmt_time(start) + '\u2013' +
                             (fmt_time(end) if end else 'end'))
            text += '  (' + ', '.join(extra) + ')'
        cr.select_font_face('Sans', 0, 1)
        cr.set_font_size(12)
        ext = cr.text_extents(text)
        bx, by = x + 6, y + h - 26
        cr.set_source_rgba(0, 0, 0, 0.6)
        cr.rectangle(bx, by, ext.x_advance + 12, 20)
        cr.fill()
        cr.set_source_rgb(1, 1, 1)
        cr.move_to(bx + 6, by + 14)
        cr.show_text(text)

    def _hit(self, ex, ey):
        for key, (x, y, w, h) in self.rects.items():
            if x <= ex <= x + w and y <= ey <= y + h:
                return key
        return None

    def _press(self, _w, ev):
        key = self._hit(ev.x, ev.y)
        if key is None or ev.button != 1:
            return False
        if ev.state & Gdk.ModifierType.CONTROL_MASK:
            sel = set(self.gui.selected) ^ {key}
            self.gui.set_selected(sel or {key})
        else:
            if key not in self.gui.selected:
                self.gui.set_selected({key})
            if self.gui.edit_wp(key):
                self.drag = (key, ev.x, ev.y,
                             {k: dict(self.gui.edit_wp(k))
                              for k in self.gui.selected
                              if self.gui.edit_wp(k)})
        return True

    def _release(self, _w, _ev):
        self.drag = None
        return False

    def _motion(self, _w, ev):
        key = self._hit(ev.x, ev.y)
        win = self.get_window()
        if win:
            cursor = None
            if self.drag or (key and self.gui.edit_wp(key)):
                cursor = Gdk.Cursor.new_from_name(
                    win.get_display(), 'grabbing' if self.drag else 'grab')
            win.set_cursor(cursor)
        if not self.drag:
            return False
        dkey, sx, sy, start = self.drag
        _, _, w, h = self.rects[dkey]
        dx, dy = (ev.x - sx) / w, (ev.y - sy) / h
        for k, orig in start.items():
            wp = self.gui.edit_wp(k)
            if wp:
                wp['offset_x'] = round(orig['offset_x'] + dx, 4)
                wp['offset_y'] = round(orig['offset_y'] + dy, 4)
        self.gui.sync_controls()
        self.gui.changed()
        return True

    def _scroll(self, _w, ev):
        key = self._hit(ev.x, ev.y)
        if key is None or not self.gui.edit_wp(key):
            return False
        if key not in self.gui.selected:
            self.gui.set_selected({key})
        ok, _dx, dy = ev.get_scroll_deltas()
        if not ok:
            dy = {Gdk.ScrollDirection.UP: -1,
                  Gdk.ScrollDirection.DOWN: 1}.get(ev.direction, 0)
        if not dy:
            return True
        for k in self.gui.selected:
            wp = self.gui.edit_wp(k)
            if not wp:
                continue
            if ev.state & Gdk.ModifierType.SHIFT_MASK:
                wp['rotation'] = _wrap_deg(wp['rotation'] - 5 * dy)
            else:
                wp['zoom'] = min(10.0, max(0.05, wp['zoom'] * 1.1 ** -dy))
        self.gui.sync_controls()
        self.gui.changed()
        return True


def fmt_time(t):
    m, s = divmod(max(0.0, t), 60)
    return f'{int(m)}:{s:04.1f}'


class RangeBar(Gtk.DrawingArea):
    """Timeline with two draggable handles selecting [start, end] seconds."""
    __gsignals__ = {'changed': (GObject.SignalFlags.RUN_FIRST, None, ())}
    PAD, R = 10, 7

    def __init__(self):
        super().__init__()
        self.duration, self.start, self.end = 0.0, 0.0, 0.0
        self._drag = None
        self.set_size_request(-1, 30)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK |
                        Gdk.EventMask.BUTTON_RELEASE_MASK |
                        Gdk.EventMask.POINTER_MOTION_MASK)
        self.connect('draw', self._draw)
        self.connect('button-press-event', self._press)
        self.connect('button-release-event', lambda *_: setattr(
            self, '_drag', None))
        self.connect('motion-notify-event', self._motion)

    def set_range(self, duration, start, end):
        self.duration = max(0.0, duration)
        self.start = start
        self.end = end if end else self.duration
        self.queue_draw()

    def _x(self, t):
        w = self.get_allocated_width() - 2 * self.PAD
        return self.PAD + (t / self.duration * w if self.duration else 0)

    def _t(self, x):
        w = self.get_allocated_width() - 2 * self.PAD
        if not self.duration or w <= 0:
            return 0.0
        return min(self.duration, max(0.0, (x - self.PAD) / w *
                                      self.duration))

    def _draw(self, _w, cr):
        h = self.get_allocated_height()
        cy = h / 2
        x0, x1 = self._x(0), self._x(self.duration)
        cr.set_source_rgba(0.5, 0.5, 0.5, 0.35)
        cr.rectangle(x0, cy - 3, x1 - x0, 6)
        cr.fill()
        if not self.duration:
            return True
        a, b = self._x(self.start), self._x(self.end)
        cr.set_source_rgb(*ACCENT)
        cr.rectangle(a, cy - 3, b - a, 6)
        cr.fill()
        for x in (a, b):
            cr.arc(x, cy, self.R, 0, 6.2832)
            cr.set_source_rgb(1, 1, 1)
            cr.fill_preserve()
            cr.set_source_rgb(*ACCENT)
            cr.set_line_width(2)
            cr.stroke()
        return True

    def _press(self, _w, ev):
        if ev.button != 1 or not self.duration:
            return False
        a, b = self._x(self.start), self._x(self.end)
        self._drag = 'start' if abs(ev.x - a) <= abs(ev.x - b) else 'end'
        self._motion(_w, ev)
        return True

    def _motion(self, _w, ev):
        if not self._drag:
            return False
        t = self._t(ev.x)
        gap = min(0.1, self.duration / 2)
        if self._drag == 'start':
            self.start = min(t, self.end - gap)
        else:
            self.end = max(t, self.start + gap)
        self.queue_draw()
        self.emit('changed')
        return True


def _wrap_deg(a):
    a = (a + 180) % 360 - 180
    return round(a, 2)


class KeyCaptureDialog(Gtk.Dialog):
    CLEAR = 1

    def __init__(self, parent, what):
        super().__init__(title='Set shortcut', transient_for=parent,
                         modal=True)
        self.result = None
        self.set_default_size(380, 140)
        box = self.get_content_area()
        box.set_spacing(8)
        box.set_border_width(16)
        box.add(Gtk.Label(label=f'Press the new shortcut for\n<b>{GLib.markup_escape_text(what)}</b>',
                          use_markup=True, justify=Gtk.Justification.CENTER))
        self.hint = Gtk.Label(label='Esc to cancel')
        self.hint.get_style_context().add_class('dim-label')
        box.add(self.hint)
        self.add_button('Cancel', Gtk.ResponseType.CANCEL)
        self.add_button('Clear shortcut', self.CLEAR)
        self.connect('key-press-event', self._key)
        self.show_all()

    def _key(self, _w, ev):
        keyval = Gdk.keyval_to_lower(ev.keyval)
        mods = ev.state & Gtk.accelerator_get_default_mod_mask()
        if ev.is_modifier:
            return True
        if keyval == Gdk.KEY_Escape and not mods:
            self.response(Gtk.ResponseType.CANCEL)
            return True
        is_fkey = Gdk.KEY_F1 <= keyval <= Gdk.KEY_F35
        if not mods and not is_fkey:
            self.hint.set_text('Use a modifier: Ctrl, Alt, Shift or Super')
            return True
        if not Gtk.accelerator_valid(keyval, mods):
            self.hint.set_text('That key cannot be used as a shortcut')
            return True
        if is_reserved(keyval, mods):
            self.hint.set_text('Reserved by the system (console switch / '
                               'session kill). Pick another.')
            return True
        self.result = Gtk.accelerator_name(keyval, mods)
        self.response(Gtk.ResponseType.OK)
        return True


def is_reserved(keyval, mods):
    """Combos the X server/kernel act on before GNOME sees them:
    Ctrl+Alt+F1..F12 switches virtual terminals (leaves the desktop) and
    Ctrl+Alt+Backspace can kill the X session."""
    ctrl_alt = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK
    if (mods & ctrl_alt) != ctrl_alt:
        return False
    return (Gdk.KEY_F1 <= keyval <= Gdk.KEY_F12
            or keyval in (Gdk.KEY_BackSpace, Gdk.KEY_Delete))


def capture_key(parent, what):
    """Returns accel string, '' to clear, or None if cancelled."""
    d = KeyCaptureDialog(parent, what)
    resp = d.run()
    result = d.result
    d.destroy()
    if resp == Gtk.ResponseType.OK:
        return result
    if resp == KeyCaptureDialog.CLEAR:
        return ''
    return None


class SettingsDialog(Gtk.Dialog):
    def __init__(self, gui):
        super().__init__(title='Shortcuts & settings', transient_for=gui,
                         modal=True)
        self.gui = gui
        self.set_default_size(460, -1)
        self.add_button('Close', Gtk.ResponseType.CLOSE)
        box = self.get_content_area()
        box.set_border_width(16)
        box.set_spacing(10)
        edit = gui.edit

        if not keybind.supported():
            warn = Gtk.Label(xalign=0, wrap=True, label=(
                'GNOME custom shortcuts are not available on this desktop. '
                'Bind your own shortcut to: uwp --next-profile'))
            box.add(warn)

        grid = Gtk.Grid(column_spacing=12, row_spacing=6)
        box.add(grid)
        row = 0

        def heading(text):
            nonlocal row
            lbl = Gtk.Label(xalign=0, use_markup=True,
                            label=f'<b>{GLib.markup_escape_text(text)}</b>')
            lbl.set_margin_top(6 if row else 0)
            grid.attach(lbl, 0, row, 2, 1)
            row += 1

        def shortcut_row(label, getter, setter):
            nonlocal row
            grid.attach(Gtk.Label(label=label, xalign=0, hexpand=True),
                        0, row, 1, 1)
            btn = Gtk.Button(label=accel_label(getter()))
            btn.set_size_request(160, -1)

            def clicked(_b):
                accel = capture_key(self, label)
                if accel is not None:
                    setter(accel)
                    btn.set_label(accel_label(accel))
            btn.connect('clicked', clicked)
            grid.attach(btn, 1, row, 1, 1)
            row += 1

        heading('Cycle profiles (works while UWP runs in the tray)')
        shortcut_row('Next profile', lambda: edit.get('keybind_next', ''),
                     lambda a: edit.__setitem__('keybind_next', a))
        shortcut_row('Previous profile', lambda: edit.get('keybind_prev', ''),
                     lambda a: edit.__setitem__('keybind_prev', a))
        heading('Jump straight to a profile')
        for p in edit['profiles']:
            shortcut_row(p['name'], lambda p=p: p.get('keybind', ''),
                         lambda a, p=p: p.__setitem__('keybind', a))

        heading('Behavior')
        pause = Gtk.CheckButton(
            label='Pause videos hidden behind maximized or fullscreen windows')
        pause.set_active(edit.get('pause_when_covered', True))
        pause.connect('toggled', lambda b: edit.__setitem__(
            'pause_when_covered', b.get_active()))
        grid.attach(pause, 0, row, 2, 1)
        row += 1
        auto = Gtk.CheckButton(label='Start UWP when I log in')
        auto.set_active(edit.get('autostart', gui.app.autostart_enabled()))
        auto.connect('toggled', lambda b: edit.__setitem__(
            'autostart', b.get_active()))
        grid.attach(auto, 0, row, 2, 1)
        row += 1
        note = Gtk.Label(xalign=0, wrap=True, label=(
            'Changes take effect when you press OK in the main window.'))
        note.get_style_context().add_class('dim-label')
        box.add(note)
        self.show_all()


class LibraryItem(Gtk.FlowBoxChild):
    def __init__(self, path, source):
        super().__init__()
        self.path, self.source = path, source
        self.kind = media.kind(path)
        self.name = os.path.basename(path)
        ev = Gtk.EventBox()
        self.add(ev)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_border_width(4)
        ev.add(box)
        overlay = Gtk.Overlay()
        self.image = Gtk.Image.new_from_icon_name(
            'video-x-generic' if self.kind == 'video' else 'image-x-generic',
            Gtk.IconSize.DIALOG)
        self.image.set_size_request(TILE_W, TILE_H)
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
        lbl = Gtk.Label(label=self.name, max_width_chars=20,
                        ellipsize=Pango.EllipsizeMode.MIDDLE)
        box.add(lbl)
        self.set_tooltip_text(path)
        self.eventbox = ev

    def set_thumb(self, pixbuf):
        pw, ph = pixbuf.get_width(), pixbuf.get_height()
        k = min(TILE_W / pw, TILE_H / ph)
        self.image.set_from_pixbuf(pixbuf.scale_simple(
            max(1, int(pw * k)), max(1, int(ph * k)),
            2))  # GdkPixbuf.InterpType.BILINEAR


CSS = b"""
.uwp-badge { background: rgba(0,0,0,0.65); color: white; font-size: 9px;
             font-weight: bold; padding: 1px 5px; border-radius: 4px; }
.uwp-panel { padding: 4px; }
"""


class WallpaperGui(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title='UWP Wallpapers')
        self.app = app
        self.edit = config.clone(app.cfg)
        self.thumbs = media.Thumbnailer()
        self.monitors = renderer.monitors()
        primary = next((m['key'] for m in self.monitors if m['primary']),
                       self.monitors[0]['key'] if self.monitors else None)
        self.selected = {primary} if primary else set()
        self._syncing = False
        self._profile_combo_busy = False
        self._live_pending = 0
        self._committed = False
        self.ss_item = None          # selected item in the slideshow list
        self._ss_busy = False

        prov = Gtk.CssProvider()
        prov.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), prov,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.set_default_size(1180, 860)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.connect('destroy', self._on_destroy)
        self.connect('key-press-event', self._on_key)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        root.set_border_width(12)
        self.add(root)

        root.add(self._build_profile_bar())

        # Editor on top, library below, split by a draggable divider.
        self.split = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        self.split.set_wide_handle(True)
        root.pack_start(self.split, True, True, 0)
        upper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        upper.set_margin_bottom(6)
        lower = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        lower.set_margin_top(6)
        # The editor sits in a scroller with no scrollbar so the divider can
        # be dragged all the way up: the editor is clipped, not squashed.
        self.upper_sw = Gtk.ScrolledWindow()
        self.upper_sw.set_policy(Gtk.PolicyType.NEVER,
                                 Gtk.PolicyType.EXTERNAL)
        self.upper_sw.set_propagate_natural_height(True)
        self.upper_sw.set_shadow_type(Gtk.ShadowType.NONE)
        self.upper_sw.add(upper)
        self.split.pack1(self.upper_sw, True, True)
        self.split.pack2(lower, True, False)
        # Any click in the editor snaps it back to full height. Capture
        # phase + unclaimed, so the click still reaches the slider/button.
        self._snap_gesture = Gtk.GestureMultiPress.new(self.upper_sw)
        self._snap_gesture.set_button(0)
        self._snap_gesture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        self._snap_gesture.connect('pressed',
                                   lambda *_: self.snap_editor_open())
        self._snap_anim = 0

        top = Gtk.Box(spacing=12)
        frame = Gtk.Frame()
        self.layout = MonitorLayout(self)
        frame.add(self.layout)
        top.pack_start(frame, True, True, 0)
        top.pack_start(self._build_controls(), False, False, 0)
        upper.pack_start(top, True, True, 0)
        hint = Gtk.Label(xalign=0, label=(
            'Click a monitor (Ctrl+click for several), then click a wallpaper '
            'below. In the preview: drag to move, scroll to zoom, '
            'Shift+scroll to rotate. Drag the divider below to resize the '
            'library.'))
        hint.get_style_context().add_class('dim-label')
        upper.add(hint)
        lower.add(self._build_library_bar())
        sw = Gtk.ScrolledWindow(vexpand=True)
        sw.set_size_request(-1, 120)
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.flow = Gtk.FlowBox(homogeneous=True, valign=Gtk.Align.START,
                                activate_on_single_click=True,
                                max_children_per_line=30,
                                selection_mode=Gtk.SelectionMode.SINGLE)
        self.flow.set_filter_func(self._filter)
        self.flow.connect('child-activated', self._on_item_activated)
        sw.add(self.flow)
        lower.pack_start(sw, True, True, 0)
        root.add(self._build_bottom_bar())

        self.reload_library()
        self.refresh_profiles()
        self.sync_controls()
        self.show_all()
        pos = self.app.cfg.get('gui', {}).get('split')
        if pos:
            self.split.set_position(pos)

    # ---- editor/library divider ----------------------------------------
    def editor_height(self):
        """Paned position at which the editor is fully visible."""
        return self.upper_sw.get_preferred_height()[1]

    def snap_editor_open(self):
        target = self.editor_height()
        start = self.split.get_position()
        if start >= target - 2 or self._snap_anim:
            return
        self.upper_sw.get_vadjustment().set_value(0)
        t0 = GLib.get_monotonic_time()
        duration = 160_000       # microseconds

        def step():
            k = min(1.0, (GLib.get_monotonic_time() - t0) / duration)
            ease = 1 - (1 - k) ** 3
            self.split.set_position(int(start + (target - start) * ease))
            if k >= 1.0:
                self._snap_anim = 0
                return False
            return True
        self._snap_anim = GLib.timeout_add(16, step)

    # ---- helpers -------------------------------------------------------
    def profile(self):
        return config.active_profile(self.edit)

    def thumb(self, path):
        t = self.thumbs.get(path)
        if t is None and os.path.exists(path):
            self.thumbs.request(path, lambda *_: self.layout.queue_draw())
        return t

    def changed(self):
        """Call after any edit to the wallpapers being shown."""
        self.layout.queue_draw()
        if self.live.get_active() and not self._live_pending:
            # Coalesce bursts (slider drags) to ~30 desktop updates/second.
            self._live_pending = GLib.timeout_add(33, self._push_live)

    def _preview_profile(self):
        p = self.profile()
        key = self.ss_key()
        if key and self.ss_item is not None:
            p = config.clone(p)
            p['monitors'][key]['_preview_item'] = self.ss_item
        return p

    def _push_live(self):
        self._live_pending = 0
        self.app.preview(self._preview_profile())
        return False

    def _on_live(self, btn):
        if btn.get_active():
            self.app.preview(self._preview_profile())
            self._status('Live preview on: the desktop follows your edits. '
                         'OK keeps them, Cancel restores.')
        else:
            if self._live_pending:
                GLib.source_remove(self._live_pending)
                self._live_pending = 0
            self.app.end_preview()

    def ss_key(self):
        """The monitor whose slideshow is being edited: exactly one
        monitor selected and it holds a slideshow."""
        if len(self.selected) == 1:
            key = next(iter(self.selected))
            if transform.is_slideshow(self.profile()['monitors'].get(key)):
                return key
        return None

    def ss_entry(self):
        key = self.ss_key()
        return self.profile()['monitors'][key] if key else None

    def edit_wp(self, key):
        """The wallpaper dict the controls edit for this monitor: the entry
        itself, or the selected slideshow item (None if nothing)."""
        entry = self.profile()['monitors'].get(key)
        if not transform.is_slideshow(entry):
            return entry
        items = entry.get('items', [])
        if key == self.ss_key() and self.ss_item is not None \
                and self.ss_item < len(items):
            return items[self.ss_item]
        return None

    def shown_wp(self, key):
        """What the layout preview draws for this monitor."""
        entry = self.profile()['monitors'].get(key)
        if not transform.is_slideshow(entry):
            return entry
        return self.edit_wp(key) or (entry.get('items') or [None])[0]

    def all_paths(self):
        for entry in self.profile()['monitors'].values():
            if transform.is_slideshow(entry):
                for item in entry.get('items', []):
                    yield item['path']
            elif entry:
                yield entry['path']

    def selected_wps(self):
        return [w for w in (self.edit_wp(k) for k in sorted(self.selected))
                if w]

    def set_selected(self, keys):
        if set(keys) != self.selected:
            self.ss_item = None
        self.selected = set(keys)
        self.sync_controls()
        self.layout.queue_draw()

    # ---- profile bar ---------------------------------------------------
    def _build_profile_bar(self):
        bar = Gtk.Box(spacing=6)
        bar.add(Gtk.Label(label='Profile:'))
        self.profile_combo = Gtk.ComboBoxText()
        self.profile_combo.set_size_request(220, -1)
        self.profile_combo.connect('changed', self._on_profile_changed)
        bar.add(self.profile_combo)
        for label, tip, cb in (
                ('New', 'Create an empty profile', self._new_profile),
                ('Duplicate', 'Copy this profile', self._dup_profile),
                ('Rename', 'Rename this profile', self._rename_profile),
                ('Delete', 'Delete this profile', self._delete_profile)):
            b = Gtk.Button(label=label, tooltip_text=tip)
            b.connect('clicked', cb)
            bar.add(b)
        s = Gtk.Button(label='Shortcuts & settings…')
        s.connect('clicked', lambda _b: self._settings())
        bar.pack_end(s, False, False, 0)
        return bar

    def refresh_profiles(self):
        self._profile_combo_busy = True
        self.profile_combo.remove_all()
        for p in self.edit['profiles']:
            label = p['name']
            if p.get('keybind'):
                label += f"   ({accel_label(p['keybind'])})"
            self.profile_combo.append(p['id'], label)
        self.profile_combo.set_active_id(self.edit['active_profile'])
        self._profile_combo_busy = False

    def _on_profile_changed(self, combo):
        if self._profile_combo_busy or not combo.get_active_id():
            return
        self.edit['active_profile'] = combo.get_active_id()
        self.sync_controls()
        self.changed()

    def _ask_name(self, title, initial=''):
        d = Gtk.Dialog(title=title, transient_for=self, modal=True)
        d.add_button('Cancel', Gtk.ResponseType.CANCEL)
        d.add_button('OK', Gtk.ResponseType.OK)
        d.set_default_response(Gtk.ResponseType.OK)
        entry = Gtk.Entry(text=initial, activates_default=True)
        box = d.get_content_area()
        box.set_border_width(12)
        box.add(entry)
        d.show_all()
        resp = d.run()
        name = entry.get_text().strip()
        d.destroy()
        return name if resp == Gtk.ResponseType.OK and name else None

    def _new_profile(self, _b, copy_from=None):
        name = self._ask_name('New profile',
                              f'Profile {len(self.edit["profiles"]) + 1}')
        if not name:
            return
        p = config.new_profile(name)
        if copy_from:
            p['monitors'] = config.clone(copy_from['monitors'])
        self.edit['profiles'].append(p)
        self.edit['active_profile'] = p['id']
        self.refresh_profiles()
        self.sync_controls()
        self.changed()

    def _dup_profile(self, b):
        self._new_profile(b, copy_from=self.profile())

    def _rename_profile(self, _b):
        p = self.profile()
        name = self._ask_name('Rename profile', p['name'])
        if name:
            p['name'] = name
            self.refresh_profiles()

    def _delete_profile(self, _b):
        if len(self.edit['profiles']) <= 1:
            self._status('You need at least one profile.')
            return
        p = self.profile()
        d = Gtk.MessageDialog(transient_for=self, modal=True,
                              message_type=Gtk.MessageType.QUESTION,
                              buttons=Gtk.ButtonsType.OK_CANCEL,
                              text=f"Delete profile “{p['name']}”?")
        resp = d.run()
        d.destroy()
        if resp != Gtk.ResponseType.OK:
            return
        self.edit['profiles'].remove(p)
        self.edit['active_profile'] = self.edit['profiles'][0]['id']
        self.refresh_profiles()
        self.sync_controls()
        self.changed()

    def _settings(self):
        d = SettingsDialog(self)
        d.run()
        d.destroy()
        self.refresh_profiles()

    # ---- transform controls -------------------------------------------
    def _build_controls(self):
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        panel.set_size_request(360, -1)
        self.sel_label = Gtk.Label(xalign=0, use_markup=True)
        panel.add(self.sel_label)
        self.wp_label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.wp_label.get_style_context().add_class('dim-label')
        panel.add(self.wp_label)

        switch = Gtk.Box(spacing=0)
        switch.get_style_context().add_class('linked')
        self.type_single = Gtk.RadioButton.new_with_label(None, 'Single')
        self.type_show = Gtk.RadioButton.new_with_label_from_widget(
            self.type_single, 'Slideshow')
        for b in (self.type_single, self.type_show):
            b.set_mode(False)                   # draw as toggle buttons
            b.connect('toggled', self._on_type)
            switch.pack_start(b, True, True, 0)
        self.type_switch = switch
        panel.add(switch)

        notebook = Gtk.Notebook()
        panel.add(notebook)
        place = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        place.set_border_width(8)
        notebook.append_page(place, Gtk.Label(label='Placement'))
        notebook.append_page(self._build_video_page(),
                             Gtk.Label(label='Video'))
        notebook.append_page(self._build_slideshow_page(),
                             Gtk.Label(label='Slideshow'))
        self.notebook = notebook

        grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        place.add(grid)
        self.mode_combo = Gtk.ComboBoxText()
        for m in transform.MODES:
            self.mode_combo.append(m, transform.MODE_LABELS[m])
        self.mode_combo.connect('changed', self._on_mode)
        grid.attach(Gtk.Label(label='Scale mode', xalign=0), 0, 0, 1, 1)
        grid.attach(self.mode_combo, 1, 0, 2, 1)

        self.adj = {}
        specs = (('zoom', 'Zoom %', 5, 1000, 1, 100),
                 ('offset_x', 'Shift X %', -200, 200, 0.5, 0),
                 ('offset_y', 'Shift Y %', -200, 200, 0.5, 0),
                 ('rotation', 'Rotate °', -180, 180, 0.5, 0))
        for row, (field, label, lo, hi, step, val) in enumerate(specs, 1):
            adj = Gtk.Adjustment(value=val, lower=lo, upper=hi,
                                 step_increment=step, page_increment=step * 10)
            adj.connect('value-changed', self._on_adj, field)
            self.adj[field] = adj
            grid.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            sc = Gtk.Scale(adjustment=adj, draw_value=False, hexpand=True)
            if field != 'zoom':
                sc.add_mark(0, Gtk.PositionType.BOTTOM, None)
            else:
                sc.add_mark(100, Gtk.PositionType.BOTTOM, None)
            grid.attach(sc, 1, row, 1, 1)
            spin = Gtk.SpinButton(adjustment=adj, digits=1, width_chars=6)
            grid.attach(spin, 2, row, 1, 1)

        rot = Gtk.Box(spacing=4)
        for label, delta in (('⟲ 90°', -90), ('⟳ 90°', 90),
                             ('180°', 180)):
            b = Gtk.Button(label=label)
            b.connect('clicked', self._rotate_by, delta)
            rot.pack_start(b, True, True, 0)
        place.add(rot)

        btns = Gtk.Box(spacing=4)
        for label, cb in (('Reset', self._reset_transform),
                          ('Copy to all', self._copy_to_all),
                          ('Clear monitor', self._clear_wallpaper)):
            b = Gtk.Button(label=label)
            b.connect('clicked', cb)
            btns.pack_start(b, True, True, 0)
        place.add(btns)
        self.live = Gtk.ToggleButton(label='◉  Live preview on desktop')
        self.live.set_tooltip_text(
            'Show these settings on the real monitors while you edit. '
            'OK keeps them; Cancel puts the old wallpapers back.')
        self.live.connect('toggled', self._on_live)
        self.live.set_margin_top(6)
        panel.add(self.live)
        self.controls = [grid, rot]
        self.action_buttons = btns
        return panel

    def _build_video_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        page.set_border_width(8)
        self.video_hint = Gtk.Label(xalign=0, wrap=True)
        self.video_hint.set_no_show_all(True)
        self.video_hint.get_style_context().add_class('dim-label')
        page.add(self.video_hint)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        page.add(box)
        self.video_box = box

        row = Gtk.Box(spacing=8)
        row.add(Gtk.Label(label='Speed', xalign=0))
        self.speed_adj = Gtk.Adjustment(value=1, lower=0.1, upper=4,
                                        step_increment=0.05,
                                        page_increment=0.25)
        self.speed_adj.connect('value-changed', self._on_speed)
        sc = Gtk.Scale(adjustment=self.speed_adj, draw_value=False,
                       hexpand=True)
        for v, txt in ((0.5, '½×'), (1, '1×'), (2, '2×'),
                       (4, '4×')):
            sc.add_mark(v, Gtk.PositionType.BOTTOM, txt)
        row.pack_start(sc, True, True, 0)
        row.add(Gtk.SpinButton(adjustment=self.speed_adj, digits=2,
                               width_chars=5))
        box.add(row)

        head = Gtk.Box(spacing=8)
        head.add(Gtk.Label(use_markup=True, label='<b>Loop section</b>',
                           xalign=0))
        self.loop_label = Gtk.Label(xalign=1, hexpand=True)
        self.loop_label.get_style_context().add_class('dim-label')
        head.pack_start(self.loop_label, True, True, 0)
        box.add(head)
        self.range_bar = RangeBar()
        self.range_bar.connect('changed', self._on_range_bar)
        box.add(self.range_bar)

        times = Gtk.Grid(column_spacing=6, row_spacing=4)
        self.loop_adj = {}
        self.frame_img = {}
        for col, (key, label) in enumerate((('loop_start', 'Start (s)'),
                                            ('loop_end', 'End (s)'))):
            adj = Gtk.Adjustment(value=0, lower=0, upper=1,
                                 step_increment=0.1, page_increment=1)
            adj.connect('value-changed', self._on_loop_spin, key)
            self.loop_adj[key] = adj
            times.attach(Gtk.Label(label=label, xalign=0), col, 0, 1, 1)
            times.attach(Gtk.SpinButton(adjustment=adj, digits=2,
                                        width_chars=7), col, 1, 1, 1)
            img = Gtk.Image()
            img.set_size_request(160, 90)
            self.frame_img[key] = img
            frame = Gtk.Frame()
            frame.add(img)
            times.attach(frame, col, 2, 1, 1)
        box.add(times)
        full = Gtk.Button(label='Full video')
        full.set_tooltip_text('Clear the loop section and play everything')
        full.connect('clicked', self._on_full_video)
        box.add(full)
        self.durations = {}          # path -> seconds (0 = unknown)
        self._frame_pending = {}     # key -> timeout id
        self._frame_gen = {'loop_start': 0, 'loop_end': 0}
        self._frame_last = {}
        return page

    def selected_videos(self):
        return [w for w in self.selected_wps()
                if media.kind(w['path']) == 'video']

    def sync_video(self):
        vids = self.selected_videos()
        self.video_box.set_sensitive(bool(vids))
        if not vids:
            self.video_hint.set_text('Select a monitor showing a video to '
                                     'change its speed or loop section.')
            self.video_hint.show()
            return
        wp = vids[0]
        dur = self.durations.get(wp['path'])
        if dur is None:
            dur = self.durations[wp['path']] = 0.0
            self.video_hint.set_text('Reading video length…')
            self.video_hint.show()
            media.run_async(media.video_duration, (wp['path'],),
                            lambda d, p=wp['path']: self._got_duration(p, d))
        elif not dur:
            self.video_hint.set_text('Reading video length…')
            self.video_hint.show()
        else:
            self.video_hint.hide()
        speed, start, end = transform.playback(wp)
        self._syncing = True
        self.speed_adj.set_value(speed)
        upper = dur or max(start, end, 1.0)
        for key in ('loop_start', 'loop_end'):
            self.loop_adj[key].set_upper(upper)
        self.loop_adj['loop_start'].set_value(start)
        self.loop_adj['loop_end'].set_value(end or upper)
        self._syncing = False
        self.range_bar.set_range(dur, start, end)
        shown_end = end or dur
        self.loop_label.set_text(
            f'{fmt_time(start)} – {fmt_time(shown_end)}  '
            f'({shown_end - start:.1f} s)' if dur else '')
        if dur:
            self._queue_frame('loop_start', wp['path'], start)
            self._queue_frame('loop_end', wp['path'],
                              max(start, shown_end - 0.05))

    def _got_duration(self, path, dur):
        self.durations[path] = dur or 0.0
        if any(w['path'] == path for w in self.selected_videos()):
            self.sync_video()

    def _queue_frame(self, key, path, t):
        """Debounced frame grab for the start/end preview images."""
        want = (path, round(t, 2))
        if self._frame_last.get(key) == want:
            return
        self._frame_last[key] = want
        if self._frame_pending.get(key):
            GLib.source_remove(self._frame_pending[key])

        def fire():
            self._frame_pending[key] = 0
            self._frame_gen[key] += 1
            gen = self._frame_gen[key]
            media.run_async(media.frame_at, (path, t, 160),
                            lambda pb: self._got_frame(key, gen, pb))
            return False
        self._frame_pending[key] = GLib.timeout_add(250, fire)

    def _got_frame(self, key, gen, pixbuf):
        if gen == self._frame_gen[key] and pixbuf is not None:
            self.frame_img[key].set_from_pixbuf(pixbuf)

    def _set_playback(self, **fields):
        for wp in self.selected_videos():
            wp.update(fields)
        self.sync_video()
        self.changed()

    def _on_speed(self, adj):
        if not self._syncing:
            self._set_playback(speed=round(adj.get_value(), 3))

    def _on_loop_spin(self, adj, key):
        if self._syncing:
            return
        wp = self.selected_videos()[0]
        dur = self.durations.get(wp['path']) or 0.0
        start = self.loop_adj['loop_start'].get_value()
        end = self.loop_adj['loop_end'].get_value()
        if key == 'loop_start':
            start = min(start, end - 0.1) if end > 0.1 else start
        else:
            end = max(end, start + 0.1)
        if dur and end >= dur - 0.01:
            end = 0.0            # "until the end"
        self._set_playback(loop_start=round(max(0.0, start), 3),
                           loop_end=round(end, 3))

    def _on_range_bar(self, bar):
        end = 0.0 if bar.end >= bar.duration - 0.01 else round(bar.end, 3)
        self._set_playback(loop_start=round(bar.start, 3), loop_end=end)

    def _on_full_video(self, _b):
        self._set_playback(loop_start=0.0, loop_end=0.0)

    # ---- slideshow ------------------------------------------------------
    def _build_slideshow_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        page.set_border_width(8)
        self.ss_hint = Gtk.Label(xalign=0, wrap=True)
        self.ss_hint.set_no_show_all(True)
        self.ss_hint.get_style_context().add_class('dim-label')
        page.add(self.ss_hint)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.ss_box = box
        page.add(box)

        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.set_min_content_height(132)
        sw.set_shadow_type(Gtk.ShadowType.IN)
        self.ss_list = Gtk.ListBox()
        self.ss_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.ss_list.connect('row-selected', self._on_ss_row)
        sw.add(self.ss_list)
        box.add(sw)
        self.ss_rows = []

        btns = Gtk.Box(spacing=4)
        for icon, tip, cb in (
                ('go-up-symbolic', 'Move up', lambda _b: self._ss_move(-1)),
                ('go-down-symbolic', 'Move down',
                 lambda _b: self._ss_move(1)),
                ('list-remove-symbolic', 'Remove from slideshow',
                 lambda _b: self._ss_remove())):
            b = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
            b.set_tooltip_text(tip)
            b.connect('clicked', cb)
            btns.add(b)
        play = Gtk.Button(label='\u25b6 Play slideshow')
        play.set_tooltip_text('Deselect the item so the live preview runs '
                              'the whole slideshow with its transitions')
        play.connect('clicked', lambda _b: self._ss_list_select(None))
        btns.pack_end(play, False, False, 0)
        box.add(btns)

        # per-item timing
        self.ss_item_row = Gtk.Box(spacing=6)
        self.ss_item_label = Gtk.Label(xalign=0)
        self.ss_item_adj = Gtk.Adjustment(value=0, lower=0, upper=3600,
                                          step_increment=1, page_increment=10)
        self.ss_item_adj.connect('value-changed', self._on_ss_item_time)
        self.ss_item_spin = Gtk.SpinButton(adjustment=self.ss_item_adj,
                                           digits=1, width_chars=6)
        self.ss_item_unit = Gtk.Label(xalign=0)
        self.ss_item_unit.get_style_context().add_class('dim-label')
        self.ss_item_row.add(self.ss_item_label)
        self.ss_item_row.add(self.ss_item_spin)
        self.ss_item_row.add(self.ss_item_unit)
        box.add(self.ss_item_row)

        box.add(Gtk.Separator())
        grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        self.ss_trans = Gtk.ComboBoxText()
        for t in transform.TRANSITIONS:
            self.ss_trans.append(t, transform.TRANSITION_LABELS[t])
        self.ss_trans.connect('changed', self._on_ss_setting)
        grid.attach(Gtk.Label(label='Transition', xalign=0), 0, 0, 1, 1)
        grid.attach(self.ss_trans, 1, 0, 2, 1)
        self.ss_adj = {}
        for row, (key, label, lo, hi, step) in enumerate((
                ('transition_time', 'Transition length (s)', 0, 10, 0.1),
                ('photo_duration', 'Photo duration (s)', 0.5, 3600, 1)), 1):
            adj = Gtk.Adjustment(value=lo, lower=lo, upper=hi,
                                 step_increment=step, page_increment=step * 10)
            adj.connect('value-changed', self._on_ss_setting)
            self.ss_adj[key] = adj
            grid.attach(Gtk.Label(label=label, xalign=0, hexpand=True),
                        0, row, 1, 1)
            grid.attach(Gtk.SpinButton(adjustment=adj, digits=1,
                                       width_chars=6), 1, row, 2, 1)
        self.ss_shuffle = Gtk.CheckButton(label='Shuffle order')
        self.ss_shuffle.connect('toggled', self._on_ss_setting)
        grid.attach(self.ss_shuffle, 0, 3, 3, 1)
        box.add(grid)
        tip = Gtk.Label(xalign=0, wrap=True, max_width_chars=40, label=(
            'Click library items to add them. Select an item to adjust its '
            'placement and loop on the other tabs. Videos play their full '
            'length (or loop section), photos use their duration.'))
        tip.get_style_context().add_class('dim-label')
        box.add(tip)
        return page

    def _on_type(self, btn):
        if self._syncing or not btn.get_active():
            return
        mons = self.profile()['monitors']
        to_show = btn is self.type_show
        for k in self.selected:
            entry = mons.get(k)
            if to_show and not transform.is_slideshow(entry):
                mons[k] = transform.new_slideshow([entry] if entry else [])
            elif not to_show and transform.is_slideshow(entry):
                items = entry.get('items', [])
                pick = self.ss_item if (self.ss_item is not None and
                                        self.ss_item < len(items)) else 0
                if items:
                    mons[k] = items[pick]
                else:
                    mons.pop(k)
        self.ss_item = 0 if (to_show and self.ss_entry() and
                             self.ss_entry()['items']) else None
        if to_show:
            self.notebook.set_current_page(2)
        self.sync_controls()
        self.changed()

    def _ss_describe(self, show, item):
        if media.kind(item['path']) == 'video':
            n = transform.video_plays(item)
            speed, start, end = transform.playback(item)
            bits = ['video', 'plays once' if n == 1 else f'plays {n}\u00d7']
            if speed != 1:
                bits.append(f'{speed:g}\u00d7 speed')
            if start or end:
                bits.append('loop section')
            return ' \u00b7 '.join(bits)
        own = item.get('duration')
        secs = transform.photo_duration(show, item)
        return f'photo \u00b7 {secs:g} s' + ('' if own else ' (default)')

    def _ss_rebuild(self):
        """Rebuild the item list for the edited slideshow (only when its
        contents changed; otherwise just sync selection and details)."""
        show = self.ss_entry()
        sig = (self.ss_key(), tuple(i['path'] for i in show['items'])
               if show else ())
        if sig == getattr(self, '_ss_sig', None):
            self._ss_busy = True
            if self.ss_item is not None and self.ss_item < len(self.ss_rows):
                self.ss_list.select_row(self.ss_rows[self.ss_item])
            else:
                self.ss_list.unselect_all()
            self._ss_busy = False
            self._ss_refresh_details()
            return
        self._ss_sig = sig
        self._ss_busy = True
        for row in self.ss_list.get_children():
            self.ss_list.remove(row)
        self.ss_rows = []
        show = self.ss_entry()
        for i, item in enumerate(show.get('items', []) if show else []):
            row = Gtk.ListBoxRow()
            hb = Gtk.Box(spacing=8)
            hb.set_border_width(3)
            img = Gtk.Image.new_from_icon_name(
                'video-x-generic' if media.kind(item['path']) == 'video'
                else 'image-x-generic', Gtk.IconSize.DND)
            img.set_size_request(64, 40)
            hb.add(img)
            vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            vb.add(Gtk.Label(label=f'{i + 1}. {os.path.basename(item["path"])}',
                             xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE))
            detail = Gtk.Label(label=self._ss_describe(show, item), xalign=0)
            detail.get_style_context().add_class('dim-label')
            vb.add(detail)
            hb.pack_start(vb, True, True, 0)
            row.add(hb)
            row.path, row.img, row.detail = item['path'], img, detail
            self.ss_list.add(row)
            self.ss_rows.append(row)
            t = self.thumbs.get(item['path'])
            if t:
                self._ss_set_img(row, t[0])
            else:
                self.thumbs.request(item['path'], self._on_thumb)
        self.ss_list.show_all()
        if self.ss_item is not None and self.ss_item < len(self.ss_rows):
            self.ss_list.select_row(self.ss_rows[self.ss_item])
        else:
            self.ss_list.unselect_all()
        self._ss_busy = False

    @staticmethod
    def _ss_set_img(row, pixbuf):
        pw, ph = pixbuf.get_width(), pixbuf.get_height()
        k = min(64 / pw, 40 / ph)
        row.img.set_from_pixbuf(pixbuf.scale_simple(
            max(1, int(pw * k)), max(1, int(ph * k)), 2))

    def _ss_thumb(self, path, result):
        if result:
            for row in self.ss_rows:
                if row.path == path:
                    self._ss_set_img(row, result[0])

    def _ss_list_select(self, index):
        self.ss_item = index
        self.sync_controls()
        self.changed()

    def _on_ss_row(self, _lb, row):
        if self._ss_busy:
            return
        index = self.ss_rows.index(row) if row in self.ss_rows else None
        if index != self.ss_item:
            self._ss_list_select(index)

    def _ss_move(self, delta):
        show = self.ss_entry()
        i = self.ss_item
        if not show or i is None:
            return
        j = i + delta
        items = show['items']
        if 0 <= j < len(items):
            items[i], items[j] = items[j], items[i]
            self.ss_item = j
            self.sync_controls()
            self.changed()

    def _ss_remove(self):
        show = self.ss_entry()
        i = self.ss_item
        if not show or i is None or i >= len(show['items']):
            return
        del show['items'][i]
        n = len(show['items'])
        self.ss_item = min(i, n - 1) if n else None
        self.sync_controls()
        self.changed()

    def _on_ss_item_time(self, adj):
        if self._syncing:
            return
        wp = self.edit_wp(self.ss_key()) if self.ss_key() else None
        if not wp:
            return
        if media.kind(wp['path']) == 'video':
            wp['plays'] = max(1, int(round(adj.get_value())))
        else:
            wp['duration'] = round(adj.get_value(), 2)   # 0 = default
        self._ss_refresh_details()
        self.changed()

    def _on_ss_setting(self, *_):
        show = self.ss_entry()
        if self._syncing or not show:
            return
        show['transition'] = self.ss_trans.get_active_id() or 'fade'
        show['transition_time'] = round(
            self.ss_adj['transition_time'].get_value(), 2)
        show['photo_duration'] = round(
            self.ss_adj['photo_duration'].get_value(), 2)
        show['shuffle'] = self.ss_shuffle.get_active()
        self._ss_refresh_details()
        self.changed()

    def _ss_refresh_details(self):
        show = self.ss_entry()
        if show:
            for row, item in zip(self.ss_rows, show['items']):
                row.detail.set_text(self._ss_describe(show, item))

    def sync_slideshow(self):
        mons = self.profile()['monitors']
        any_show = any(transform.is_slideshow(mons.get(k))
                       for k in self.selected)
        self._syncing = True
        self.type_switch.set_sensitive(bool(self.selected))
        (self.type_show if any_show else self.type_single).set_active(True)
        self._syncing = False
        show = self.ss_entry()
        self.ss_box.set_sensitive(bool(show))
        if show is None:
            self.ss_hint.set_text(
                'Select one monitor set to Slideshow to edit its slideshow.'
                if any_show else 'Switch a monitor to Slideshow (above) to '
                'build a slideshow of photos and videos.')
            self.ss_hint.show()
        elif not show.get('items'):
            self.ss_hint.set_text('Empty: click library items to add them.')
            self.ss_hint.show()
        else:
            self.ss_hint.hide()
        self._ss_rebuild()
        self._syncing = True
        if show:
            self.ss_trans.set_active_id(show.get('transition', 'fade'))
            self.ss_adj['transition_time'].set_value(
                show.get('transition_time', 1.0))
            self.ss_adj['photo_duration'].set_value(
                show.get('photo_duration', 10.0))
            self.ss_shuffle.set_active(bool(show.get('shuffle')))
        wp = self.edit_wp(self.ss_key()) if show else None
        self.ss_item_row.set_sensitive(bool(wp))
        if wp and media.kind(wp['path']) == 'video':
            self.ss_item_label.set_text('Selected video: play')
            self.ss_item_adj.configure(transform.video_plays(wp), 1, 100,
                                       1, 5, 0)
            self.ss_item_spin.set_digits(0)
            self.ss_item_unit.set_text('time(s), then move on')
        else:
            self.ss_item_label.set_text('Selected photo: show for')
            self.ss_item_adj.configure(float(wp.get('duration') or 0)
                                       if wp else 0, 0, 3600, 1, 10, 0)
            self.ss_item_spin.set_digits(1)
            self.ss_item_unit.set_text('s (0 = photo duration)')
        self._syncing = False

    def sync_controls(self):
        """Refresh the control panel from the selected monitors."""
        n = len(self.selected)
        names = ', '.join(sorted(self.selected)) or 'none'
        self.sel_label.set_markup(
            f'<b>{"Monitor" if n == 1 else "Monitors"}:</b> '
            f'{GLib.markup_escape_text(names)}')
        wps = self.selected_wps()
        has = bool(wps)
        for w in self.controls:
            w.set_sensitive(has)
        self.sync_video()
        self.sync_slideshow()
        if not has:
            show = self.ss_entry()
            self.wp_label.set_text(
                'Slideshow: select an item in the Slideshow tab to adjust it'
                if show and show.get('items') else
                'No wallpaper: pick one from the library')
            self._select_library_item(None)
            return
        wp = wps[0]
        paths = {w['path'] for w in wps}
        self.wp_label.set_text(os.path.basename(wp['path']) if len(paths) == 1
                               else f'{len(paths)} different wallpapers')
        self._syncing = True
        self.mode_combo.set_active_id(wp['mode'])
        self.adj['zoom'].set_value(wp['zoom'] * 100)
        self.adj['offset_x'].set_value(wp['offset_x'] * 100)
        self.adj['offset_y'].set_value(wp['offset_y'] * 100)
        self.adj['rotation'].set_value(wp['rotation'])
        self._syncing = False
        self._select_library_item(wp['path'] if len(paths) == 1 else None)

    def _on_mode(self, combo):
        if self._syncing or not combo.get_active_id():
            return
        for wp in self.selected_wps():
            wp['mode'] = combo.get_active_id()
        self.changed()

    def _on_adj(self, adj, field):
        if self._syncing:
            return
        v = adj.get_value()
        if field != 'rotation':
            v /= 100
        for wp in self.selected_wps():
            wp[field] = v
        self.changed()

    def _rotate_by(self, _b, delta):
        for wp in self.selected_wps():
            wp['rotation'] = _wrap_deg(wp['rotation'] + delta)
        self.sync_controls()
        self.changed()

    def _reset_transform(self, _b):
        for wp in self.selected_wps():
            fresh = transform.new_wallpaper(wp['path'])
            wp.update({k: fresh[k] for k in transform.TRANSFORM_KEYS})
        self.sync_controls()
        self.changed()

    def _copy_to_all(self, _b):
        mons = self.profile()['monitors']
        src = next((mons[k] for k in sorted(self.selected) if k in mons),
                   None)
        if not src:
            return
        for m in self.monitors:
            if mons.get(m['key']) is not src:
                mons[m['key']] = config.clone(src)
        self._status(f"Copied to all {len(self.monitors)} monitors.")
        self.changed()

    def _clear_wallpaper(self, _b):
        for k in self.selected:
            self.profile()['monitors'].pop(k, None)
        self.sync_controls()
        self.changed()

    # ---- library -------------------------------------------------------
    def _build_library_bar(self):
        bar = Gtk.Box(spacing=6)
        lbl = Gtk.Label(use_markup=True, label='<b>Library</b>')
        bar.add(lbl)
        b = Gtk.Button(label='Add files…')
        b.connect('clicked', self._add_files)
        bar.add(b)
        b = Gtk.Button(label='Add folder…')
        b.connect('clicked', self._add_folder)
        bar.add(b)
        self.kind_combo = Gtk.ComboBoxText()
        for k, label in (('all', 'All'), ('image', 'Images'),
                         ('video', 'Videos')):
            self.kind_combo.append(k, label)
        self.kind_combo.set_active_id('all')
        self.kind_combo.connect('changed', lambda _c: self.flow.invalidate_filter())
        self.search = Gtk.SearchEntry(placeholder_text='Filter by name')
        self.search.connect('search-changed',
                            lambda _e: self.flow.invalidate_filter())
        bar.pack_end(self.search, False, False, 0)
        bar.pack_end(self.kind_combo, False, False, 0)
        self.lib_count = Gtk.Label()
        self.lib_count.get_style_context().add_class('dim-label')
        bar.add(self.lib_count)
        return bar

    def _filter(self, item):
        k = self.kind_combo.get_active_id()
        if k != 'all' and item.kind != k:
            return False
        q = self.search.get_text().strip().lower()
        return not q or q in item.name.lower()

    def reload_library(self):
        for child in self.flow.get_children():
            self.flow.remove(child)
        self.items = {}
        entries = media.scan(self.edit['library'])
        for path, source in entries:
            item = LibraryItem(path, source)
            item.eventbox.connect('button-press-event', self._on_item_button,
                                  item)
            self.flow.add(item)
            self.items[path] = item
            self.thumbs.request(path, self._on_thumb)
        self.flow.show_all()
        self.lib_count.set_text(f'{len(entries)} items' if entries else
                                'Empty: add some images or videos')

    def _on_thumb(self, path, result):
        item = self.items.get(path)
        if item and result:
            item.set_thumb(result[0])
        if path in set(self.all_paths()):
            self.layout.queue_draw()
            self._ss_thumb(path, result)

    def _select_library_item(self, path):
        item = self.items.get(path) if path else None
        if item:
            self.flow.select_child(item)
        else:
            self.flow.unselect_all()

    def _save_library(self):
        # Library edits are kept even if the window is cancelled.
        self.app.cfg['library'] = list(self.edit['library'])
        config.save(self.app.cfg)
        self.reload_library()
        self.sync_controls()

    def _add_paths(self, paths):
        lib = self.edit['library']
        added = [p for p in paths if p not in lib]
        lib.extend(added)
        if added:
            self._save_library()

    def _browse(self):
        from .browser import DEFAULT_SIZE, run_browser
        prefs = self.app.cfg.setdefault('browser', {})
        paths, last_dir, size = run_browser(
            self, prefs.get('dir'), prefs.get('size', DEFAULT_SIZE),
            self.edit['library'])
        prefs.update(dir=last_dir, size=size)
        self.edit['browser'] = dict(prefs)
        config.save(self.app.cfg)
        self._add_paths(paths)

    def _add_files(self, _b):
        self._browse()

    def _add_folder(self, _b):
        self._browse()

    def _assign(self, path, keys, replace=False):
        """Put path on these monitors; slideshows get it appended instead
        (unless replace=True)."""
        mons = self.profile()['monitors']
        for k in keys:
            entry = mons.get(k)
            wp = transform.new_wallpaper(path)
            if transform.is_slideshow(entry) and not replace:
                entry.setdefault('items', []).append(wp)
                if k == self.ss_key():
                    self.ss_item = len(entry['items']) - 1
                continue
            if entry and not transform.is_slideshow(entry):
                wp['mode'] = entry['mode']
            mons[k] = wp
        self.sync_controls()
        self.changed()

    def _on_item_activated(self, _flow, item):
        if not self.selected:
            self._status('Select a monitor first.')
            return
        self._assign(item.path, self.selected)

    def _on_item_button(self, _w, ev, item):
        if ev.button != 3:
            return False
        menu = Gtk.Menu()

        def add(label, cb):
            mi = Gtk.MenuItem(label=label)
            mi.connect('activate', lambda _m: cb())
            menu.append(mi)
        if self.ss_key():
            add('Add to slideshow',
                lambda: self._assign(item.path, [self.ss_key()]))
        add('Set on all monitors',
            lambda: self._assign(item.path, [m['key'] for m in self.monitors],
                                 replace=True))
        add('Open containing folder', lambda: subprocess.Popen(
            ['xdg-open', os.path.dirname(item.path)]))
        menu.append(Gtk.SeparatorMenuItem())
        if item.source == item.path:
            add('Remove from library', lambda: self._remove_source(item.source))
        else:
            add(f'Remove folder “{os.path.basename(item.source)}” '
                'from library', lambda: self._remove_source(item.source))
        menu.show_all()
        menu.attach_to_widget(self, None)
        menu.popup_at_pointer(ev)
        return True

    def _remove_source(self, source):
        if source in self.edit['library']:
            self.edit['library'].remove(source)
            self._save_library()

    # ---- bottom bar ----------------------------------------------------
    def _build_bottom_bar(self):
        bar = Gtk.Box(spacing=6)
        self.status = Gtk.Label(xalign=0)
        bar.pack_start(self.status, True, True, 0)
        cancel = Gtk.Button(label='Cancel')
        cancel.connect('clicked', lambda _b: self.destroy())
        ok = Gtk.Button(label='OK')
        ok.get_style_context().add_class('suggested-action')
        ok.connect('clicked', self._on_ok)
        ok.set_size_request(100, -1)
        bar.pack_end(ok, False, False, 0)
        bar.pack_end(cancel, False, False, 0)
        return bar

    def _status(self, text):
        self.status.set_text(text)
        GLib.timeout_add_seconds(
            4, lambda: self.status.get_text() == text and
            self.status.set_text('') and False)

    def _on_ok(self, _b):
        self._committed = True
        self.app.commit(self.edit)
        self.destroy()

    def _on_key(self, _w, ev):
        if ev.keyval == Gdk.KEY_Escape:
            self.destroy()
            return True
        return False

    def _on_destroy(self, _w):
        if self._snap_anim:
            GLib.source_remove(self._snap_anim)
            self._snap_anim = 0
        # Remember where the library divider was (UI state, not a setting,
        # so it's saved even on Cancel).
        self.app.cfg.setdefault('gui', {})['split'] = self.split.get_position()
        config.save(self.app.cfg)
        if self._live_pending:
            GLib.source_remove(self._live_pending)
            self._live_pending = 0
        if not self._committed:
            self.app.end_preview()
        self.thumbs.shutdown()
