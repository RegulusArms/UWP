"""Global shortcuts via GNOME's custom keybindings.

GNOME (X11 and Wayland) runs the command for us, so no key grabbing is
needed. The command pokes the running instance's exported GAction over
D-Bus with `gdbus`, which is near-instant.
"""
from gi.repository import Gio

APP_ID = 'io.github.RegulusArms.UWP'
APP_PATH = '/io/github/RegulusArms/UWP'

MK_SCHEMA = 'org.gnome.settings-daemon.plugins.media-keys'
CK_SCHEMA = MK_SCHEMA + '.custom-keybinding'
CK_BASE = '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/'
PREFIX = 'uwp-'


def supported():
    src = Gio.SettingsSchemaSource.get_default()
    return bool(src and src.lookup(MK_SCHEMA, True)
                and src.lookup(CK_SCHEMA, True))


def action_command(action, param=None):
    arg = f"\"[<'{param}'>]\"" if param else "'[]'"
    return (f'gdbus call --session --dest {APP_ID} --object-path {APP_PATH} '
            f"--method org.gtk.Actions.Activate '{action}' {arg} '{{}}'")


def is_reserved(accel):
    """Ctrl+Alt+F1..F12 / Backspace / Delete are handled by the X server or
    system before GNOME (VT switch, session kill); never register them."""
    from gi.repository import Gdk, Gtk
    key, mods = Gtk.accelerator_parse(accel)
    ctrl_alt = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK
    return ((mods & ctrl_alt) == ctrl_alt and
            (Gdk.KEY_F1 <= key <= Gdk.KEY_F12 or
             key in (Gdk.KEY_BackSpace, Gdk.KEY_Delete)))


def desired_bindings(cfg):
    """slot -> (name, command, accel) for every configured shortcut."""
    out = {}
    if cfg.get('keybind_next'):
        out['next'] = ('UWP: next wallpaper profile',
                       action_command('next-profile'), cfg['keybind_next'])
    if cfg.get('keybind_prev'):
        out['prev'] = ('UWP: previous wallpaper profile',
                       action_command('prev-profile'), cfg['keybind_prev'])
    for p in cfg['profiles']:
        if p.get('keybind'):
            out['profile-' + p['id']] = (
                f"UWP: profile {p['name']}",
                action_command('set-profile', p['id']), p['keybind'])
    return {k: v for k, v in out.items() if v[2] and not is_reserved(v[2])}


def sync(cfg):
    """Make GNOME's custom keybindings match cfg. Returns False if the
    desktop does not support it."""
    if not supported():
        return False
    mk = Gio.Settings.new(MK_SCHEMA)
    paths = list(mk.get_strv('custom-keybindings'))
    want = desired_bindings(cfg)
    want_paths = {CK_BASE + PREFIX + slot + '/': v for slot, v in want.items()}

    for path in [p for p in paths if p.startswith(CK_BASE + PREFIX)]:
        if path not in want_paths:
            s = Gio.Settings.new_with_path(CK_SCHEMA, path)
            for key in ('name', 'command', 'binding'):
                s.reset(key)
            paths.remove(path)
    for path, (name, command, accel) in want_paths.items():
        s = Gio.Settings.new_with_path(CK_SCHEMA, path)
        s.set_string('name', name)
        s.set_string('command', command)
        s.set_string('binding', accel)
        if path not in paths:
            paths.append(path)
    mk.set_strv('custom-keybindings', paths)
    Gio.Settings.sync()
    return True


def clear_all():
    cfg = {'profiles': []}
    sync(cfg)
