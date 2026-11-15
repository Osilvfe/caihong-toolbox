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
    'PD_SPR_AVS': 'USB PD AVS',
    'PD_PPS_SPR_AVS': 'USB PD PPS / AVS',
    'APPLE_BRICK_ID': 'Apple Brick ID',
    'BRICKID': 'Apple Brick ID',
}


def active_value(value):
    match = re.search(r'\[([^]]+)\]', value or '')
    if match:
        return match[1].strip()
    values = (value or '').split()
    return values[0] if len(values) == 1 else None


def parse_fastchg_status(value):
    fields = dict(item.split('=', 1) for item in (value or '').split() if '=' in item)
    if fields.get('owner') not in ('idle', 'pps', 'vooc', 'pd'):
        return {}
    result = {'owner': fields['owner']}
    try:
        result['generation'] = int(fields['generation'])
        if not 0 <= result['generation'] <= 0xffffffff:
            return {}
        for name in ('fault', 'input_known', 'input_present'):
            result[name] = int(fields[name])
            if result[name] not in (0, 1):
                return {}
        for name in ('vooc_pending', 'pd_svooc', 'pps_cap_known', 'pps_available'):
            if fields.get(name) in ('0', '1'):
                result[name] = int(fields[name])
    except (KeyError, ValueError):
        return {}
    return result


def parse_pps_capabilities(value):
    result = {'available': False, 'fixed': [], 'pps': []}
    for line in (value or '').splitlines():
        match = re.match(r'pdo\d+=0x([0-9a-fA-F]{1,8})(?:\s|$)', line)
        if not match:
            continue
        pdo = int(match[1], 16)
        if pdo >> 30 == 0:
            minimum = maximum = ((pdo >> 10) & 0x3ff) * 50
            current = (pdo & 0x3ff) * 10
            if not minimum or not current:
                continue
            result['fixed'].append({'min_mv': minimum, 'max_mv': maximum,
                                    'current_ma': current})
        elif pdo >> 28 == 12:
            entry = {'min_mv': ((pdo >> 8) & 0xff) * 100,
                     'max_mv': ((pdo >> 17) & 0xff) * 100,
                     'current_ma': (pdo & 0x7f) * 50}
            if not 0 < entry['min_mv'] <= entry['max_mv'] or not entry['current_ma']:
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


def input_connected(adapter):
    state = adapter.get('fastchg') or {}
    if state.get('input_known') == 1:
        return state['input_present'] == 1
    if adapter.get('online') == 1 or adapter.get('pumps_online', 0) > 0:
        return True
    if adapter.get('online') == 0:
        return False
    return None


def detect_protocol(adapter, pd, capabilities, pumps_online):
    def result(label, kind, evidence):
        return {'protocol': label, 'protocol_kind': kind, 'protocol_evidence': evidence}

    if pd.get('power_role') == 'source' and pd.get('partner_present'):
        return result('USB OTG · 对外供电', 'otg', 'Type-C 当前角色为供电方，不是给平板充电')
    connected = input_connected(dict(adapter, pumps_online=pumps_online))
    if connected is False:
        return result('未接入', 'disconnected', '当前没有物理输入；不沿用上一次充电器的能力')
    if connected is None:
        return result('接入状态未知', 'unknown', '缺少可用的输入状态，未推测协议')
    state = adapter.get('fastchg') or {}
    owner = state.get('owner')
    if state.get('fault') == 1:
        return result('快充故障 · 安全锁止', 'fault', '内核报告清理不安全，不能按电源能力显示快充已启动')
    if adapter.get('protocol_transition'):
        return result('充电状态切换中', 'pending', '采样期间内核协议状态发生变化，等待下一次一致读数')
    if owner in ('pps', 'vooc'):
        name = 'USB PD PPS' if owner == 'pps' else 'VOOC / SVOOC'
        suffix = '双泵' if pumps_online >= 2 else '单泵' if pumps_online == 1 else '协商 / 恢复中'
        return result(f'{name} · {suffix}', owner,
                      f'内核当前协议所有者为 {owner}；电源公布的 PDO/APDO 不决定活动协议')
    if owner == 'pd':
        return result('USB PD 固定电压', 'pd', '内核当前协议所有者为 pd，不因充电器支持 PPS 而改标')
    if state.get('vooc_pending') == 1:
        return result('快充协商中', 'pending', '内核正在判断 VOOC 优先路径，尚未确认任何快充已启动')
    if pumps_online:
        return result('电荷泵工作 · 协议待确认', 'unknown',
                      '缺少活动协议确认；双泵、高电压或 PPS 能力都不能单独证明协议')
    reported = (adapter.get('active_type') or '').upper()
    if reported == 'PD':
        return result('USB PD 固定电压', 'pd', '依据 USB 端口类型上报；不是根据电压或功率推断')
    if reported == 'PD_DRP':
        return result(USB_TYPES[reported], 'pd', '端口报告 PD 双角色，未确认具体活动档位')
    if reported in ('PD_PPS', 'PD_SPR_AVS', 'PD_PPS_SPR_AVS'):
        label = 'USB PD · PPS 可用，未启用' if owner == 'idle' and reported == 'PD_PPS' else \
            USB_TYPES[reported] + ' · 活动状态未确认'
        return result(label, 'pending', '端口类型或电源能力不等于已经进入可调电压快充')
    if reported in USB_TYPES:
        high_voltage = reported in ('DCP', 'APPLE_BRICK_ID', 'BRICKID') \
            and (adapter.get('voltage_now') or 0) >= 7000000
        suffix = ' · 高压协议待确认' if high_voltage else ''
        return result(USB_TYPES[reported] + suffix, 'usb',
                      '依据 USB 类型上报；不将高压 DCP 猜测为 QC、VOOC 或其他私有协议')
    if capabilities.get('available') or pd.get('operation_mode') == 'usb_power_delivery':
        return result('USB PD 能力已报告 · 活动协议待确认', 'unknown',
                      '读到了 PD 能力，但没有足够证据确认当前使用固定 PD、PPS 或私有快充')
    return result('已接入 · 协议未识别', 'unknown', '未获得可确认的活动协议，保留未知状态')


def classify_protocol(adapter, pd, capabilities, pumps_online):
    return detect_protocol(adapter, pd, capabilities, pumps_online)['protocol']


def path_power_uw(voltage_uv, current_ua):
    if not voltage_uv or not current_ua or voltage_uv <= 0 or current_ua <= 0:
        return None
    return voltage_uv * current_ua // 1000000
