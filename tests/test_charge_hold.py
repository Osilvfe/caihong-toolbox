import pytest

from caihong_toolbox import charge_guard
from caihong_toolbox.charge_hold import (CONFIG_PATH, decide, load_config,
                                         save_config, validate_enabled, validate_level)
from caihong_toolbox.hardware import Paths


def snapshot(capacity, behaviour, enabled=True, level=80, available=True):
    return {'battery': {'capacity': capacity, 'charge_behaviour': behaviour,
                        'control_available': available},
            'charge_hold': {'enabled': enabled, 'level': level}}


def test_config_defaults_when_missing(tmp_path):
    assert load_config(Paths(tmp_path)) == {'enabled': False, 'level': 80}


def test_config_roundtrip_with_private_permissions(tmp_path):
    paths = Paths(tmp_path)
    assert save_config(paths, True, 80) == {'enabled': True, 'level': 80}
    assert load_config(paths) == {'enabled': True, 'level': 80}
    config = paths.path(CONFIG_PATH)
    assert config.stat().st_mode & 0o777 == 0o600
    assert config.parent.stat().st_mode & 0o777 == 0o700


def test_invalid_config_falls_back_to_defaults(tmp_path):
    paths = Paths(tmp_path)
    path = paths.path(CONFIG_PATH)
    path.parent.mkdir(parents=True)
    path.write_text('{not json')
    assert load_config(paths) == {'enabled': False, 'level': 80}
    path.write_text('{"enabled": "yes", "level": 500}')
    assert load_config(paths) == {'enabled': False, 'level': 80}


@pytest.mark.parametrize('level', [10, 50, 80, 100])
def test_level_accepts_bounds(level):
    assert validate_level(level) == level


@pytest.mark.parametrize('level', [None, True, 9, 101, '80', 80.0])
def test_level_rejects_invalid_values(level):
    with pytest.raises(ValueError):
        validate_level(level)


def test_enabled_rejects_non_boolean():
    with pytest.raises(ValueError):
        validate_enabled(1)


@pytest.mark.parametrize('capacity,enabled,level,expected', [
    (80, True, 80, True),
    (100, True, 80, True),
    (79, True, 80, False),
    (10, True, 80, False),
    (80, False, 80, None),
    (None, True, 80, None),
])
def test_decide(capacity, enabled, level, expected):
    assert decide(capacity, enabled, level) == expected


def test_enforce_enables_bypass_at_target(monkeypatch):
    calls = []
    monkeypatch.setattr(charge_guard, 'apply_charge',
                        lambda paths, request: calls.append(request))
    result = charge_guard.enforce(object(), snapshot(80, 'auto'))
    assert result == {'changed': True, 'enabled': True}
    assert calls == [{'action': 'charge-bypass', 'enabled': True}]


def test_enforce_resumes_charging_below_target(monkeypatch):
    calls = []
    monkeypatch.setattr(charge_guard, 'apply_charge',
                        lambda paths, request: calls.append(request))
    result = charge_guard.enforce(object(), snapshot(79, 'inhibit-charge'))
    assert result == {'changed': True, 'enabled': False}
    assert calls == [{'action': 'charge-bypass', 'enabled': False}]


def test_enforce_skips_matching_state(monkeypatch):
    calls = []
    monkeypatch.setattr(charge_guard, 'apply_charge',
                        lambda paths, request: calls.append(request))
    assert charge_guard.enforce(object(), snapshot(80, 'inhibit-charge')) == {'changed': False}
    assert calls == []


def test_enforce_skips_when_policy_disabled(monkeypatch):
    calls = []
    monkeypatch.setattr(charge_guard, 'apply_charge',
                        lambda paths, request: calls.append(request))
    assert charge_guard.enforce(object(), snapshot(90, 'auto', enabled=False)) == {'changed': False}
    assert calls == []


def test_enforce_skips_without_control_interface(monkeypatch):
    calls = []
    monkeypatch.setattr(charge_guard, 'apply_charge',
                        lambda paths, request: calls.append(request))
    result = charge_guard.enforce(object(), snapshot(90, 'auto', available=False))
    assert result == {'changed': False, 'error': '缺少 Caihong 专用充电接口'}
    assert calls == []


def test_set_charge_hold_enables_timer_and_persists(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    calls = []
    monkeypatch.setattr(charge_guard, 'set_internal_service',
                        lambda name, enabled: calls.append((name, enabled)))
    monkeypatch.setattr(charge_guard, 'read_snapshot', lambda target: snapshot(80, 'auto'))
    monkeypatch.setattr(charge_guard, 'apply_charge',
                        lambda target, request: calls.append(request))
    result = charge_guard.set_charge_hold(paths, True, 80)
    assert load_config(paths) == {'enabled': True, 'level': 80}
    assert calls[0] == ('caihong-toolbox-charge-guard.timer', True)
    assert calls[1] == {'action': 'charge-bypass', 'enabled': True}
    assert '已启用' in result['message']


def test_set_charge_hold_disable_stops_timer_and_resumes_charging(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    calls = []
    monkeypatch.setattr(charge_guard, 'set_internal_service',
                        lambda name, enabled: calls.append((name, enabled)))
    monkeypatch.setattr(charge_guard, 'read_snapshot', lambda target: snapshot(79, 'inhibit-charge'))
    monkeypatch.setattr(charge_guard, 'apply_charge',
                        lambda target, request: calls.append(request))
    result = charge_guard.set_charge_hold(paths, False, 80)
    assert load_config(paths) == {'enabled': False, 'level': 80}
    assert calls[0] == ('caihong-toolbox-charge-guard.timer', False)
    assert calls[1] == {'action': 'charge-bypass', 'enabled': False}
    assert '已停用' in result['message']