"""Single-instance application: tray icon + desktop renderer + GUI.

Running `uwp` again talks to the existing instance (GApplication), and all
actions are exported on D-Bus so global shortcuts can trigger them.
"""
import os
import sys

from gi.repository import Gdk, Gio, GLib, Gst, Gtk

from . import config, keybind, renderer
from .keybind import APP_ID

AUTOSTART_PATH = os.path.join(os.path.dirname(config.CONFIG_DIR), 'autostart',
                              APP_ID + '.desktop')


def launcher_command():
    return os.environ.get('UWP_LAUNCHER') or \
        f'{sys.executable} -m uwp'


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.gui = None
        self._first = True
        self._reapply_id = 0
        opts = (('open', 'Open the settings window'),
                ('background', 'Start in the tray without opening the window'),
                ('next-profile', 'Switch to the next profile'),
                ('prev-profile', 'Switch to the previous profile'),
                ('quit', 'Quit the running instance'))
        for name, desc in opts:
            self.add_main_option(name, 0, GLib.OptionFlags.NONE,
                                 GLib.OptionArg.NONE, desc, None)
        self.add_main_option('profile', 0, GLib.OptionFlags.NONE,
                             GLib.OptionArg.STRING,
                             'Switch to the profile with this name or id',
                             'NAME')

    # ---- lifecycle -----------------------------------------------------
    def do_startup(self):
        Gtk.Application.do_startup(self)
        Gst.init(None)
        renderer.prefer_hardware_decoders()
        self.cfg = config.load()
        self.desktop = renderer.Desktop()

        for name, cb in (('open', lambda *_: self.open_gui()),
                         ('next-profile', lambda *_: self.cycle_profile(1)),
                         ('prev-profile', lambda *_: self.cycle_profile(-1)),
                         ('toggle-pause', lambda *_: self.toggle_pause()),
                         ('quit', lambda *_: self.quit_app())):
            a = Gio.SimpleAction.new(name, None)
            a.connect('activate', cb)
            self.add_action(a)
        a = Gio.SimpleAction.new('set-profile', GLib.VariantType('s'))
        a.connect('activate', lambda _a, v: self.set_profile(v.get_string()))
        self.add_action(a)

        from .tray import Tray
        self.tray = Tray(self)
        self.cover = renderer.CoverWatcher(self.desktop.set_covered)
        self.cover.set_enabled(self.cfg.get('pause_when_covered', True))

        display = Gdk.Display.get_default()
        display.connect('monitor-added', self._monitors_changed)
        display.connect('monitor-removed', self._monitors_changed)
        Gdk.Screen.get_default().connect('size-changed',
                                         self._monitors_changed)

        self.desktop.apply(config.active_profile(self.cfg))
        keybind.sync(self.cfg)
        self.hold()   # keep running with no windows (tray mode)

    def do_command_line(self, cmdline):
        opts = cmdline.get_options_dict().end().unpack()
        first, self._first = self._first, False
        if opts.get('quit'):
            self.quit_app()
        elif opts.get('next-profile'):
            self.cycle_profile(1)
        elif opts.get('prev-profile'):
            self.cycle_profile(-1)
        elif opts.get('profile'):
            name = opts['profile']
            p = next((p for p in self.cfg['profiles']
                      if name in (p['id'], p['name'])), None)
            if p:
                self.set_profile(p['id'])
            else:
                cmdline.printerr(f'uwp: no profile named {name!r}\n')
                return 1
        elif opts.get('open'):
            self.open_gui()
        elif opts.get('background'):
            pass
        elif not first or not self.cfg['library']:
            # Relaunching (e.g. from the app grid) or first-ever run.
            self.open_gui()
        return 0

    def quit_app(self):
        if self.gui:
            self.gui.destroy()
        self.desktop.shutdown()
        self.release()
        self.quit()

    # ---- profiles ------------------------------------------------------
    def set_profile(self, pid, notify=True):
        p = config.find_profile(self.cfg, pid)
        if not p:
            return
        self.cfg['active_profile'] = pid
        config.save(self.cfg)
        self.desktop.apply(p)
        self.tray.refresh()
        if notify:
            n = Gio.Notification.new('Wallpaper profile')
            n.set_body(p['name'])
            self.send_notification('profile', n)
            GLib.timeout_add_seconds(
                3, lambda: self.withdraw_notification('profile') or False)

    def cycle_profile(self, step):
        ps = self.cfg['profiles']
        ids = [p['id'] for p in ps]
        i = ids.index(self.cfg['active_profile']) if \
            self.cfg['active_profile'] in ids else 0
        self.set_profile(ids[(i + step) % len(ids)])

    def toggle_pause(self):
        self.desktop.set_user_paused(not self.desktop.user_paused)
        self.tray.refresh()

    # ---- GUI -------------------------------------------------------------
    def open_gui(self):
        if self.gui is None:
            from .gui import WallpaperGui
            self.gui = WallpaperGui(self)
            self.gui.connect('destroy', self._gui_closed)
        self.gui.present()

    def _gui_closed(self, _w):
        self.gui = None

    def preview(self, profile):
        """Live preview of an unsaved profile from the GUI."""
        self.desktop.apply(config.clone(profile), preview=True)

    def end_preview(self):
        """Drop the live preview and show the saved profile again."""
        if self.desktop.previewing:
            self.desktop.apply(config.active_profile(self.cfg))

    def commit(self, edit):
        """OK pressed: persist and apply everything from the GUI."""
        autostart = edit.pop('autostart', None)
        edit['library'] = self.cfg['library']
        self.cfg = edit
        config.save(self.cfg)
        if autostart is not None:
            self.set_autostart(autostart)
        if not keybind.sync(self.cfg):
            print('uwp: GNOME custom shortcuts unavailable; '
                  'bind "uwp --next-profile" manually')
        self.cover.set_enabled(self.cfg.get('pause_when_covered', True))
        self.desktop.apply(config.active_profile(self.cfg))
        self.tray.refresh()

    # ---- autostart -----------------------------------------------------
    def autostart_enabled(self):
        return os.path.exists(AUTOSTART_PATH)

    def set_autostart(self, enabled):
        if enabled:
            os.makedirs(os.path.dirname(AUTOSTART_PATH), exist_ok=True)
            with open(AUTOSTART_PATH, 'w') as f:
                f.write('[Desktop Entry]\nType=Application\n'
                        'Name=UWP Wallpapers\n'
                        f'Exec={launcher_command()} --background\n'
                        f'Icon={APP_ID}\nNoDisplay=true\n'
                        'X-GNOME-Autostart-enabled=true\n'
                        'X-GNOME-Autostart-Delay=3\n')
        elif os.path.exists(AUTOSTART_PATH):
            os.remove(AUTOSTART_PATH)

    # ---- monitor hotplug ----------------------------------------------
    def _monitors_changed(self, *_):
        if self._reapply_id:
            GLib.source_remove(self._reapply_id)
        self._reapply_id = GLib.timeout_add(800, self._reapply)

    def _reapply(self):
        self._reapply_id = 0
        self.desktop.reapply()
        return False
