import struct
from unittest.mock import Mock

import pytest

from caihong_toolbox.companion import KeyboardCompanion
from caihong_toolbox.hardware import Paths, Unavailable


@pytest.fixture
def keyboard(tmp_path, monkeypatch):
    controls = tmp_path / 'oneplus'
    controls.mkdir()
    (controls / 'f4_compensation').write_text('0\n')
    (controls / 'media_keys').write_text('1\n')
    event = tmp_path / 'event10'
    event.write_bytes(b'')
    node = {'controls': controls, 'event': event, 'address': 'AA:BB:CC:DD:EE:FF'}
    monkeypatch.setattr('caihong_toolbox.companion.keyboard_nodes', lambda paths: [node])
    monkeypatch.setattr('caihong_toolbox.companion.pogo_node', lambda paths: None)
    bluez = Mock()
    return KeyboardCompanion(Paths(tmp_path), bluez), node, bluez


def finish(companion):
    companion.thread.join(timeout=2)
    assert not companion.running()


def test_foreign_gate_is_never_stolen(keyboard):
    companion, node, bluez = keyboard
    (node['controls'] / 'f4_compensation').write_text('1\n')
    with pytest.raises(Unavailable, match='其他程序'):
        companion.start()
    assert (node['controls'] / 'f4_compensation').read_text().strip() == '1'
    bluez.write.assert_not_called()


def test_initial_ble_failure_never_arms_gate(keyboard):
    companion, node, bluez = keyboard
    bluez.write.side_effect = Unavailable('initial request failed')
    with pytest.raises(Unavailable):
        companion.start()
    assert (node['controls'] / 'f4_compensation').read_text().strip() == '0'
    assert not companion.running()


def test_syn_dropped_disarms_instead_of_guessing_state(keyboard):
    companion, node, bluez = keyboard
    node['event'].write_bytes(struct.pack('llHHi', 0, 0, 0, 3, 0))
    companion.start()
    finish(companion)
    assert '输入事件丢失' in companion.error
    assert (node['controls'] / 'f4_compensation').read_text().strip() == '0'
    assert bluez.write.call_args.args[1].hex() == '1301010015'


def test_key_write_failure_disarms_and_requests_touchpad_on(keyboard):
    companion, node, bluez = keyboard
    node['event'].write_bytes(b''.join(struct.pack('llHHi', 0, 0, kind, code, value)
                                     for kind, code, value in ((4, 4, 0x7006b), (1, 62, 1), (0, 0, 0))))
    bluez.write.side_effect = [None, Unavailable('key request failed'), None]
    companion.start()
    finish(companion)
    assert companion.error == 'key request failed'
    assert (node['controls'] / 'f4_compensation').read_text().strip() == '0'
    assert bluez.write.call_args.args[1].hex() == '1301010015'


def test_cleanup_failure_is_visible(keyboard):
    companion, node, bluez = keyboard
    bluez.write.side_effect = [None, Unavailable('cleanup request failed')]
    companion.start()
    finish(companion)
    assert companion.error == 'cleanup request failed'
    assert (node['controls'] / 'f4_compensation').read_text().strip() == '0'


def test_stopping_compensation_preserves_media_preference(keyboard):
    companion, node, bluez = keyboard
    companion.start()
    finish(companion)
    companion.stop()
    assert (node['controls'] / 'media_keys').read_text().strip() == '1'


def test_stop_without_start_sends_nothing(keyboard):
    companion, node, bluez = keyboard
    companion.stop()
    bluez.write.assert_not_called()
