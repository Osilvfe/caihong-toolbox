import fcntl
import os
import select
import struct
import threading
import time

from .bluez import Bluez, touchpad_command, writing_command
from .hardware import Unavailable, keyboard_nodes, number, pen_controller, pogo_node
from .keyboard import KeyboardClient


class KeyboardCompanion:
    def __init__(self, paths, bluez):
        self.paths, self.bluez = paths, bluez
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.thread = None
        self.target = True
        self.error = None
        self.last_write_ms = None
        self.node = None
        self.characteristic = None

    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def send(self, enabled):
        started = time.monotonic()
        self.bluez.write(self.characteristic, touchpad_command(enabled))
        self.last_write_ms = round((time.monotonic() - started) * 1000, 2)

    def start(self):
        if self.running():
            return
        nodes = keyboard_nodes(self.paths)
        if len(nodes) != 1:
            raise Unavailable('没有唯一匹配的蓝牙键盘与内核补偿接口')
        pogo = pogo_node(self.paths)
        if pogo and number(pogo / 'attached') == 1:
            raise Unavailable('键盘已磁吸连接，请使用现有 Pogo 路径')
        self.node = nodes[0]
        gate = self.node['controls'] / 'f4_compensation'
        if number(gate) != 0:
            raise Unavailable('补偿门控已被其他程序启用，拒绝抢占')
        self.characteristic = self.bluez.keyboard_characteristic(self.node['address'])
        keyboard = self.node['event'].open('rb', buffering=0)
        try:
            fcntl.flock(keyboard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.lock:
                self.send(True)
                self.target = True
                gate.write_text('1\n')
                self.error = None
                self.stopped.clear()
                self.thread = threading.Thread(target=self.run, args=(keyboard,), daemon=True)
                self.thread.start()
        except BaseException:
            keyboard.close()
            if number(gate) == 1:
                gate.write_text('0\n')
            raise

    def set_target(self, enabled):
        with self.lock:
            self.send(enabled)
            self.target = enabled

    def run(self, keyboard):
        packet = struct.Struct('llHHi')
        scan = None
        try:
            if packet.size != 24:
                raise Unavailable('不支持此输入事件 ABI')
            while not self.stopped.is_set():
                pogo = pogo_node(self.paths)
                if pogo and number(pogo / 'attached') == 1:
                    break
                readable, _, _ = select.select([keyboard], [], [], 0.2)
                if not readable:
                    continue
                payload = os.read(keyboard.fileno(), packet.size * 64)
                if not payload or len(payload) % packet.size:
                    raise Unavailable('蓝牙键盘已断开或输入记录不完整')
                for seconds, microseconds, kind, code, value in packet.iter_unpack(payload):
                    if kind == 0:
                        if code == 3:
                            raise Unavailable('输入事件丢失，补偿已停止，请重新开启')
                        if code == 0:
                            scan = None
                    elif kind == 4 and code == 4:
                        scan = value
                    elif kind == 1 and value == 1 and scan in (0x7006b, 0x7006c) and code in (62, 530):
                        with self.lock:
                            target = not self.target if code == 530 else self.target
                            self.send(target)
                            self.target = target
        except (OSError, RuntimeError) as error:
            self.error = str(error)
        finally:
            try:
                (self.node['controls'] / 'f4_compensation').write_text('0\n')
            except OSError:
                pass
            try:
                with self.lock:
                    self.send(True)
                    self.target = True
            except (OSError, RuntimeError) as error:
                self.error = str(error)
            keyboard.close()

    def stop(self):
        self.stopped.set()
        if self.thread:
            self.thread.join(timeout=7)
            if self.thread.is_alive():
                raise RuntimeError('补偿尚未退出，请检查后台日志')


class InputManager:
    def __init__(self, paths, bluez=None):
        self.paths = paths
        self.bluez = bluez or Bluez()
        self.keyboard = KeyboardClient()
        self.pen_lock = threading.Lock()
        self.pen_active = None
        self.pen_error = None
        self.feedback_at = float('-inf')

    def keyboard_apply(self, request):
        self.keyboard.apply(request)

    def pen_apply(self, request):
        with self.pen_lock:
            if request['action'] == 'pen-feedback':
                if time.monotonic() - self.feedback_at < 2:
                    raise ValueError('短振操作过于频繁，请间隔至少两秒')
                address, characteristic = self.bluez.pen(self.paths)
                self.feedback_at = time.monotonic()
                self.bluez.write(characteristic, bytes.fromhex('2c920102ff02'))
                return
            controller = pen_controller(self.paths)
            enabled = request['enabled']
            tool, level = request['tool'], request['level']
            self.pen_error = None
            if not enabled:
                self.disable_pen(controller)
                return
            if number(controller / 'pen_scan') != 1:
                raise Unavailable('笔扫描模式与已验证基准不同；工具箱不会修改扫描模式')
            address, characteristic = self.bluez.pen(self.paths)
            previous = self.pen_active
            if previous:
                self.disable_pen(controller)
            try:
                self.bluez.write(characteristic, writing_command(tool, level, True))
                (controller / 'pen_vibrator').write_text('1\n')
                if number(controller / 'pen_vibrator') != 1:
                    raise RuntimeError('触控器未确认书写反馈开启请求')
                self.pen_active = {'address': address, 'tool': tool, 'level': level}
            except BaseException:
                self.pen_active = {'address': address, 'tool': tool, 'level': level}
                self.disable_pen(controller)
                raise

    def disable_pen(self, controller=None):
        errors = []
        active = self.pen_active
        try:
            controller = controller or pen_controller(self.paths)
            (controller / 'pen_vibrator').write_text('0\n')
            if number(controller / 'pen_vibrator') != 0:
                raise RuntimeError('触控器未确认关闭请求')
        except (OSError, RuntimeError) as error:
            errors.append(str(error))
        try:
            address, characteristic = self.bluez.pen(self.paths)
            if active and address != active['address']:
                raise Unavailable('笔身份已变化，未向另一支笔发送关闭命令')
            self.bluez.write(characteristic, writing_command(
                active['tool'] if active else 'pencil', active['level'] if active else 3, False))
        except (OSError, RuntimeError) as error:
            errors.append(str(error))
        self.pen_active = None
        self.pen_error = '；'.join(errors) or None
        if errors:
            raise RuntimeError('关闭未完全确认：' + self.pen_error)

    def monitor_pen(self):
        if not self.pen_active or not self.pen_lock.acquire(blocking=False):
            return
        try:
            if not self.pen_active:
                return
            objects = self.bluez.objects()
            connected = any(interfaces.get('org.bluez.Device1', {}).get('Connected')
                            and interfaces['org.bluez.Device1'].get('Address', '').upper() == self.pen_active['address']
                            for interfaces in objects.values())
            controller = pen_controller(self.paths)
            if not connected or number(controller / 'pen_vibrator') != 1 or number(controller / 'pen_scan') != 1:
                self.disable_pen(controller)
        except (OSError, RuntimeError) as error:
            reason = str(error)
            try:
                self.disable_pen()
            except (OSError, RuntimeError) as cleanup_error:
                reason += '；' + str(cleanup_error)
            self.pen_error = reason
        finally:
            self.pen_lock.release()

    def close(self):
        with self.pen_lock:
            if self.pen_active:
                self.disable_pen()
