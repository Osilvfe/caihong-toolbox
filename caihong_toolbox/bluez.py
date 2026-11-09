import json
import re

from .hardware import Unavailable


KEYBOARD_SERVICE = '5768d878-b436-11ee-a506-0242ac120002'
KEYBOARD_WRITE = '5768da9e-b436-11ee-a506-0242ac120002'
PEN_SERVICE = '18092dbc-2a69-11ec-8d3d-0242ac130003'
PEN_WRITE = '18093046-2a69-11ec-8d3d-0242ac130003'
TOOLS = {'pencil': 0, 'eraser': 1, 'ballpoint': 2, 'fountain': 3}


def touchpad_command(enabled):
    if type(enabled) is not bool:
        raise ValueError('开关值必须为布尔值')
    return bytes.fromhex('1301010015' if enabled else '1301010116')


def writing_command(tool, level, enabled):
    if tool not in TOOLS or type(level) is not int or not 1 <= level <= 5 or type(enabled) is not bool:
        raise ValueError('无效的书写反馈参数')
    return bytes((0x2c, 0x94, 0x02, TOOLS[tool], level if enabled else 0))


class Bluez:
    def __init__(self):
        from gi.repository import Gio, GLib
        self.Gio, self.GLib = Gio, GLib
        self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)

    def call(self, path, interface, method, parameters=None):
        try:
            return self.bus.call_sync('org.bluez', path, interface, method, parameters, None,
                                      self.Gio.DBusCallFlags.NO_AUTO_START, 3000, None).unpack()
        except self.GLib.Error as error:
            raise Unavailable('蓝牙接口不可用：' + error.message) from error

    def objects(self):
        return self.call('/', 'org.freedesktop.DBus.ObjectManager', 'GetManagedObjects')[0]

    def target(self, objects, address):
        found = [(path, interfaces['org.bluez.Device1']) for path, interfaces in objects.items()
                 if interfaces.get('org.bluez.Device1', {}).get('Address', '').upper() == address]
        if len(found) != 1 or not all(found[0][1].get(key) for key in ('Paired', 'Connected', 'ServicesResolved')):
            raise Unavailable('附件须已配对、连接并完成服务发现；工具箱不会主动配对或重连')
        return found[0]

    def characteristic(self, objects, device, service_uuid, characteristic_uuid, flags):
        services = {path for path, interfaces in objects.items()
                    if interfaces.get('org.bluez.GattService1', {}).get('Device') == device
                    and interfaces['org.bluez.GattService1'].get('UUID') == service_uuid}
        found = [path for path, interfaces in objects.items()
                 if interfaces.get('org.bluez.GattCharacteristic1', {}).get('Service') in services
                 and interfaces['org.bluez.GattCharacteristic1'].get('UUID') == characteristic_uuid
                 and set(flags).issubset(interfaces['org.bluez.GattCharacteristic1'].get('Flags', []))]
        if len(found) != 1:
            raise Unavailable('未找到唯一匹配的原厂特征，拒绝发送命令')
        return found[0]

    def keyboard_characteristic(self, address):
        objects = self.objects()
        target, properties = self.target(objects, address)
        if not re.fullmatch(r'(?:usb|bluetooth):v22D9p3869d[0-9A-Fa-f]{4}', properties.get('Modalias', '')):
            raise Unavailable('蓝牙键盘型号与已验证型号不匹配')
        return self.characteristic(objects, target, KEYBOARD_SERVICE, KEYBOARD_WRITE, ('write',))

    def pen(self, paths, objects=None):
        objects = self.objects() if objects is None else objects
        identity = paths.path('/var/lib/caihong-pen/paired.json')
        address = None
        if identity.exists():
            try:
                address = json.loads(identity.read_text())['address'].upper()
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
                raise Unavailable('已有笔身份记录无效，拒绝猜测附件') from error
        found = [(path, interfaces['org.bluez.Device1']) for path, interfaces in objects.items()
                 if interfaces.get('org.bluez.Device1', {}).get('Name') == 'OnePlus Pencil Pro'
                 and interfaces['org.bluez.Device1'].get('Paired')
                 and (address is None or interfaces['org.bluez.Device1'].get('Address', '').upper() == address)]
        if len(found) != 1:
            raise Unavailable('未找到唯一已配对的 OnePlus Pencil Pro')
        target, properties = self.target(objects, found[0][1]['Address'].upper())
        command = self.characteristic(objects, target, PEN_SERVICE, PEN_WRITE, ('write', 'notify'))
        revisions = [path for path, interfaces in objects.items()
                     if interfaces.get('org.bluez.GattCharacteristic1', {}).get('UUID') == '00002a26-0000-1000-8000-00805f9b34fb'
                     and interfaces['org.bluez.GattCharacteristic1'].get('Service', '').startswith(target + '/')
                     and 'read' in interfaces['org.bluez.GattCharacteristic1'].get('Flags', [])]
        if len(revisions) != 1:
            raise Unavailable('缺少可核对的笔固件版本')
        value = self.call(revisions[0], 'org.bluez.GattCharacteristic1', 'ReadValue',
                          self.GLib.Variant('(a{sv})', ({},)))[0]
        firmware = bytes(value).decode('ascii').strip('\x00')
        if not firmware.startswith('4D45.03.00.10 '):
            raise Unavailable('笔固件版本未经验证，暂不发送厂商命令')
        return properties['Address'].upper(), command

    def write(self, characteristic, payload):
        parameters = self.GLib.Variant('(aya{sv})', (payload, {'type': self.GLib.Variant('s', 'request')}))
        self.call(characteristic, 'org.bluez.GattCharacteristic1', 'WriteValue', parameters)
