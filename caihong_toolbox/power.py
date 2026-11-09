import re


USB_TYPES = {
    'SDP': 'USB SDP 标准端口',
    'DCP': 'USB DCP 专用充电',
    'CDP': 'USB CDP 充电下行端口',
    'ACA': 'USB ACA 配件充电',
    'C': 'USB Type-C',
    'PD': 'USB PD',
    'PD_DRP': 'USB PD 双角色端口',
    'PD_PPS': 'USB PD PPS',
    'APPLE_BRICK_ID': 'Apple Brick ID',
    'BRICKID': 'Apple Brick ID',
}


def active_value(value):
    match = re.search(r'\[([^]]+)\]', value or '')
    return match[1].strip() if match else None


def parse_pps_capabilities(value):
    result = {'available': False, 'fixed': [], 'pps': []}
    for line in (value or '').splitlines():
        match = re.match(r'pdo\d+=0x([0-9a-fA-F]+)(.*)', line)
        if not match:
            continue
        pdo = int(match[1], 16)
        details = match[2]
        fixed = re.search(r'fixed=(\d+)mV (\d+)mA', details)
        pps = re.search(r'pps=(\d+)-(\d+)mV (\d+)mA', details)
        if fixed:
            minimum = maximum = int(fixed[1])
            current = int(fixed[2])
            if not minimum or not current:
                continue
            result['fixed'].append({'min_mv': minimum, 'max_mv': maximum,
                                    'current_ma': current})
        elif pps:
            entry = {'min_mv': int(pps[1]), 'max_mv': int(pps[2]),
                     'current_ma': int(pps[3])}
            if not entry['min_mv'] or not entry['max_mv'] or not entry['current_ma']:
                continue
            result['pps'].append(entry)
        else:
            continue
    result['available'] = bool(result['fixed'] or result['pps'])
    return result


def capability_summary(capabilities):
    pps, fixed = capabilities.get('pps', []), capabilities.get('fixed', [])
    ranges = [f'{entry["min_mv"] / 1000:.1f}–{entry["max_mv"] / 1000:.1f} V / '
              f'{entry["current_ma"] / 1000:.1f} A' for entry in pps]
    if fixed:
        peak = max(fixed, key=lambda entry: entry['max_mv'] * entry['current_ma'])
        fixed_text = (f'固定最高 {peak["max_mv"] / 1000:.1f} V / '
                      f'{peak["current_ma"] / 1000:.1f} A')
    else:
        fixed_text = None
    parts = []
    if ranges:
        parts.append('PPS ' + '；PPS '.join(ranges))
    if fixed_text:
        parts.append(fixed_text)
    return '；'.join(parts) if parts else None


def pps_current_limit(capabilities, voltage_mv=8500):
    limits = [entry['current_ma'] for entry in capabilities.get('pps', [])
              if entry['min_mv'] <= voltage_mv <= entry['max_mv']]
    return max(limits) if limits else None


def classify_protocol(adapter, pd, capabilities, pumps_online):
    if adapter.get('online') != 1:
        return '未接入'
    voltage = adapter.get('voltage_now') or 0
    has_pps = bool(capabilities.get('pps'))
    has_fixed = bool(capabilities.get('fixed'))
    high_voltage = voltage >= 7000000
    if pumps_online >= 2 and high_voltage and has_pps:
        return 'USB PD PPS · 双泵'
    if pumps_online >= 2 and high_voltage and not has_pps:
        return 'VOOC / SVOOC · 双泵'
    if pumps_online >= 1 and high_voltage and not has_pps:
        return 'VOOC / SVOOC · 单泵'
    if pumps_online >= 2:
        return '双泵充电 · 低压待识别'
    if has_pps:
        return 'USB PD PPS · 待启动'
    if has_fixed and high_voltage:
        return 'USB PD 固定电压'
    reported = adapter.get('active_type')
    if reported in USB_TYPES:
        return USB_TYPES[reported]
    if pd.get('supported'):
        return 'USB PD · 能力已报告'
    return '已接入 · 协议未识别'


def path_power_uw(voltage_uv, current_ua):
    if not voltage_uv or not current_ua or voltage_uv <= 0 or current_ua <= 0:
        return None
    return voltage_uv * current_ua // 1000000
