import json
from unittest.mock import Mock

import pytest

from caihong_toolbox.companion import InputManager
from caihong_toolbox.hardware import Paths, Unavailable
from caihong_toolbox.keyboard import KeyboardClient


def test_status_reads_system_daemon_without_enable_request():
    client = KeyboardClient()
    snapshot = {'automatic': True, 'compensation_running': True, 'media_keys': 0}
    client.call = Mock(return_value=(json.dumps(snapshot),))
    assert client.status() == snapshot
    client.call.assert_called_once_with('GetStatus')


def test_daemon_absence_is_not_claimed_as_enabled():
    client = KeyboardClient()
    client.call = Mock(side_effect=Unavailable('not installed'))
    assert not client.status()['automatic']
    assert 'not installed' in client.status()['error']


@pytest.mark.parametrize('action,method', [('keyboard-media', 'SetMediaKeys'), ('keyboard-touchpad', 'SetTouchpad')])
def test_preferences_delegate_without_owning_compensation(action, method):
    client = KeyboardClient()
    client.call = Mock()
    client.apply({'action': action, 'enabled': False})
    client.call.assert_called_once_with(method, False)


def test_compensation_has_no_public_switch():
    client = KeyboardClient()
    client.call = Mock()
    with pytest.raises(ValueError):
        client.apply({'action': 'keyboard-compensation', 'enabled': False})
    client.call.assert_not_called()


def test_toolbox_exit_does_not_stop_system_keyboard(tmp_path):
    manager = InputManager(Paths(tmp_path), Mock())
    manager.keyboard = Mock()
    manager.close()
    manager.keyboard.stop.assert_not_called()
