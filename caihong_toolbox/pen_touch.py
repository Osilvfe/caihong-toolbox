from collections import deque
from datetime import datetime, timezone
import json
import re
import threading
import time
import uuid

from .hardware import Unavailable
from .pen_slide import PenHidObserver


GESTURE_SERVICE = '18093398-2a69-11ec-8d3d-0242ac130003'
BATTERY_UUID = '180937bc-2a69-11ec-8d3d-0242ac130003'
DOUBLE_UUID = '180938ac-2a69-11ec-8d3d-0242ac130003'
DEBUG_SERVICE = '6e400001-b5a3-f393-e0a9-e50e24dcca9e'
DEBUG_UUID = '6e400003-b5a3-f393-e0a9-e50e24dcca9e'
CHARACTERISTIC = 'org.bluez.GattCharacteristic1'


def refresh_rate(paths):
    rates = set()
    nodes = {node.resolve() for node in paths.path('/sys/kernel/debug/dri').glob('*/state')}
    for node in nodes:
        for block in re.split(r'(?m)(?=^crtc\[)', node.read_text()):
            if re.search(r'(?m)^\s*active=1\s*$', block):
                match = re.search(r'(?m)^\s*mode: "[^"]+": (\d+) ', block)
                if not match:
                    return None
                rates.add(int(match[1]))
    return rates.pop() if len(rates) == 1 else None


class PenTouchSession:
    def __init__(self, paths, bluez):
        self.paths, self.bluez = paths, bluez
        self.lock = threading.RLock()
        self.cancel = threading.Event()
        self.thread = None
        self.owner = None
        self.session = None
        self.events = deque(maxlen=256)
        self.sequence = 0
        self.count = 0
        self.state = '未启动'
        self.error = None
        self.ready = False
        self.fresh_reply = False
        self.last_heartbeat = 0
        self.started = 0
        self.pending_ack = None
        self.ack_count = 0
        self.last_ack_at = float('-inf')
        self.address, self.command = None, None
        self.hid_available = False
        self.slide_attempted = False
        self.slide_requested = None
        self.wheel_events = 0

    def record(self, kind, value, payload=b''):
        with self.lock:
            self.sequence += 1
            self.events.append({'id': self.sequence, 'kind': kind, 'value': value,
                                'hex': payload[:256].hex(),
                                'time': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
                                'elapsed': round(time.monotonic() - self.started, 3)})

    def observe(self, channel, payload):
        payload = bytes(payload)
        if channel == 'command':
            if payload == bytes.fromhex('2c0601054d45030010'):
                self.fresh_reply = True
            if payload in (bytes.fromhex('2c90010100'), bytes.fromhex('2c90010101')):
                received = time.monotonic()
                if not payload[-1] or self.pending_ack is None or self.pending_ack[0] != payload:
                    self.pending_ack = (payload, received, received + (0.2 if payload[-1] else 0))
        kind, value = channel, payload.hex()
        if channel == 'double' and payload == b'\x01' and self.ready:
            kind, value = 'double-tap', '收到专用双击通知'
            with self.lock:
                self.count += 1
        elif channel == 'debug':
            value = payload.decode('utf-8', errors='replace').strip()[:256]
            if re.search(r'\bfilm\s+[0-9a-fA-F]{2}\b', value):
                kind = 'film'
        self.record(kind, value, payload)

    def heartbeat(self, owner):
        with self.lock:
            if owner == self.owner:
                self.last_heartbeat = time.monotonic()

    def snapshot(self):
        with self.lock:
            return {'session': self.session, 'state': self.state, 'ready': self.ready,
                    'running': bool(self.thread and self.thread.is_alive()),
                    'count': self.count, 'error': self.error, 'events': list(self.events),
                    'hid_available': self.hid_available, 'slide_requested': self.slide_requested,
                    'wheel_events': self.wheel_events}

    def start(self, owner):
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise Unavailable('已有笔身观察窗口，请先停止')
            self.owner, self.session = owner, uuid.uuid4().hex
            self.events.clear()
            self.sequence, self.count, self.ack_count = 0, 0, 0
            self.error, self.pending_ack = None, None
            self.ready, self.fresh_reply = False, False
            self.address, self.command, self.slide_requested = None, None, None
            self.slide_attempted, self.hid_available, self.wheel_events = False, False, 0
            self.last_ack_at = float('-inf')
            self.started = self.last_heartbeat = time.monotonic()
            self.state = '初始化中，请稍候'
            self.cancel.clear()
            self.thread = threading.Thread(target=self.run, daemon=True, name='pen-touch')
            self.thread.start()
            return {'message': '正在初始化笔身观察；请等待“观察中”再操作', 'session': self.session}

    def stop(self, owner=None):
        with self.lock:
            if owner is not None and owner != self.owner:
                return
            self.cancel.set()
            thread = self.thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=24)
            if thread.is_alive():
                raise RuntimeError('笔通知仍在清理，请检查服务日志')

    def set_slide(self, owner, enabled):
        if type(enabled) is not bool:
            raise ValueError('滑动开关必须为布尔值')
        with self.lock:
            if owner != self.owner or not self.ready or self.cancel.is_set():
                raise Unavailable('请先在本窗口开始笔身观察，等待就绪')
            if not self.hid_available:
                raise Unavailable('本支笔的蓝牙 HID 接口不可用，未修改滑动开关')
            self.slide_attempted = True
            self.slide_requested = None
            payload = bytes((0x2c, 0x96, 0x01, int(enabled)))
            try:
                self.bluez.write(self.command, payload)
            except Exception as error:
                self.error = '滑动请求未确认：' + str(error)
                self.record('error', self.error)
                raise
            self.slide_requested, self.error = enabled, None
            self.record('slide-request', '已发送手动滑动开关请求', payload)
            return {'message': '滑动开关请求已发送，可自由操作并观察事件；此状态不是硬件回读'}

    def cleanup_slide(self):
        with self.lock:
            self.ready = False
            if not self.slide_attempted or self.slide_requested is False:
                return
            try:
                self.bluez.target(self.bluez.objects(), self.address)
                self.bluez.write(self.command, bytes.fromhex('2c960100'))
                self.slide_requested = False
                self.record('slide-cleanup', '退出时已请求关闭本窗口启用的滑动；不声称恢复未知的先前状态', bytes.fromhex('2c960100'))
            except Exception as error:
                self.error = (self.error + '；' if self.error else '') + '滑动关闭未确认：' + str(error)

    def run(self):
        from gi.repository import GLib, Gio
        context = GLib.MainContext.new()
        context.push_thread_default()
        acquired, subscription, observer, command, address = [], None, None, None, None
        try:
            identity = json.loads(self.paths.path('/var/lib/caihong-pen/paired.json').read_text())
            address, command = self.bluez.pen(self.paths)
            self.address, self.command = address, command
            if identity['address'].upper() != address:
                raise Unavailable('笔身份不一致，拒绝开始观察')
            objects = self.bluez.objects()
            device, properties = self.bluez.target(objects, address)
            try:
                observer = PenHidObserver(self.paths, address)
                self.hid_available = True
                self.record('info', '仅读取本支笔的蓝牙 Mouse / Keyboard，不抓取键盘附件或笔尖，不抢占输入')
            except (OSError, Unavailable) as error:
                self.record('info', 'HID 观察不可用，双击通知仍可观察：' + str(error))
            channels = {command: 'command'}
            for channel, characteristic_uuid in (('battery', BATTERY_UUID), ('double', DOUBLE_UUID)):
                channels[self.bluez.characteristic(objects, device, GESTURE_SERVICE,
                                                   characteristic_uuid, ('notify',))] = channel
            try:
                channels[self.bluez.characteristic(objects, device, DEBUG_SERVICE,
                                                   DEBUG_UUID, ('notify',))] = 'debug'
            except Unavailable:
                self.record('info', '无调试通知；仍可观察专用双击事件')

            def changed(bus, sender, path, interface, signal, parameters, user_data):
                try:
                    changed_interface, values, invalidated = parameters.unpack()
                    if path == device and changed_interface == 'org.bluez.Device1':
                        if values.get('Connected') is False or values.get('ServicesResolved') is False:
                            raise Unavailable('笔已断联，观察停止，不自动重连')
                    if path in channels and changed_interface == CHARACTERISTIC and 'Value' in values:
                        self.observe(channels[path], values['Value'])
                except Exception as error:
                    self.error = str(error)
                    self.cancel.set()

            subscription = self.bluez.bus.signal_subscribe(
                'org.bluez', 'org.freedesktop.DBus.Properties', 'PropertiesChanged',
                None, None, Gio.DBusSignalFlags.NONE, changed, None)
            for path in channels:
                if self.cancel.is_set():
                    return
                self.bluez.call(path, CHARACTERISTIC, 'StartNotify')
                acquired.append(path)
                self.record('subscribe', channels[path])
            self.bluez.write(command, bytes.fromhex('2c060105'))
            query_deadline = time.monotonic() + 5
            initialized, checked_at = False, 0
            while not self.cancel.is_set():
                for iteration in range(64):
                    if not context.pending():
                        break
                    context.iteration(False)
                if self.cancel.is_set():
                    break
                now = time.monotonic()
                if now - self.last_heartbeat > 10:
                    self.record('info', '窗口心跳中断，停止并清理')
                    break
                if now - checked_at >= 1:
                    self.bluez.target(self.bluez.objects(), address)
                    checked_at = now
                if not initialized:
                    if self.fresh_reply:
                        if self.cancel.is_set():
                            break
                        self.bluez.write(command, bytes.fromhex('2c390101'))
                        self.bluez.write(command, bytes.fromhex('2c950100'))
                        initialized = self.ready = True
                        self.state = '观察中'
                        self.record('ready', '新鲜固件回复已确认；真实离座及普通模式请求已发送')
                    elif now >= query_deadline:
                        raise Unavailable('未收到新鲜固件回复；未发送离座或普通模式配置')
                if initialized and observer:
                    for event in observer.read():
                        if event['type'] == 2:
                            self.wheel_events += 1
                        self.record('pen-hid', event)
                if self.pending_ack and now >= self.pending_ack[2]:
                    payload, received, due = self.pending_ack
                    self.pending_ack = None
                    try:
                        rate = refresh_rate(self.paths)
                    except OSError:
                        rate = None
                    allowed = not payload[-1] or (type(rate) is int and 30 <= rate <= 240 and rate not in (90, 144))
                    if now - received <= 1 and allowed and now - self.last_ack_at >= 0.2:
                        self.bluez.write(command, payload)
                        self.ack_count += 1
                        self.last_ack_at = now
                        self.record('status-ack', '应答真实状态通知', payload)
                time.sleep(0.025)
        except Exception as error:
            self.error = str(error)
            self.record('error', self.error)
        finally:
            self.cleanup_slide()
            if observer:
                observer.close()
            if subscription is not None:
                self.bluez.bus.signal_unsubscribe(subscription)
            for path in reversed(acquired):
                try:
                    self.bluez.call(path, CHARACTERISTIC, 'StopNotify')
                except Exception as error:
                    self.error = (self.error + '；' if self.error else '') + '通知清理：' + str(error)
            self.state = '已停止（存在错误）' if self.error else '已停止'
            context.pop_thread_default()
