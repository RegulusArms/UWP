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


def find_profile(cfg, pid):
    return next((p for p in cfg['profiles'] if p['id'] == pid), None)


def active_profile(cfg):
    return find_profile(cfg, cfg['active_profile']) or cfg['profiles'][0]


def clone(cfg):
    return copy.deepcopy(cfg)
