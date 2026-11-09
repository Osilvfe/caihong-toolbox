import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from caihong_toolbox.hardware import Paths, Unavailable, apply_charge, read_snapshot, validate_request


def write(paths, name, value):
    path = paths.path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)
    return path


@pytest.fixture
def paths(tmp_path):
    result = Paths(tmp_path)
    write(result, '/proc/device-tree/compatible', 'oneplus,caihong\x00qcom,sm8650\x00')
    write(result, '/proc/device-tree/model', 'Caihong Test\x00')
    return result


@pytest.fixture
def charging(paths):
    for name, value in {'charge_behaviour': '[auto] inhibit-charge',
                        'charge_control_start_threshold': '95',
                        'charge_control_end_threshold': '100',
                        'device/oneplus_charge_control_status': 'pending=0 last_rc=0 raw_soc=77'}.items():
        write(paths, '/sys/class/power_supply/qcom-battmgr-bat/' + name, value)
    return paths


def test_unknown_values_stay_unknown(paths):
    snapshot = read_snapshot(paths)
    assert snapshot['device']['supported']
    assert snapshot['device']['model'] == 'Caihong Test'
    assert snapshot['battery']['state_of_health'] is None
    assert snapshot['battery']['capacity_ratio'] is None
    assert snapshot['adapter']['online'] is None
    assert not snapshot['battery']['control_available']


def test_capacity_ratio_is_not_firmware_health(paths):
    for name, value in {'state_of_health': '91', 'charge_full': '8000000',
                        'charge_full_design': '10000000', 'capacity': '0'}.items():
        write(paths, '/sys/class/power_supply/qcom-battmgr-bat/' + name, value)
    battery = read_snapshot(paths)['battery']
    assert battery['state_of_health'] == 91
    assert battery['capacity_ratio'] == 80
    assert battery['capacity'] == 0


def test_power_and_protocol_identification(paths):
    for name, value in {'power_now': '25000000', 'voltage_now': '4300000',
                        'current_now': '5800000'}.items():
        write(paths, '/sys/class/power_supply/qcom-battmgr-bat/' + name, value)
    for name, value in {'online': '1', 'voltage_now': '8900000', 'current_now': '700000',
                        'voltage_max': '9400000', 'current_max': '5000000',
                        'input_current_limit': '2000000',
                        'usb_type': 'Unknown [SDP] DCP CDP PD PD_PPS'}.items():
        write(paths, '/sys/class/power_supply/qcom-battmgr-usb/' + name, value)
    write(paths, '/sys/class/power_supply/qcom-battmgr-usb/device/oneplus_pps_capabilities',
          'data_size=512\npdo0=0x0201912c fixed=5000mV 3000mA\n'
          'pdo5=0xc0dc3264 pps=5000-11000mV 5000mA\n')
    write(paths, '/sys/class/usb_power_delivery/pd0/device/supports_usb_power_delivery', 'yes')
    write(paths, '/sys/class/usb_power_delivery/pd0/revision', '3.0')
    write(paths, '/sys/class/usb_power_delivery/pd0/device/device/power_role', 'source [sink]')
    write(paths, '/sys/class/usb_power_delivery/pd0/device/device/data_role', 'host [device]')
    write(paths, '/sys/class/usb_power_delivery/pd0/device/device/orientation', 'normal')
    for name, voltage, current in (('sc8547-1-6f', '8650000', '1500000'),
                                   ('sc8547-0-6f', '8450000', '1500000')):
        write(paths, '/sys/class/power_supply/' + name + '/online', '1')
        write(paths, '/sys/class/power_supply/' + name + '/voltage_now', voltage)
        write(paths, '/sys/class/power_supply/' + name + '/current_now', current)
    snapshot = read_snapshot(paths)
    adapter = snapshot['adapter']
    assert snapshot['battery']['power_uw'] == 25000000
    assert adapter['buck_power_uw'] == 6230000
    assert adapter['pump_power_uw'] == 25650000
    assert adapter['input_power_uw'] == 31880000
    assert adapter['pumps_online'] == 2
    assert adapter['active_type'] == 'SDP'
    assert adapter['pd']['power_role'] == 'sink'
    assert adapter['pps_current_limit_ma'] == 5000
    assert adapter['protocol'] == 'USB PD PPS · 双泵'
    assert 'PPS 5.0–11.0 V / 5.0 A' in adapter['capability_summary']


def test_unplugged_adapter_has_no_inferred_protocol(paths):
    write(paths, '/sys/class/power_supply/qcom-battmgr-usb/online', '0')
    adapter = read_snapshot(paths)['adapter']
    assert adapter['protocol'] == '未接入'
    assert adapter['input_power_uw'] is None
    assert not adapter['capabilities']['available']


def test_online_adapter_without_telemetry_has_no_inferred_power(paths):
    write(paths, '/sys/class/power_supply/qcom-battmgr-usb/online', '1')
    adapter = read_snapshot(paths)['adapter']
    assert adapter['buck_power_uw'] is None
    assert adapter['input_power_uw'] is None


def test_safe_charge_abi(charging):
    assert read_snapshot(charging)['battery']['control_available']
    write(charging, '/sys/class/power_supply/qcom-battmgr-bat/charge_control_start_threshold', '5000')
    assert not read_snapshot(charging)['battery']['control_available']


def test_wrong_board_cannot_control_charge(charging):
    write(charging, '/proc/device-tree/compatible', 'oneplus,other\x00qcom,sm8650\x00')
    assert not read_snapshot(charging)['battery']['control_available']
    with pytest.raises(Unavailable):
        apply_charge(charging, {'action': 'charge-reset'})


@pytest.mark.parametrize('payload', [
    {'action': 'charge-reset'}, {'action': 'pen-feedback'},
    {'action': 'service-set', 'name': 'caihong-ssc.service', 'enabled': True},
    {'action': 'charge-limit', 'start': 75, 'end': 80, 'experimental': True},
    *[{'action': action, 'enabled': enabled} for action in (
        'charge-bypass', 'keyboard-media', 'keyboard-touchpad')
      for enabled in (True, False)],
    *[{'action': 'pen-writing', 'enabled': True, 'tool': tool, 'level': level}
      for tool in ('pencil', 'eraser', 'ballpoint', 'fountain') for level in range(1, 6)],
])
def test_allowed_requests(payload):
    assert validate_request(payload) == payload


@pytest.mark.parametrize('payload', [
    None, [], {}, {'action': []}, {'action': 'shell', 'command': 'id'},
    {'action': 'charge-reset', 'path': '/etc/passwd'},
    {'action': 'keyboard-compensation', 'enabled': False},
    {'action': 'charge-bypass', 'enabled': 1},
    {'action': 'charge-limit', 'start': True, 'end': 80, 'experimental': True},
    {'action': 'charge-limit', 'start': 80, 'end': 80, 'experimental': True},
    {'action': 'charge-limit', 'start': -1, 'end': 80, 'experimental': True},
    {'action': 'charge-limit', 'start': 70, 'end': 101, 'experimental': True},
    {'action': 'charge-limit', 'start': 70, 'end': 80, 'experimental': False},
    {'action': 'pen-writing', 'enabled': True, 'tool': 'unknown', 'level': 3},
    {'action': 'pen-writing', 'enabled': True, 'tool': 'pencil', 'level': True},
    {'action': 'pen-writing', 'enabled': True, 'tool': 'pencil', 'level': 6},
    {'action': 'service-set', 'name': 'sshd.service', 'enabled': True},
    {'action': 'service-set', 'name': 'caihong-toolbox.service', 'enabled': False},
    {'action': 'service-set', 'name': 'caihong-ssc.service', 'enabled': 1},
])
def test_rejects_arbitrary_requests(payload):
    with pytest.raises(ValueError):
        validate_request(payload)


def test_charge_reuses_cli_without_shell_or_save(charging, monkeypatch):
    program = write(charging, '/usr/local/sbin/caihong-charge', 'test fixture')
    program.chmod(0o755)
    monkeypatch.setattr('caihong_toolbox.hardware.os.access', lambda *arguments: True)
    metadata_root = Mock(st_uid=0, st_mode=0o100755)
    real_stat = Path.stat
    monkeypatch.setattr(Path, 'stat', lambda path, **options: metadata_root if path == program else real_stat(path, **options))
    run = Mock(return_value=Mock(returncode=0, stdout=json.dumps({'charge_behaviour': 'auto'})))
    monkeypatch.setattr('caihong_toolbox.hardware.subprocess.run', run)
    apply_charge(charging, {'action': 'charge-limit', 'start': 75, 'end': 80, 'experimental': True})
    assert run.call_args.args[0] == [str(program), 'limit', '75', '80']
    assert '--save' not in run.call_args.args[0]
    assert 'shell' not in run.call_args.kwargs
