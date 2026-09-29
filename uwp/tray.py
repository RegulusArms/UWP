"""Top-bar icon (AppIndicator, shown by Ubuntu's AppIndicator extension)."""
import os

import gi
from gi.repository import Gtk

try:
    gi.require_version('AyatanaAppIndicator3', '0.1')
    from gi.repository import AyatanaAppIndicator3 as AppIndicator
except (ImportError, ValueError):
    AppIndicator = None

ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'icons')


class Tray:
    def __init__(self, app):
        self.app = app
        self._building = False
        if AppIndicator:
            self.ind = AppIndicator.Indicator.new(
                'uwp', 'uwp-tray',
                AppIndicator.IndicatorCategory.APPLICATION_STATUS)
            self.ind.set_icon_theme_path(ICON_DIR)
            self.ind.set_icon_full('uwp-tray', 'UWP Wallpapers')
            self.ind.set_title('UWP Wallpapers')
            self.ind.set_status(AppIndicator.IndicatorStatus.ACTIVE)
            self.status_icon = None
        else:  # legacy fallback for desktops without AppIndicator
            self.ind = None
            self.status_icon = Gtk.StatusIcon.new_from_file(
                os.path.join(ICON_DIR, 'uwp-tray.svg'))
            self.status_icon.set_tooltip_text('UWP Wallpapers')
            self.status_icon.connect('activate', lambda _i: app.open_gui())
            self.status_icon.connect(
                'popup-menu', lambda _i, b, t: self.menu.popup(
                    None, None, None, None, b, t))
        self.refresh()

    def refresh(self):
        self._building = True
        cfg = self.app.cfg
        menu = Gtk.Menu()
        open_item = Gtk.MenuItem(label='Open UWP…')
        open_item.connect('activate', lambda _m: self.app.open_gui())
        menu.append(open_item)
        menu.append(Gtk.SeparatorMenuItem())
        head = Gtk.MenuItem(label='Profiles')
        head.set_sensitive(False)
        menu.append(head)
        group = None
        for p in cfg['profiles']:
            item = Gtk.RadioMenuItem.new_with_label_from_widget(group, p['name'])
            group = group or item
            item.set_active(p['id'] == cfg['active_profile'])
            item.connect('toggled', self._on_profile, p['id'])
            menu.append(item)
        nxt = Gtk.MenuItem(label='Next profile')
        nxt.connect('activate', lambda _m: self.app.cycle_profile(1))
        menu.append(nxt)
        menu.append(Gtk.SeparatorMenuItem())
        pause = Gtk.CheckMenuItem(label='Pause videos')
        pause.set_active(self.app.desktop.user_paused)
        pause.connect('toggled', lambda m: self.app.desktop.set_user_paused(
            m.get_active()))
        menu.append(pause)
        quit_item = Gtk.MenuItem(label='Quit')
        quit_item.connect('activate', lambda _m: self.app.quit_app())
        menu.append(quit_item)
        menu.show_all()
        self.menu = menu
        if self.ind:
            self.ind.set_menu(menu)
            # Middle-click on the icon opens the window directly.
            self.ind.set_secondary_activate_target(open_item)
        self._building = False

    def _on_profile(self, item, pid):
        if not self._building and item.get_active():
            self.app.set_profile(pid)
