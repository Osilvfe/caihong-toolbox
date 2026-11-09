from concurrent.futures import ThreadPoolExecutor
import json
import logging
import signal
import threading

from gi.repository import Gio, GLib

from .charge_guard import set_charge_hold
from .companion import InputManager
from .hardware import Paths, Unavailable, apply_charge, is_caihong, read_snapshot, validate_request
from .pen_touch import PenTouchSession
from .services import read_services, set_service


BUS_NAME = 'org.caihong.Toolbox1'
OBJECT_PATH = '/org/caihong/Toolbox1'
POLKIT_ACTION = 'org.caihong.toolbox.configure'
INTERFACE_XML = '''<node><interface name="org.caihong.Toolbox1">
<method name="GetStatus"><arg name="status" type="s" direction="out"/></method>
<method name="Apply"><arg name="request" type="s" direction="in"/>
<arg name="result" type="s" direction="out"/></method>
<method name="ReleasePenTouch"><arg name="result" type="s" direction="out"/></method>
</interface></node>'''


class Service:
    def __init__(self):
        self.paths = Paths()
        self.inputs = InputManager(self.paths)
        self.touch = PenTouchSession(self.paths, self.inputs.bluez)
        self.pool = ThreadPoolExecutor(max_workers=3)
        self.operation_lock = threading.Lock()
        self.pending = 0
        self.monitoring = False
        self.stopping = False
        self.suspended = False
        self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)

    def status(self):
        snapshot = read_snapshot(self.paths)
        snapshot['services'] = read_services()
        snapshot['service'] = True
        snapshot['keyboard'].update(self.inputs.keyboard.status())
        snapshot['pen'].update(writing=self.inputs.pen_active, error=self.inputs.pen_error,
                               connected=False, feedback_available=False)
        snapshot['pen']['touch'] = self.touch.snapshot()
        try:
            objects = self.inputs.bluez.objects()
            identity = self.paths.path('/var/lib/caihong-pen/paired.json')
            address = None
            if identity.exists():
                try:
                    address = json.loads(identity.read_text())['address'].upper()
                except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
                    raise Unavailable('笔身份记录无效，未推测连接状态') from error
            pens = [interfaces['org.bluez.Device1'] for interfaces in objects.values()
                    if interfaces.get('org.bluez.Device1', {}).get('Name') == 'OnePlus Pencil Pro'
                    and interfaces['org.bluez.Device1'].get('Paired')
                    and interfaces['org.bluez.Device1'].get('Connected')
                    and (address is None or interfaces['org.bluez.Device1'].get('Address', '').upper() == address)]
            snapshot['pen']['connected'] = len(pens) == 1
            snapshot['pen']['feedback_available'] = len(pens) == 1
        except RuntimeError as error:
            snapshot['bluetooth_error'] = str(error)
        return snapshot

    def start_touch(self, request, sender):
        validate_request(request)
        if not is_caihong(self.paths):
            raise Unavailable('仅允许在 Caihong 上观察笔身触控')
        with self.operation_lock:
            if self.stopping or self.suspended:
                raise Unavailable('服务停止或休眠中，无法开始观察')
            if request['action'] == 'pen-slide-set':
                return self.touch.set_slide(sender, request['enabled'])
            return self.touch.start(sender)

    def release_touch(self, sender):
        self.touch.stop(sender)
        return {'message': '本窗口的笔身观察已停止'}

    def apply(self, request):
        validate_request(request)
        if not is_caihong(self.paths):
            raise Unavailable('仅允许在设备树匹配 oneplus,caihong 的设备上修改设置')
        if not self.operation_lock.acquire(blocking=False):
            raise RuntimeError('另一个设置请求正在执行，请稍后再试')
        try:
            if self.stopping or self.suspended:
                raise Unavailable('后台正在停止或设备正在休眠，设置未应用')
            logging.info('applying setting: %s', json.dumps(request, ensure_ascii=False))
            if request['action'] == 'service-set':
                return set_service(request['name'], request['enabled'])
            if request['action'] == 'charge-hold':
                return set_charge_hold(self.paths, request['enabled'], request['level'])
            if request['action'].startswith('charge-'):
                apply_charge(self.paths, request)
            elif request['action'].startswith('keyboard-'):
                self.inputs.keyboard_apply(request)
            else:
                self.inputs.pen_apply(request)
            logging.info('setting request completed: %s', request['action'])
            return {'message': '设置请求已完成；实际效果及控制器状态请以设备反馈为准'}
        finally:
            self.operation_lock.release()

    def finished(self, future, invocation):
        self.pending -= 1
        try:
            result = future.result()
            invocation.return_value(GLib.Variant('(s)', (json.dumps(result, ensure_ascii=False),)))
        except Exception as error:
            logging.warning('request failed: %s', error)
            invocation.return_dbus_error(BUS_NAME + '.Error', str(error))
        return False

    def submit(self, function, invocation, *arguments):
        future = self.pool.submit(function, *arguments)
        future.add_done_callback(lambda completed: GLib.idle_add(self.finished, completed, invocation))

    def method(self, connection, sender, path, interface, method, parameters, invocation):
        if self.pending >= 4:
            invocation.return_dbus_error(BUS_NAME + '.Busy', '后台忙，请稍后再试')
            return
        self.pending += 1
        if method == 'GetStatus':
            self.touch.heartbeat(sender)
            self.submit(self.status, invocation)
            return
        if method == 'ReleasePenTouch':
            self.submit(self.release_touch, invocation, sender)
            return
        if method != 'Apply':
            self.pending -= 1
            invocation.return_dbus_error(BUS_NAME + '.Error', '未知方法')
            return
        try:
            encoded = parameters.unpack()[0]
            if len(encoded) > 1024:
                raise ValueError('请求过长')
            request = validate_request(json.loads(encoded))
        except (ValueError, TypeError) as error:
            self.pending -= 1
            invocation.return_dbus_error(BUS_NAME + '.Error', str(error))
            return
        subject = ('system-bus-name', {'name': GLib.Variant('s', sender)})
        authorization = GLib.Variant('((sa{sv})sa{ss}us)', (subject, POLKIT_ACTION, {}, 1, ''))

        def authorized(bus, response, user_data):
            try:
                allowed, challenge, details = bus.call_finish(response).unpack()[0]
                if not allowed:
                    raise PermissionError('未获管理员授权，设置未修改')
            except Exception as error:
                self.pending -= 1
                invocation.return_dbus_error(BUS_NAME + '.NotAuthorized', str(error))
                return
            logging.info('authorized setting: sender=%s action=%s', sender, request['action'])
            if request['action'] in ('pen-touch-start', 'pen-slide-set'):
                self.submit(self.start_touch, invocation, request, sender)
            else:
                self.submit(self.apply, invocation, request)

        self.bus.call('org.freedesktop.PolicyKit1', '/org/freedesktop/PolicyKit1/Authority',
                      'org.freedesktop.PolicyKit1.Authority', 'CheckAuthorization', authorization,
                      None, Gio.DBusCallFlags.NONE, 120000, None, authorized, None)

    def monitor(self):
        if not self.monitoring:
            self.monitoring = True
            future = self.pool.submit(self.inputs.monitor_pen)
            future.add_done_callback(lambda completed: GLib.idle_add(self.monitored, completed))
        return True

    def monitored(self, future):
        self.monitoring = False
        future.result()
        return False

    def sleep_changed(self, connection, sender, path, interface, name, parameters, user_data):
        self.suspended = parameters.unpack()[0]
        if self.suspended:
            def disable():
                with self.operation_lock:
                    self.touch.stop()
                    self.inputs.close()
            self.pool.submit(disable)

    def run(self):
        loop = GLib.MainLoop()
        interface = Gio.DBusNodeInfo.new_for_xml(INTERFACE_XML).interfaces[0]
        self.bus.register_object(OBJECT_PATH, interface, self.method, None, None)
        owner = Gio.bus_own_name_on_connection(self.bus, BUS_NAME, Gio.BusNameOwnerFlags.NONE,
                                               None, lambda *arguments: loop.quit())
        subscription = self.bus.signal_subscribe('org.freedesktop.login1', 'org.freedesktop.login1.Manager',
                                                 'PrepareForSleep', '/org/freedesktop/login1', None,
                                                 Gio.DBusSignalFlags.NONE, self.sleep_changed, None)
        timer = GLib.timeout_add_seconds(3, self.monitor)
        signal.signal(signal.SIGTERM, lambda *arguments: loop.quit())
        signal.signal(signal.SIGINT, lambda *arguments: loop.quit())
        try:
            loop.run()
        finally:
            self.stopping = True
            GLib.source_remove(timer)
            self.bus.signal_unsubscribe(subscription)
            Gio.bus_unown_name(owner)
            try:
                with self.operation_lock:
                    self.touch.stop()
                    self.inputs.close()
            finally:
                self.pool.shutdown(wait=True, cancel_futures=True)


def main():
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    Service().run()
