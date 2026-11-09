import json

from .hardware import Unavailable


class KeyboardClient:
    def call(self, method, enabled=None):
        from gi.repository import Gio, GLib
        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        parameters = None if enabled is None else GLib.Variant('(b)', (enabled,))
        try:
            return bus.call_sync('org.caihong.Keyboard1', '/org/caihong/Keyboard1',
                                 'org.caihong.Keyboard1', method, parameters, None,
                                 Gio.DBusCallFlags.NO_AUTO_START, 5000, None).unpack()
        except GLib.Error as error:
            raise Unavailable('键盘系统服务不可用：' + error.message) from error

    def status(self):
        try:
            return json.loads(self.call('GetStatus')[0])
        except (OSError, ValueError, RuntimeError) as error:
            return {'automatic': False, 'compensation_running': False, 'touchpad_target': None,
                    'error': str(error), 'last_write_ms': None}

    def apply(self, request):
        method = {'keyboard-media': 'SetMediaKeys', 'keyboard-touchpad': 'SetTouchpad'}.get(request['action'])
        if method is None or type(request.get('enabled')) is not bool:
            raise ValueError('键盘补偿由系统自动管理，不提供开关')
        self.call(method, request['enabled'])
