import struct
from unittest.mock import Mock

import pytest

from caihong_toolbox.hardware import Paths, Unavailable, validate_request
from caihong_toolbox.pen_slide import decode_events, pen_hid_nodes
from caihong_toolbox.pen_touch import PenTouchSession


@pytest.fixture
def manual_session(tmp_path):
    bluez = Mock()
    session = PenTouchSession(Paths(tmp_path), bluez)
    session.owner, session.ready, session.hid_available = ':1.owner', True, True
    session.address, session.command = 'AA:BB:CC:DD:EE:FF', '/pen/command'
    return session, bluez


def test_only_explicit_manual_switch_changes(manual_session):
    session, bluez = manual_session
    bluez.write.assert_not_called()
    session.set_slide(':1.owner', True)
    session.set_slide(':1.owner', False)
    assert [call.args[1].hex() for call in bluez.write.call_args_list] == ['2c960101', '2c960100']
    assert all(event['kind'] != 'phase' for event in session.events)
    assert 'remaining' not in session.snapshot()


def test_exit_closes_only_manually_enabled_slide(manual_session):
    session, bluez = manual_session
    session.cleanup_slide()
    bluez.write.assert_not_called()
    session.ready = True
    session.set_slide(':1.owner', True)
    session.cleanup_slide()
    assert [call.args[1].hex() for call in bluez.write.call_args_list] == ['2c960101', '2c960100']


@pytest.mark.parametrize('condition', ['other-owner', 'not-ready', 'stopping', 'missing-hid'])
def test_switch_requires_own_ready_observer(manual_session, condition):
    session, bluez = manual_session
    if condition == 'other-owner':
        session.owner = ':1.other'
    elif condition == 'not-ready':
        session.ready = False
    elif condition == 'stopping':
        session.cancel.set()
    else:
        session.hid_available = False
    with pytest.raises(Unavailable):
        session.set_slide(':1.owner', True)
    bluez.write.assert_not_called()


def test_failed_enable_is_unknown_and_exit_retries_off(manual_session):
    session, bluez = manual_session
    bluez.write.side_effect = [Unavailable('write failed'), None]
    with pytest.raises(Unavailable):
        session.set_slide(':1.owner', True)
    assert session.slide_requested is None
    session.cleanup_slide()
    assert session.slide_requested is False
    assert [call.args[1].hex() for call in bluez.write.call_args_list] == ['2c960101', '2c960100']


def test_wheel_and_keys_not_pointer_motion():
    packet = struct.Struct('llHHi')
    events = [(2, 0, 12), (2, 1, -9), (2, 8, -1), (2, 11, -120), (1, 104, 1), (0, 0, 0)]
    decoded = decode_events(b''.join(packet.pack(1, 2, kind, code, value) for kind, code, value in events), 'event8')
    assert [(item['type'], item['code'], item['value']) for item in decoded] == [(2, 8, -1), (2, 11, -120), (1, 104, 1)]
    assert all(item['node'] == 'event8' for item in decoded)


@pytest.mark.parametrize('payload', [b'bad', struct.pack('llHHi', 1, 2, 0, 3, 0)])
def test_incomplete_or_dropped_input_stops(payload):
    with pytest.raises(Unavailable):
        decode_events(payload, 'event8')


def test_nodes_match_only_this_pen_bluetooth_identity(tmp_path):
    paths = Paths(tmp_path)
    for index, name, address, bustype in (
        (0, 'OnePlus Pencil Pro Mouse', 'AA:BB:CC:DD:EE:FF', '0005'),
        (1, 'OnePlus Pencil Pro Keyboard', 'AA:BB:CC:DD:EE:FF', '0005'),
        (2, 'OnePlus Pencil Pro Mouse', '11:22:33:44:55:66', '0005'),
        (3, 'Novatek NT36532E Pen', 'AA:BB:CC:DD:EE:FF', '001c'),
        (4, 'OnePlus Keyboard', 'AA:BB:CC:DD:EE:FF', '0005'),
    ):
        node = paths.path(f'/sys/class/input/event{index}/device')
        (node / 'id').mkdir(parents=True)
        (node / 'name').write_text(name)
        (node / 'uniq').write_text(address.lower())
        (node / 'id/bustype').write_text(bustype)
    nodes = pen_hid_nodes(paths, 'AA:BB:CC:DD:EE:FF')
    assert [node.name for node, name in nodes] == ['event0', 'event1']
    with pytest.raises(Unavailable):
        pen_hid_nodes(paths, '77:88:99:AA:BB:CC')


@pytest.mark.parametrize('payload', [
    {'action': 'pen-slide-set', 'enabled': 1},
    {'action': 'pen-slide-set', 'enabled': True, 'seconds': 3600},
    {'action': 'pen-slide-start', 'undocked': True},
])
def test_slide_start_rejects_unapproved_parameters(payload):
    with pytest.raises(ValueError):
        validate_request(payload)


def test_manual_slide_request():
    assert validate_request({'action': 'pen-slide-set', 'enabled': True})
    assert validate_request({'action': 'pen-slide-set', 'enabled': False})
