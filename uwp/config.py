"""Persistent settings: library, profiles, keybinds (~/.config/uwp/config.json)."""
import copy
import json
import os
import uuid

CONFIG_DIR = os.path.join(os.environ.get('XDG_CONFIG_HOME') or
                          os.path.expanduser('~/.config'), 'uwp')
CONFIG_PATH = os.path.join(CONFIG_DIR, 'config.json')
CACHE_DIR = os.path.join(os.environ.get('XDG_CACHE_HOME') or
                         os.path.expanduser('~/.cache'), 'uwp')


def child_env():
    """Environment for apps we launch: undo __main__'s X11 forcing so they
    run natively on Wayland."""
    env = dict(os.environ)
    for k in ('GDK_BACKEND', 'GST_GL_WINDOW'):
        env.pop(k, None)
    if env.get('UWP_WAYLAND_DISPLAY'):
        env['WAYLAND_DISPLAY'] = env.pop('UWP_WAYLAND_DISPLAY')
    return env


def new_profile(name):
    # Hex ids keep keybind shell commands free of quoting problems.
    return {'id': uuid.uuid4().hex[:8], 'name': name, 'keybind': '',
            'monitors': {}}


def default_config():
    p = new_profile('Default')
    return {
        'version': 1,
        'library': [],               # files and/or folders
        'profiles': [p],
        'active_profile': p['id'],
        'keybind_next': '',          # GTK accelerator strings, '' = unset
        'keybind_prev': '',
        'pause_when_covered': True,  # pause videos under maximized windows
    }


def load():
    cfg = default_config()
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
        if isinstance(data, dict):
            cfg.update(data)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        print(f'uwp: could not read {CONFIG_PATH}: {e}; using defaults')
    if not cfg['profiles']:
        cfg['profiles'] = [new_profile('Default')]
    if not find_profile(cfg, cfg.get('active_profile')):
        cfg['active_profile'] = cfg['profiles'][0]['id']
    return cfg


def save(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_PATH + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


def remap_monitors(cfg, mons):
    """Move profile entries to a monitor's new connector name when the same
    physical monitor (by EDID id) shows up under another one, e.g. after
    switching between Xorg and Wayland. mons is renderer.monitors().
    Returns True if cfg changed (caller saves)."""
    seen = cfg.setdefault('monitor_ids', {})   # connector -> id, last seen
    ids = [m.get('id') for m in mons]
    # Identical monitors can share an id; those follow the connector only.
    now = {m['key']: m['id'] for m in mons
           if m.get('id') and ids.count(m['id']) == 1}
    by_id = {mid: key for key, mid in now.items()}
    moves = {old: by_id[mid] for old, mid in seen.items()
             if by_id.get(mid, old) != old}
    changed = False
    if moves:
        for p in cfg['profiles']:
            if not any(k in p['monitors'] for k in moves):
                continue
            # A moved entry wins over a stale one already on its new name.
            out = {k: v for k, v in p['monitors'].items()
                   if k not in moves and k not in moves.values()}
            for old, new in moves.items():
                if old in p['monitors']:
                    out[new] = p['monitors'][old]
                elif new in p['monitors'] and new not in moves:
                    out[new] = p['monitors'][new]
            p['monitors'] = out
            changed = True
    for key, mid in now.items():
        if seen.get(key) != mid:
            seen[key] = mid
            changed = True
    for old in moves:
        if old not in now and seen.pop(old, None):
            changed = True
    return changed


def find_profile(cfg, pid):
    return next((p for p in cfg['profiles'] if p['id'] == pid), None)


def active_profile(cfg):
    return find_profile(cfg, cfg['active_profile']) or cfg['profiles'][0]


def clone(cfg):
    return copy.deepcopy(cfg)
