import re
import subprocess


UNIT_PATTERN = re.compile(r'^caihong-[a-z0-9-]+\.(?:service|timer)$')
PROTECTED = {'caihong-toolbox.service', 'caihong-mark-boot-successful.service'}

DETAILS = {
    'caihong-adbd.service': ('ADB 调试服务', '按设备模式启动 ADB 守护进程；关闭后仍可在终端手动运行 adb。', None),
    'caihong-bluetooth-autostart.service': ('蓝牙控制器自启', '开机为 WCN7850 蓝牙控制器上电。', None),
    'caihong-charge.service': ('充电偏好恢复', '开机恢复 Caihong 充电偏好。', None),
    'caihong-clean-pogo-kde.service': ('Pogo 桌面清理', '移除 KDE 中不适用于 Pogo 键盘的背光动作。', 'keyboard'),
    'caihong-fix-kwin-brightness.service': ('KWin 亮度曲线', '写入原厂派生的自动亮度曲线。', None),
    'caihong-grow-rootfs.service': ('扩展根分区', '首次启动把根文件系统扩展到分区容量。', None),
    'caihong-hardware-mac.service': ('硬件地址恢复', '为 WLAN 和蓝牙加载设备专属硬件地址。', None),
    'caihong-hotspot.service': ('并发热点', '启用 Caihong 并发 Wi-Fi 热点。', None),
    'caihong-keyboard.service': ('键盘自动兼容', '管理蓝牙键盘的 F4 补丁和触控板偏好。', 'keyboard'),
    'caihong-mark-boot-successful.service': ('标记启动成功', 'A/B 启动槽成功标记；关闭可能破坏回退保护。', None),
    'caihong-pen-autoconnect.timer': ('手写笔自动重连', '按记录恢复已配对笔的连接和扫描模式。', 'pen'),
    'caihong-pen-charge.timer': ('手写笔自动充电', '周期性尝试有界的笔充电。', 'pen'),
    'caihong-pen-pair.timer': ('手写笔自动配对', '重试最近磁吸过的笔配对。', 'pen'),
    'caihong-ssc.service': ('传感器子系统', '运行 Qualcomm Sensor Subsystem 监听器。', None),
    'caihong-toolbox.service': ('工具箱后台', '提供只读状态和经 Polkit 授权的设置接口。', None),
}


def validate_service_name(name):
    if not isinstance(name, str) or not UNIT_PATTERN.fullmatch(name):
        raise ValueError('只允许管理 caihong-*.service 或 caihong-*.timer')
    return name


def validate_managed_service(name):
    validate_service_name(name)
    if name in PROTECTED:
        raise ValueError('该服务受保护，不能在工具箱中停用')
    return name


def _run(arguments, timeout=4):
    return subprocess.run(arguments, capture_output=True, text=True, timeout=timeout,
                          check=False)


def _blocks(payload):
    block = {}
    for line in payload.splitlines() + ['']:
        if not line:
            if block:
                yield block
                block = {}
            continue
        key, separator, value = line.partition('=')
        if separator:
            block[key] = value


def _unit_files():
    try:
        result = _run(['systemctl', 'list-unit-files', '--type=service', '--type=timer',
                       '--no-legend', '--no-pager', 'caihong-*.service', 'caihong-*.timer'])
    except (OSError, subprocess.SubprocessError):
        return {}
    if result.returncode:
        return {}
    units = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2 and UNIT_PATTERN.fullmatch(fields[0]):
            units[fields[0]] = fields[1]
    return units


def _service_details(unit, description, unit_file_state, active, sub):
    details = DETAILS.get(unit)
    if details:
        title, text, requires = details
    else:
        title = description if description and description != unit else unit
        text = 'Caihong 系统服务。'
        requires = 'pen' if 'pen' in unit else 'keyboard' if 'keyboard' in unit else None
    protected = unit in PROTECTED
    manageable = (not protected and unit_file_state in
                  ('enabled', 'disabled', 'enabled-runtime', 'disabled-runtime'))
    return {
        'unit': unit, 'title': title, 'description': text, 'requires': requires,
        'protected': protected, 'manageable': manageable,
        'enabled': unit_file_state in ('enabled', 'enabled-runtime'),
        'active': active == 'active', 'state': active + ('/' + sub if sub else ''),
        'unit_file_state': unit_file_state,
    }


def read_services():
    unit_files = _unit_files()
    timer_services = {unit[:-6] + '.service' for unit in unit_files if unit.endswith('.timer')}
    units = [unit for unit in unit_files if unit not in timer_services]
    if not units:
        return {'items': [], 'error': '没有发现 caihong 服务或计时器'}
    arguments = ['systemctl', 'show', '--no-pager', '-p',
                 'Id,Description,LoadState,UnitFileState,ActiveState,SubState', '--']
    arguments.extend(units)
    try:
        result = _run(arguments)
    except (OSError, subprocess.SubprocessError) as error:
        return {'items': [], 'error': '无法读取 systemd 状态：' + str(error)}
    if result.returncode:
        return {'items': [], 'error': result.stderr.strip() or 'systemctl show 失败'}
    parsed = {block.get('Id'): block for block in _blocks(result.stdout) if block.get('Id')}
    items = []
    for unit in units:
        block = parsed.get(unit, {})
        load = block.get('LoadState', 'not-found')
        state = block.get('UnitFileState', unit_files.get(unit, ''))
        if load != 'loaded' or state in ('masked', 'masked-runtime'):
            continue
        item = _service_details(unit, block.get('Description', unit), state,
                                block.get('ActiveState', 'unknown'),
                                block.get('SubState', ''))
        item['load_state'] = load
        items.append(item)
    return {'items': sorted(items, key=lambda item: item['title']), 'error': None}


def set_service(name, enabled):
    validate_managed_service(name)
    if type(enabled) is not bool:
        raise ValueError('服务开关值必须为布尔值')
    action = 'enable' if enabled else 'disable'
    try:
        result = _run(['systemctl', action, '--now', name], timeout=20)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError('systemctl ' + action + ' 失败：' + str(error)) from error
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or 'systemctl 操作失败')
    return {'message': ('已启用并运行 ' if enabled else '已停用并停止 ') + name}
