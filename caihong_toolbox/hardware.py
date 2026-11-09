import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import time

from .power import (active_value, capability_summary, classify_protocol,
                    parse_pps_capabilities, path_power_uw, pps_current_limit)
from .resources import ResourceSampler
from .services import validate_managed_service


class Unavailable(RuntimeError):
    pass


class Paths:
    def __init__(self, root=Path('/')):
        self.root = root
        self.battery = self.path('/sys/class/power_supply/qcom-battmgr-bat')
        self.pps_cached = None
        self.pps_cached_at = 0.0
        self.resources = ResourceSampler(self.root)

    def path(self, name):
        return self.root / name.lstrip('/')


def text(path):
    try:
        with path.open() as stream:
            return stream.read(16384).replace('\x00', ' ').strip()
    except (OSError, UnicodeError):
        return None


def number(path):
    try:
        return int(text(path))
    except (TypeError, ValueError):
        return None


def fields(value):
    return dict(item.split('=', 1) for item in (value or '').split() if '=' in item)


def is_caihong(paths):
    try:
        return b'oneplus,caihong' in paths.path('/proc/device-tree/compatible').read_bytes().split(b'\x00')
    except OSError:
        return False


def keyboard_nodes(paths):
    found = []
    for node in paths.path('/sys/class/input').glob('event*'):
        device = node / 'device'
        if (text(device / 'id/bustype'), text(device / 'id/vendor'), text(device / 'id/product')) != ('0005', '22d9', '3869'):
            continue
        name = text(device / 'name') or ''
        if 'Touchpad' in name or 'Consumer Control' in name:
            continue
        address = (text(device / 'uniq') or '').upper()
        if not re.fullmatch(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}', address):
            continue
        resolved = device.resolve()
        controls = [parent / 'oneplus' for parent in (resolved, *resolved.parents)
                    if parent.name.startswith('0005:22D9:3869.')
                    and (parent / 'oneplus/f4_compensation').is_file()]
        if len(controls) == 1:
            found.append({'event': paths.path('/dev/input') / node.name,
                          'controls': controls[0], 'address': address})
    return found


def pogo_node(paths):
    matches = [node for node in paths.path('/sys/bus/serial/devices').glob('*')
               if fields(text(node / 'uevent')).get('DRIVER') == 'oneplus-pogo'
               and (node / 'attached').is_file()]
    return matches[0] if len(matches) == 1 else None


def pen_controller(paths):
    matches = [node for node in paths.path('/sys/bus/spi/devices').glob('spi*.*')
               if (node / 'driver').resolve().name == 'nt36532e'
               and (node / 'pen_vibrator').is_file()]
    if len(matches) != 1:
        raise Unavailable('当前内核没有唯一匹配的笔反馈接口')
    return matches[0]


_default_paths = None


def read_snapshot(paths=None):
    global _default_paths
    if paths is None:
        if _default_paths is None:
            _default_paths = Paths()
        paths = _default_paths
    battery = {name: number(paths.battery / name) for name in (
        'capacity', 'temp', 'current_now', 'voltage_now', 'power_now', 'state_of_health',
        'cycle_count', 'charge_full', 'charge_full_design',
        'charge_control_start_threshold', 'charge_control_end_threshold')}
    battery['status'] = text(paths.battery / 'status')
    behaviour = text(paths.battery / 'charge_behaviour')
    match = re.search(r'\[([^]]+)\]', behaviour or '')
    battery['charge_behaviour'] = match[1] if match else behaviour
    controller = text(paths.battery / 'device/oneplus_charge_control_status')
    battery['controller'] = fields(controller)
    start, end = battery['charge_control_start_threshold'], battery['charge_control_end_threshold']
    battery['control_available'] = bool(is_caihong(paths) and controller is not None
                                        and behaviour is not None and start is not None
                                        and end is not None and 0 <= start < end <= 100)
    full, design = battery['charge_full'], battery['charge_full_design']
    battery['capacity_ratio'] = round(full * 100 / design, 1) if full and design and design > 0 else None
    battery['power_uw'] = battery['power_now']
    if battery['power_uw'] is None:
        battery['power_uw'] = path_power_uw(battery['voltage_now'], battery['current_now'])
    usb = paths.path('/sys/class/power_supply/qcom-battmgr-usb')
    adapter = {name: number(usb / name) for name in (
        'online', 'voltage_now', 'current_now', 'voltage_max', 'current_max',
        'input_current_limit')}
    adapter['usb_type'] = text(usb / 'usb_type')
    adapter['active_type'] = active_value(adapter['usb_type'])
    adapter['buck_power_uw'] = path_power_uw(adapter['voltage_now'], adapter['current_now'])
    pumps = []
    for pump in sorted(paths.path('/sys/class/power_supply').glob('sc8547-*')):
        entry = {name: number(pump / name) for name in (
            'online', 'voltage_now', 'current_now', 'input_voltage_limit')}
        entry['name'] = pump.name
        entry['power_uw'] = path_power_uw(entry['voltage_now'], entry['current_now'])
        pumps.append(entry)
    online_pumps = [pump for pump in pumps if pump['online'] == 1]
    adapter['pumps'] = pumps
    adapter['pumps_online'] = len(online_pumps)
    adapter['pump_power_uw'] = (sum(pump['power_uw'] or 0 for pump in online_pumps)
                                if online_pumps else None)
    power_paths = ([adapter['buck_power_uw']] if adapter['buck_power_uw'] else []
                   if adapter['online'] == 1 else [])
    power_paths.extend(pump['power_uw'] for pump in online_pumps if pump['power_uw'])
    adapter['input_power_uw'] = sum(power_paths) if power_paths else None
    pd_path = paths.path('/sys/class/usb_power_delivery/pd0')
    adapter['pd'] = {
        'supported': text(pd_path / 'device/supports_usb_power_delivery') == 'yes',
        'revision': text(pd_path / 'revision'),
        'power_role': active_value(text(pd_path / 'device/device/power_role')),
        'data_role': active_value(text(pd_path / 'device/device/data_role')),
        'orientation': text(pd_path / 'device/device/orientation'),
        'operation_mode': text(pd_path / 'device/device/power_operation_mode'),
    }
    if adapter['online'] == 1:
        now = time.monotonic()
        if paths.pps_cached is None or now - paths.pps_cached_at >= 15:
            paths.pps_cached = parse_pps_capabilities(text(usb / 'device/oneplus_pps_capabilities'))
            paths.pps_cached_at = now
    else:
        paths.pps_cached = None
        paths.pps_cached_at = 0.0
    adapter['capabilities'] = paths.pps_cached or parse_pps_capabilities(None)
    adapter['capability_summary'] = capability_summary(adapter['capabilities'])
    adapter['pps_current_limit_ma'] = pps_current_limit(adapter['capabilities'])
    adapter['protocol'] = classify_protocol(adapter, adapter['pd'], adapter['capabilities'],
                                             adapter['pumps_online'])
    keyboard = {'available': False, 'media_keys': None, 'compensation_gate': None}
    nodes = keyboard_nodes(paths)
    if len(nodes) == 1:
        keyboard.update(available=True, address=nodes[0]['address'],
                        media_keys=number(nodes[0]['controls'] / 'media_keys'),
                        compensation_gate=number(nodes[0]['controls'] / 'f4_compensation'))
    pogo = pogo_node(paths)
    keyboard['pogo_attached'] = number(pogo / 'attached') if pogo else None
    keyboard['pogo_touchpad'] = number(pogo / 'touchpad_enabled') if pogo else None
    pen = {'controller_available': False, 'scan_mode': None, 'vibrator_request': None}
    try:
        node = pen_controller(paths)
        pen.update(controller_available=True, scan_mode=number(node / 'pen_scan'),
                   vibrator_request=number(node / 'pen_vibrator'))
    except Unavailable:
        pass
    pen['hardware_support'] = any(paths.path('/proc/device-tree').glob(
        'soc@0/*/*/touchscreen@0/novatek,pen-support'))
    pen['paired'] = paths.path('/var/lib/caihong-pen/paired.json').is_file()
    keyboard['hardware_support'] = pogo is not None or bool(nodes)
    return {
        'device': {'supported': is_caihong(paths), 'model': text(paths.path('/proc/device-tree/model')),
                   'kernel': platform.release(), 'architecture': platform.machine()},
        'battery': battery, 'adapter': adapter, 'keyboard': keyboard, 'pen': pen,
        'resources': paths.resources.read(),
        'diagnostics': {name: text(paths.battery / ('device/' + name)) for name in (
            'oneplus_charge_control_status', 'oneplus_power_diagnostics', 'oneplus_ffc_status')},
    }


def validate_request(request):
    if not isinstance(request, dict) or not isinstance(request.get('action'), str):
        raise ValueError('无效的设置请求')
    schemas = {
        'charge-bypass': {'enabled'}, 'charge-limit': {'start', 'end', 'experimental'},
        'charge-reset': set(), 'keyboard-media': {'enabled'},
        'keyboard-touchpad': {'enabled'},
        'pen-writing': {'enabled', 'tool', 'level'}, 'pen-feedback': set(),
        'pen-touch-start': {'undocked'}, 'pen-slide-set': {'enabled'},
        'service-set': {'name', 'enabled'},
    }
    action = request['action']
    if action not in schemas or set(request) != schemas[action] | {'action'}:
        raise ValueError('设置项或参数不在白名单内')
    if 'enabled' in request and type(request['enabled']) is not bool:
        raise ValueError('开关值必须为布尔值')
    if action == 'charge-limit':
        start, end = request['start'], request['end']
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= 100:
            raise ValueError('必须满足 0 ≤ 恢复阈值 < 停止阈值 ≤ 100')
        if request['experimental'] is not True:
            raise ValueError('充电阈值尚待完整实测，需要明确确认实验风险')
    if action == 'pen-writing':
        if request['tool'] not in ('pencil', 'eraser', 'ballpoint', 'fountain'):
            raise ValueError('不支持的笔工具')
        if type(request['level']) is not int or not 1 <= request['level'] <= 5:
            raise ValueError('书写反馈强度必须为 1–5 的整数')
    if action == 'pen-touch-start' and request['undocked'] is not True:
        raise ValueError('请确认笔确实已离开磁吸位置')
    if action == 'service-set':
        validate_managed_service(request['name'])
    return request


def apply_charge(paths, request):
    if not read_snapshot(paths)['battery']['control_available']:
        raise Unavailable('缺少 Caihong 专用充电接口，拒绝写入旧阈值映射')
    program = paths.path('/usr/local/sbin/caihong-charge')
    metadata = program.stat()
    if metadata.st_uid != 0 or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH) or not os.access(program, os.X_OK):
        raise Unavailable('充电控制程序必须由 root 持有且不可被普通用户改写')
    action = request['action']
    arguments = {'charge-bypass': ['bypass', 'on' if request.get('enabled') else 'off'],
                 'charge-limit': ['limit', str(request.get('start')), str(request.get('end'))],
                 'charge-reset': ['reset']}[action]
    result = subprocess.run([str(program), *arguments], capture_output=True, text=True,
                            timeout=12, check=False, env={'PATH': '/usr/bin:/usr/sbin', 'LANG': 'C.UTF-8'})
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or '充电请求失败，可能已部分应用，请检查控制器状态')
    return json.loads(result.stdout)
