import json
import time
from unittest.mock import Mock

import pytest

from caihong_toolbox.hardware import Paths, Unavailable, validate_request
from caihong_toolbox.pen_touch import BATTERY_UUID, DOUBLE_UUID, PenTouchSession, refresh_rate


@pytest.mark.parametrize('payload', [
    {'action': 'pen-touch-start', 'seconds': True, 'undocked': True},
    {'action': 'pen-touch-start', 'seconds': 121, 'undocked': True},
    {'action': 'pen-touch-start', 'seconds': 14, 'undocked': True},
    {'action': 'pen-touch-start', 'seconds': 60, 'undocked': False},
    {'action': 'pen-touch-start', 'seconds': 60, 'undocked': 1},
    {'action': 'pen-touch-start', 'seconds': 60, 'undocked': True, 'payload': 'ff'},
])
def test_invalid_touch_request(payload):
    with pytest.raises(ValueError):
        validate_request(payload)


def test_touch_request_without_prescribed_duration():
    assert validate_request({'action': 'pen-touch-start', 'undocked': True})


def test_only_dedicated_ready_double_counts(tmp_path):
    session = PenTouchSession(Paths(tmp_path), Mock())
    session.started = time.monotonic()
    session.observe('double', b'\x01')
    session.ready = True
    session.observe('debug', b'film 24\r\n')
    session.observe('battery', b'\x01')
    session.observe('double', b'\x02')
    assert session.count == 0
    session.observe('double', b'\x01')
    assert session.count == 1
    assert [entry['kind'] for entry in session.events] == ['double', 'film', 'battery', 'double', 'double-tap']


def test_cached_or_wrong_firmware_not_readiness(tmp_path):
    session = PenTouchSession(Paths(tmp_path), Mock())
    session.observe('command', bytes.fromhex('2c0601054d45030011'))
    assert not session.fresh_reply
    session.observe('command', bytes.fromhex('2c0601054d45030010'))
    assert session.fresh_reply


def test_owner_only_heartbeat_and_stop(tmp_path):
    session = PenTouchSession(Paths(tmp_path), Mock())
    session.owner, session.last_heartbeat = ':1.owner', 0
    session.heartbeat(':1.other')
    session.stop(':1.other')
    assert session.last_heartbeat == 0
    assert not session.cancel.is_set()
    session.heartbeat(':1.owner')
    assert session.last_heartbeat > 0
    session.stop(':1.owner')
    assert session.cancel.is_set()


@pytest.mark.parametrize('rate', [60, 90, 120, 144])
def test_active_refresh_parser(tmp_path, rate):
    paths = Paths(tmp_path)
    node = paths.path('/sys/kernel/debug/dri/0/state')
    node.parent.mkdir(parents=True)
    node.write_text(f'crtc[1]:\n\tactive=1\n\tmode: "3000x2120": {rate} 1234\n')
    assert refresh_rate(paths) == rate
    node.write_text('crtc[1]:\n\tactive=0\n')
    assert refresh_rate(paths) is None


@pytest.fixture
def trial(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    identity = paths.path('/var/lib/caihong-pen/paired.json')
    identity.parent.mkdir(parents=True)
    identity.write_text(json.dumps({'address': 'AA:BB:CC:DD:EE:FF'}))
    bluez = Mock()
    bluez.pen.return_value = ('AA:BB:CC:DD:EE:FF', '/pen/command')
    bluez.objects.return_value = {}
    bluez.target.return_value = ('/pen', {})
    bluez.characteristic.side_effect = lambda objects, device, service, characteristic, flags: '/pen/' + characteristic
    session = PenTouchSession(paths, bluez)
    session.started = session.last_heartbeat = time.monotonic()
    monkeypatch.setattr('caihong_toolbox.pen_touch.time.sleep', lambda seconds: session.cancel.set())
    return session, bluez


def test_vendor_gate_and_cleanup_with_fresh_reply(trial):
    session, bluez = trial
    bluez.write.side_effect = lambda path, payload: session.observe('command', bytes.fromhex('2c0601054d45030010')) if payload.hex() == '2c060105' else None
    session.run()
    acquired = [call.args[0] for call in bluez.call.call_args_list if call.args[2] == 'StartNotify']
    released = [call.args[0] for call in bluez.call.call_args_list if call.args[2] == 'StopNotify']
    assert '/pen/' + BATTERY_UUID in acquired
    assert '/pen/' + DOUBLE_UUID in acquired
    assert released == list(reversed(acquired))
    assert [call.args[1].hex() for call in bluez.write.call_args_list] == ['2c060105', '2c390101', '2c950100']
    assert not session.ready
    bluez.bus.signal_unsubscribe.assert_called_once()


def test_no_initialization_without_fresh_reply(trial):
    session, bluez = trial
    session.run()
    assert [call.args[1].hex() for call in bluez.write.call_args_list] == ['2c060105']


def test_lost_window_heartbeat_does_not_initialize(trial):
    session, bluez = trial
    session.last_heartbeat = time.monotonic() - 20
    bluez.write.side_effect = lambda path, payload: session.observe('command', bytes.fromhex('2c0601054d45030010'))
    session.run()
    assert [call.args[1].hex() for call in bluez.write.call_args_list] == ['2c060105']
    assert any(call.args[2] == 'StopNotify' for call in bluez.call.call_args_list)


def test_disconnect_stops_without_reconnect(trial):
    session, bluez = trial
    bluez.target.side_effect = [('/pen', {}), Unavailable('disconnected')]
    session.run()
    assert 'disconnected' in session.error
    bluez.bus.signal_unsubscribe.assert_called_once()
    assert any(call.args[2] == 'StopNotify' for call in bluez.call.call_args_list)
    assert not any(call.args[2] == 'Connect' for call in bluez.call.call_args_list)


def test_failed_subscription_releases_previous_only(trial):
    session, bluez = trial
    def call(path, interface, method):
        if method == 'StartNotify' and path.endswith(DOUBLE_UUID):
            raise Unavailable('subscription failed')
    bluez.call.side_effect = call
    session.run()
    assert 'subscription failed' in session.error
    assert [call.args[0] for call in bluez.call.call_args_list if call.args[2] == 'StopNotify'] == ['/pen/' + BATTERY_UUID, '/pen/command']
    bluez.write.assert_not_called()


def test_wrong_identity_never_subscribes(trial):
    session, bluez = trial
    bluez.pen.return_value = ('11:22:33:44:55:66', '/other/command')
    session.run()
    assert '身份' in session.error
    bluez.call.assert_not_called()
    bluez.write.assert_not_called()


def test_event_storage_bounded(tmp_path):
    session = PenTouchSession(Paths(tmp_path), Mock())
    for index in range(300):
        session.record('debug', str(index))
    assert len(session.events) == 256
    assert session.events[0]['id'] == 45


def test_hex_film_codes_are_diagnostic_only(tmp_path):
    session = PenTouchSession(Paths(tmp_path), Mock())
    session.ready = True
    session.observe('debug', b'film 05')
    session.observe('debug', b'film FF')
    assert [event['kind'] for event in session.events] == ['film', 'film']
    assert session.count == 0


@pytest.mark.parametrize('fail_input', [False, True])
def test_slide_exit_closes_switch_and_input(trial, monkeypatch, fail_input):
    session, bluez = trial
    observer = Mock()
    observer.read.return_value = []
    if fail_input:
        observer.read.side_effect = Unavailable('input failed')
    monkeypatch.setattr('caihong_toolbox.pen_touch.PenHidObserver', lambda paths, address: observer)
    bluez.write.side_effect = lambda path, payload: session.observe('command', bytes.fromhex('2c0601054d45030010')) if payload.hex() == '2c060105' else None
    session.run()
    assert [call.args[1].hex() for call in bluez.write.call_args_list] == ['2c060105', '2c390101', '2c950100']
    observer.close.assert_called_once()
    assert not any(event['kind'] == 'slide-request' for event in session.events)
    if fail_input:
        assert 'input failed' in session.error


def test_missing_pen_mouse_still_allows_double_observation(trial, monkeypatch):
    session, bluez = trial
    def unavailable(paths, address):
        raise Unavailable('missing pen mouse')
    monkeypatch.setattr('caihong_toolbox.pen_touch.PenHidObserver', unavailable)
    session.run()
    assert [call.args[1].hex() for call in bluez.write.call_args_list] == ['2c060105']
    assert any('missing pen mouse' in str(event['value']) for event in session.events)


def test_long_observation_does_not_stop_at_old_event_limit(tmp_path):
    session = PenTouchSession(Paths(tmp_path), Mock())
    for index in range(4200):
        session.observe('battery', b'\x50')
    assert not session.cancel.is_set()
    assert len(session.events) == 256
