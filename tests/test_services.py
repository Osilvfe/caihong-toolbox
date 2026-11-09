from unittest.mock import Mock

import pytest

from caihong_toolbox import services


def test_services_parse_systemd_state_and_hide_timer_services(monkeypatch):
    unit_files = {
        'caihong-keyboard.service': 'enabled',
        'caihong-pen-charge.timer': 'enabled',
        'caihong-pen-charge.service': 'static',
        'caihong-toolbox.service': 'static',
        'caihong-hw-report.service': 'masked-runtime',
    }
    payload = '''Id=caihong-keyboard.service
Description=caihong-keyboard.service
LoadState=loaded
UnitFileState=enabled
ActiveState=active
SubState=running

Id=caihong-pen-charge.timer
Description=caihong-pen-charge.timer
LoadState=loaded
UnitFileState=enabled
ActiveState=active
SubState=waiting

Id=caihong-toolbox.service
Description=caihong-toolbox.service
LoadState=loaded
UnitFileState=static
ActiveState=active
SubState=running

Id=caihong-hw-report.service
Description=caihong-hw-report.service
LoadState=masked
UnitFileState=masked-runtime
ActiveState=inactive
SubState=dead
'''
    monkeypatch.setattr(services, '_unit_files', lambda: unit_files)
    monkeypatch.setattr(services, '_run', lambda arguments, timeout=4: Mock(
        returncode=0, stdout=payload, stderr=''))
    result = services.read_services()
    assert result['error'] is None
    found = {item['unit']: item for item in result['items']}
    assert 'caihong-pen-charge.service' not in found
    assert 'caihong-hw-report.service' not in found
    assert found['caihong-keyboard.service']['requires'] == 'keyboard'
    assert found['caihong-pen-charge.timer']['requires'] == 'pen'
    assert found['caihong-toolbox.service']['protected']
    assert not found['caihong-toolbox.service']['manageable']


def test_services_reject_non_caihong_and_protected_units(monkeypatch):
    with pytest.raises(ValueError):
        services.set_service('sshd.service', False)
    with pytest.raises(ValueError):
        services.set_service('caihong-toolbox.service', False)
    with pytest.raises(ValueError):
        services.set_service('caihong-ssc.service', 1)


def test_services_use_systemctl_enable_now_and_disable_now(monkeypatch):
    calls = []
    monkeypatch.setattr(services, '_run', lambda arguments, timeout=4: (
        calls.append((arguments, timeout)) or Mock(returncode=0, stdout='', stderr='')))
    assert '已启用并运行' in services.set_service('caihong-ssc.service', True)['message']
    assert '已停用并停止' in services.set_service('caihong-ssc.service', False)['message']
    assert calls == [
        (['systemctl', 'enable', '--now', 'caihong-ssc.service'], 20),
        (['systemctl', 'disable', '--now', 'caihong-ssc.service'], 20),
    ]


def test_charge_hold_timer_is_protected_but_internal_toggle_works(monkeypatch):
    with pytest.raises(ValueError):
        services.set_service(services.CHARGE_HOLD_TIMER, True)
    calls = []
    monkeypatch.setattr(services, '_run', lambda arguments, timeout=4: (
        calls.append((arguments, timeout)) or Mock(returncode=0, stdout='', stderr='')))
    result = services.set_internal_service(services.CHARGE_HOLD_TIMER, True)
    assert '已启用并运行' in result['message']
    assert calls == [(['systemctl', 'enable', '--now', services.CHARGE_HOLD_TIMER], 20)]


def test_service_status_exposes_read_only_service_inventory(monkeypatch):
    from caihong_toolbox.service import Service
    service = Service.__new__(Service)
    service.paths = Mock()
    monkeypatch.setattr('caihong_toolbox.service.read_snapshot', lambda paths: {
        'device': {}, 'battery': {}, 'adapter': {}, 'keyboard': {}, 'pen': {},
        'diagnostics': {}})
    service.inputs = Mock()
    service.inputs.keyboard.status.return_value = {}
    service.inputs.pen_active = False
    service.inputs.pen_error = None
    service.inputs.bluez.objects.side_effect = RuntimeError('not available')
    service.touch = Mock()
    service.touch.snapshot.return_value = {}
    monkeypatch.setattr('caihong_toolbox.service.read_services', lambda: {
        'items': [{'unit': 'caihong-ssc.service'}], 'error': None})
    snapshot = service.status()
    assert snapshot['service'] is True
    assert snapshot['services']['items'][0]['unit'] == 'caihong-ssc.service'
