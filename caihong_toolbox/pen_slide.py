import os
import stat
import struct

from .hardware import Unavailable, text


PACKET = struct.Struct('llHHi')
WHEEL_CODES = (6, 8, 11, 12)


def pen_hid_nodes(paths, address):
    nodes = []
    for node in sorted(paths.path('/sys/class/input').glob('event*')):
        device = node / 'device'
        name = text(device / 'name')
        if ((text(device / 'uniq') or '').upper() == address
                and text(device / 'id/bustype') == '0005'
                and name in ('OnePlus Pencil Pro Mouse', 'OnePlus Pencil Pro Keyboard')):
            nodes.append((paths.path('/dev/input') / node.name, name))
    if sum(name == 'OnePlus Pencil Pro Mouse' for node, name in nodes) != 1:
        raise Unavailable('缺少唯一匹配的本支笔蓝牙 Mouse 节点，未开启滑动')
    return nodes


def decode_events(payload, node):
    if PACKET.size != 24 or len(payload) % PACKET.size:
        raise Unavailable('笔输入事件 ABI 或记录长度不受支持')
    events = []
    for seconds, microseconds, kind, code, value in PACKET.iter_unpack(payload):
        if kind == 0 and code == 3:
            raise Unavailable('笔输入事件丢失，观察停止，请重新开始')
        if kind == 1 or (kind == 2 and code in WHEEL_CODES) or (kind == 4 and code == 4):
            events.append({'node': node, 'type': kind, 'code': code, 'value': value,
                           'kernel_seconds': seconds, 'kernel_microseconds': microseconds})
    return events


class PenHidObserver:
    def __init__(self, paths, address):
        self.descriptors = {}
        try:
            for node, name in pen_hid_nodes(paths, address):
                descriptor = os.open(node, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW)
                self.descriptors[descriptor] = node.name
                if not stat.S_ISCHR(os.fstat(descriptor).st_mode):
                    raise Unavailable('笔输入路径不是字符设备')
        except BaseException:
            self.close()
            raise

    def read(self):
        events = []
        for descriptor, node in self.descriptors.items():
            try:
                payload = os.read(descriptor, PACKET.size * 64)
            except BlockingIOError:
                continue
            if not payload:
                raise Unavailable('笔 HID 节点已移除，观察停止')
            events.extend(decode_events(payload, node))
        return events

    def close(self):
        for descriptor in self.descriptors:
            os.close(descriptor)
        self.descriptors.clear()
