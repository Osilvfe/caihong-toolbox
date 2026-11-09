import json
import os

CONFIG_PATH = '/var/lib/caihong-toolbox/charge-hold.json'
DEFAULT_LEVEL = 80
LEVEL_MIN = 10
LEVEL_MAX = 100


def default_config():
    return {'enabled': False, 'level': DEFAULT_LEVEL}


def validate_enabled(enabled):
    if type(enabled) is not bool:
        raise ValueError('自动维持开关值必须为布尔值')
    return enabled


def validate_level(level):
    if type(level) is not int or not LEVEL_MIN <= level <= LEVEL_MAX:
        raise ValueError(f'目标电量必须为 {LEVEL_MIN}–{LEVEL_MAX} 的整数')
    return level


def load_config(paths):
    config = default_config()
    try:
        data = json.loads(paths.path(CONFIG_PATH).read_text())
    except (OSError, ValueError, UnicodeError):
        return config
    if not isinstance(data, dict):
        return config
    if type(data.get('enabled')) is bool:
        config['enabled'] = data['enabled']
    level = data.get('level')
    if type(level) is int and LEVEL_MIN <= level <= LEVEL_MAX:
        config['level'] = level
    return config


def save_config(paths, enabled, level):
    validate_enabled(enabled)
    validate_level(level)
    path = paths.path(CONFIG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    temporary = path.with_name(path.name + '.tmp')
    payload = json.dumps({'enabled': enabled, 'level': level}, ensure_ascii=False) + '\n'
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        stream.write(payload)
    os.replace(temporary, path)
    return {'enabled': enabled, 'level': level}


def decide(capacity, enabled, level):
    """Return the desired bypass state, or None when no change is justified."""
    if not enabled or type(capacity) is not int:
        return None
    if capacity >= level:
        return True
    if capacity <= level - 1:
        return False
    return None
