import struct
from unittest.mock import Mock

import pytest

from caihong_toolbox.bluez import Bluez, touchpad_command, writing_command
from caihong_toolbox.companion import InputManager, KeyboardCompanion
from caihong_toolbox.hardware import Paths, Unavailable


@pytest.fixture
def pen_setup(tmp_path):
    paths = Paths(tmp_path)
    node = paths.path('/sys/bus/spi/devices/spi0.0')
    node.mkdir(parents=True)
    driver = tmp_path / 'drivers/nt36532e'
    driver.mkdir(parents=True)
    (node / 'driver').symlink_to(driver)
    (node / 'pen_scan').write_text('1\n')
    (node / 'pen_vibrator').write_text('0\n')
    bluez = Mock()
    bluez.pen.return_value = ('AA:BB:CC:DD:EE:FF', '/pen/command')
    return paths, node, bluez


def test_protocol_defaults():
    assert touchpad_command(True).hex() == '1301010015'
    assert touchpad_command(False).hex() == '1301010116'
    assert writing_command('pencil', 3, True).hex() == '2c94020003'
    for tool in ('pencil', 'eraser', 'ballpoint', 'fountain'):
        for level in range(1, 6):
            assert writing_command(tool, level, True)[-1] == level
            assert writing_command(tool, level, False)[-1] == 0


def test_writing_enable_disable_preserves_scan(pen_setup):
    paths, node, bluez = pen_setup
    manager = InputManager(paths, bluez)
    manager.pen_apply({'action': 'pen-writing', 'enabled': True, 'tool': 'pencil', 'level': 3})
    assert (node / 'pen_vibrator').read_text().strip() == '1'
    assert manager.pen_active['level'] == 3
    manager.pen_apply({'action': 'pen-writing', 'enabled': False, 'tool': 'pencil', 'level': 3})
    assert (node / 'pen_vibrator').read_text().strip() == '0'
    assert manager.pen_active is None
    assert (node / 'pen_scan').read_text().strip() == '1'
    assert [call.args[1].hex() for call in bluez.write.call_args_list] == ['2c94020003', '2c94020000']


def test_writing_failure_requests_cleanup(pen_setup):
    paths, node, bluez = pen_setup
    bluez.write.side_effect = [Unavailable('write failed'), None]
    manager = InputManager(paths, bluez)
    with pytest.raises(Unavailable, match='write failed'):
        manager.pen_apply({'action': 'pen-writing', 'enabled': True, 'tool': 'pencil', 'level': 3})
    assert (node / 'pen_vibrator').read_text().strip() == '0'
    assert manager.pen_active is None
    assert bluez.write.call_args.args[1].hex() == '2c94020000'


def test_unknown_scan_is_not_changed(pen_setup):
    paths, node, bluez = pen_setup
    (node / 'pen_scan').write_text('3\n')
    with pytest.raises(Unavailable):
        InputManager(paths, bluez).pen_apply({'action': 'pen-writing', 'enabled': True, 'tool': 'pencil', 'level': 3})
    bluez.write.assert_not_called()
    assert (node / 'pen_scan').read_text().strip() == '3'


def test_short_feedback_is_rate_limited(pen_setup):
    paths, node, bluez = pen_setup
    manager = InputManager(paths, bluez)
    manager.pen_apply({'action': 'pen-feedback'})
    with pytest.raises(ValueError):
        manager.pen_apply({'action': 'pen-feedback'})
    assert bluez.write.call_count == 1
    assert bluez.write.call_args.args[1].hex() == '2c920102ff02'


def test_monitor_clears_feedback_without_reconnect(pen_setup):
    paths, node, bluez = pen_setup
    manager = InputManager(paths, bluez)
    manager.pen_apply({'action': 'pen-writing', 'enabled': True, 'tool': 'pencil', 'level': 3})
    bluez.objects.return_value = {}
    manager.monitor_pen()
    assert manager.pen_active is None
    assert (node / 'pen_vibrator').read_text().strip() == '0'
    assert (node / 'pen_scan').read_text().strip() == '1'


def test_bluez_failure_still_closes_spi_gate(pen_setup):
    paths, node, bluez = pen_setup
    manager = InputManager(paths, bluez)
    manager.pen_apply({'action': 'pen-writing', 'enabled': True, 'tool': 'pencil', 'level': 3})
    bluez.objects.side_effect = Unavailable('BlueZ unavailable')
    manager.monitor_pen()
    assert manager.pen_active is None
    assert (node / 'pen_vibrator').read_text().strip() == '0'
    assert 'BlueZ unavailable' in manager.pen_error


def test_keyboard_replay_and_cleanup(tmp_path, monkeypatch):
    node = {'event': tmp_path / 'event10', 'controls': tmp_path / 'oneplus', 'address': 'AA:BB:CC:DD:EE:FF'}
    node['controls'].mkdir()
    gate = node['controls'] / 'f4_compensation'
    gate.write_text('0\n')
    packet = struct.Struct('llHHi')
    payload = []
    for code, value in ((62, 1), (62, 0), (530, 1), (530, 0),
                         (62, 1), (62, 2), (62, 0), (530, 1), (530, 0)):
        for kind, event_code, event_value in ((4, 4, 0x7006b), (1, code, value), (0, 0, 0)):
            payload.append(packet.pack(0, 0, kind, event_code, event_value))
    node['event'].write_bytes(b''.join(payload))
    monkeypatch.setattr('caihong_toolbox.companion.keyboard_nodes', lambda paths: [node])
    monkeypatch.setattr('caihong_toolbox.companion.pogo_node', lambda paths: None)
    bluez = Mock()
    companion = KeyboardCompanion(Paths(tmp_path), bluez)
    companion.start()
    companion.thread.join(timeout=2)
    assert not companion.running()
    assert gate.read_text().strip() == '0'
    assert [call.args[1].hex() for call in bluez.write.call_args_list] == [
        '1301010015', '1301010015', '1301010116', '1301010116', '1301010015', '1301010015']


def test_unpaired_or_unresolved_target_rejected():
    bluez = Bluez.__new__(Bluez)
    for properties in ({'Address': 'AA', 'Paired': False},
                       {'Address': 'AA', 'Paired': True, 'Connected': True, 'ServicesResolved': False}):
        with pytest.raises(Unavailable):
            bluez.target({'/device': {'org.bluez.Device1': properties}}, 'AA')


def test_wrong_keyboard_identity_rejected():
    bluez = Bluez.__new__(Bluez)
    bluez.objects = lambda: {'/device': {'org.bluez.Device1': {
        'Address': 'AA', 'Paired': True, 'Connected': True, 'ServicesResolved': True,
        'Modalias': 'usb:v1234p3869d0300'}}}
    with pytest.raises(Unavailable, match='型号'):
        bluez.keyboard_characteristic('AA')
