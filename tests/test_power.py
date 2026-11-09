from caihong_toolbox.power import (active_value, capability_summary,
                                   classify_protocol, parse_pps_capabilities,
                                   path_power_uw)


CAPABILITIES = '''data_size=512
pdo0=0x0201912c fixed=5000mV 3000mA
pdo4=0x000641f4 fixed=20000mV 5000mA
pdo5=0xc0dc3264 pps=5000-11000mV 5000mA
'''


def test_active_usb_type_uses_bracketed_value():
    assert active_value('Unknown [SDP] DCP CDP PD_PPS') == 'SDP'
    assert active_value('source [sink]') == 'sink'
    assert active_value(None) is None


def test_pps_capability_parser_preserves_fixed_and_pps_apdos():
    capabilities = parse_pps_capabilities(CAPABILITIES)
    assert capabilities['available']
    assert capabilities['pps'] == [{'min_mv': 5000, 'max_mv': 11000, 'current_ma': 5000}]
    assert len(capabilities['fixed']) == 2
    assert 'PPS 5.0–11.0 V / 5.0 A' in capability_summary(capabilities)
    assert '固定最高 20.0 V / 5.0 A' in capability_summary(capabilities)


def test_all_zero_pdos_are_not_a_capability():
    capabilities = parse_pps_capabilities(
        'data_size=512\n' + ''.join(f'pdo{i}=0x00000000 fixed=0mV 0mA\n'
                                     for i in range(7)))
    assert not capabilities['available']
    assert capabilities['fixed'] == capabilities['pps'] == []


def test_protocol_does_not_trust_stale_sdp_when_dual_pumps_are_active():
    adapter = {'online': 1, 'voltage_now': 8900000, 'active_type': 'SDP'}
    assert classify_protocol(adapter, {'supported': True},
                             parse_pps_capabilities(CAPABILITIES), 2) == 'USB PD PPS · 双泵'
    adapter['voltage_now'] = 5000000
    assert classify_protocol(adapter, {'supported': True},
                             parse_pps_capabilities(CAPABILITIES), 2) == '双泵充电 · 低压待识别'


def test_high_voltage_dual_pumps_without_pdos_is_vooc_not_pps():
    adapter = {'online': 1, 'voltage_now': 9011000, 'active_type': 'SDP'}
    empty = parse_pps_capabilities(
        'data_size=512\n' + ''.join(f'pdo{i}=0x00000000 fixed=0mV 0mA\n'
                                     for i in range(7)))
    assert classify_protocol(adapter, {'supported': True}, empty, 2) == 'VOOC / SVOOC · 双泵'
    assert classify_protocol(adapter, {'supported': True}, empty, 1) == 'VOOC / SVOOC · 单泵'


def test_other_protocols_have_distinct_labels():
    empty = parse_pps_capabilities('')
    assert classify_protocol({'online': 1, 'voltage_now': 5000000, 'active_type': 'DCP'},
                             {'supported': False}, empty, 0) == 'USB DCP 专用充电'
    assert classify_protocol({'online': 1, 'voltage_now': 5000000, 'active_type': 'CDP'},
                             {'supported': False}, empty, 0) == 'USB CDP 充电下行端口'
    capabilities = parse_pps_capabilities('pdo4=0x000641f4 fixed=20000mV 5000mA\n')
    assert classify_protocol({'online': 1, 'voltage_now': 9000000, 'active_type': 'PD'},
                             {'supported': True}, capabilities, 0) == 'USB PD 固定电压'
    assert classify_protocol({'online': 1, 'voltage_now': 5000000, 'active_type': 'PD_PPS'},
                             {'supported': True}, parse_pps_capabilities(CAPABILITIES), 0) == 'USB PD PPS · 待启动'


def test_path_power_ignores_missing_or_zero_telemetry():
    assert path_power_uw(8900000, 700000) == 6230000
    assert path_power_uw(0, 700000) is None
    assert path_power_uw(8900000, None) is None
