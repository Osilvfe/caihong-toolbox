import json
import threading
from unittest.mock import Mock
from xml.etree import ElementTree

import pytest
from gi.repository import GLib

from caihong_toolbox.hardware import Paths, Unavailable
from caihong_toolbox.service import BUS_NAME, POLKIT_ACTION, Service


@pytest.fixture
def service(tmp_path):
    instance = Service.__new__(Service)
    instance.paths = Paths(tmp_path)
    instance.inputs = Mock()
    instance.touch = Mock()
    instance.bus = Mock()
    instance.pending = 0
    instance.stopping = False
    instance.suspended = False
    instance.submit = Mock()
    instance.operation_lock = threading.Lock()
    return instance


def invoke(service, payload):
    invocation = Mock()
    service.method(None, ':1.123', '/org/caihong/Toolbox1', BUS_NAME, 'Apply',
                   GLib.Variant('(s)', (json.dumps(payload),)), invocation)
    return invocation


def test_status_needs_no_authorization(service):
    invocation = Mock()
    service.method(None, ':1.123', '/', BUS_NAME, 'GetStatus', GLib.Variant('()', ()), invocation)
    service.bus.call.assert_not_called()
    service.submit.assert_called_once_with(service.status, invocation)


def test_invalid_request_never_reaches_polkit_or_hardware(service):
    invocation = invoke(service, {'action': 'shell', 'command': 'id'})
    invocation.return_dbus_error.assert_called_once()
    service.bus.call.assert_not_called()
    service.submit.assert_not_called()
    assert service.pending == 0


def test_mutation_waits_for_polkit_and_uses_actual_sender(service):
    invocation = invoke(service, {'action': 'pen-feedback'})
    service.submit.assert_not_called()
    arguments = service.bus.call.call_args.args
    subject, action, details, flags, cancellation = arguments[4].unpack()
    assert subject == ('system-bus-name', {'name': ':1.123'})
    assert action == POLKIT_ACTION
    assert flags == 1
    service.bus.call_finish.return_value = GLib.Variant('((bba{ss}))', ((False, False, {}),))
    arguments[-2](service.bus, Mock(), None)
    invocation.return_dbus_error.assert_called_once()
    service.submit.assert_not_called()
    assert service.pending == 0


def test_authorized_request_only_then_submitted(service):
    invocation = invoke(service, {'action': 'pen-feedback'})
    callback = service.bus.call.call_args.args[-2]
    service.bus.call_finish.return_value = GLib.Variant('((bba{ss}))', ((True, False, {}),))
    callback(service.bus, Mock(), None)
    service.submit.assert_called_once_with(service.apply, invocation, {'action': 'pen-feedback'})


def test_service_set_reaches_allowlisted_backend(service, monkeypatch):
    compatible = service.paths.path('/proc/device-tree/compatible')
    compatible.parent.mkdir(parents=True)
    compatible.write_text('oneplus,caihong\x00')
    called = []
    monkeypatch.setattr('caihong_toolbox.service.set_service',
                        lambda name, enabled: called.append((name, enabled)) or {'message': 'ok'})
    result = service.apply({'action': 'service-set', 'name': 'caihong-ssc.service', 'enabled': False})
    assert result == {'message': 'ok'}
    assert called == [('caihong-ssc.service', False)]


@pytest.mark.parametrize('request_data', [{'action': 'pen-touch-start', 'undocked': True},
                                           {'action': 'pen-slide-set', 'enabled': True}])
def test_touch_start_binds_actual_bus_sender(service, request_data):
    request = request_data
    invocation = invoke(service, request)
    callback = service.bus.call.call_args.args[-2]
    service.bus.call_finish.return_value = GLib.Variant('((bba{ss}))', ((True, False, {}),))
    callback(service.bus, Mock(), None)
    service.submit.assert_called_once_with(service.start_touch, invocation, request, ':1.123')


def test_manual_slide_binds_caller_and_boolean(service):
    compatible = service.paths.path('/proc/device-tree/compatible')
    compatible.parent.mkdir(parents=True)
    compatible.write_text('oneplus,caihong\x00')
    service.start_touch({'action': 'pen-slide-set', 'enabled': True}, ':1.123')
    service.touch.set_slide.assert_called_once_with(':1.123', True)


def test_release_only_own_session_without_polkit(service):
    invocation = Mock()
    service.method(None, ':1.123', '/', BUS_NAME, 'ReleasePenTouch', GLib.Variant('()', ()), invocation)
    service.bus.call.assert_not_called()
    service.submit.assert_called_once_with(service.release_touch, invocation, ':1.123')


def test_read_status_renews_only_actual_sender(service):
    service.method(None, ':1.123', '/', BUS_NAME, 'GetStatus', GLib.Variant('()', ()), Mock())
    service.touch.heartbeat.assert_called_once_with(':1.123')


def test_other_board_rejects_mutations_even_after_authorization(service):
    with pytest.raises(Unavailable):
        service.apply({'action': 'pen-feedback'})
    service.inputs.pen_apply.assert_not_called()


def test_bus_requests_are_bounded(service):
    service.pending = 4
    invocation = invoke(service, {'action': 'pen-feedback'})
    invocation.return_dbus_error.assert_called_once()
    service.bus.call.assert_not_called()
    service.submit.assert_not_called()


@pytest.mark.parametrize('condition', ['stopping', 'suspended'])
def test_no_mutation_during_shutdown_or_sleep(service, condition):
    compatible = service.paths.path('/proc/device-tree/compatible')
    compatible.parent.mkdir(parents=True)
    compatible.write_text('oneplus,caihong\x00')
    setattr(service, condition, True)
    with pytest.raises(Unavailable):
        service.apply({'action': 'pen-feedback'})
    service.inputs.pen_apply.assert_not_called()


def test_xml_assets_parse_and_no_passwordless_action():
    from pathlib import Path
    base = Path(__file__).resolve().parent.parent
    policy = ElementTree.parse(base / 'packaging/org.caihong.toolbox.policy')
    assert policy.find('.//action').attrib['id'] == POLKIT_ACTION
    assert policy.find('.//allow_active').text == 'auth_admin_keep'
    assert policy.find('.//allow_any').text == 'no'
    assert policy.find('.//allow_inactive').text == 'no'
    ElementTree.parse(base / 'packaging/org.caihong.Toolbox1.conf')
    ElementTree.parse(base / 'resources/caihong-toolbox.svg')


def test_sleep_cleans_inputs_but_resume_does_not_restart_them(service):
    service.pool = Mock()
    service.pool.submit.side_effect = lambda function: function()
    service.sleep_changed(None, None, None, None, None, GLib.Variant('(b)', (True,)), None)
    assert service.suspended
    service.inputs.close.assert_called_once()
    service.touch.stop.assert_called_once_with()
    service.sleep_changed(None, None, None, None, None, GLib.Variant('(b)', (False,)), None)
    assert not service.suspended
    service.inputs.close.assert_called_once()
    service.inputs.keyboard.start.assert_not_called()
