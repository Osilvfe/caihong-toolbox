import pytest

from caihong_toolbox.power import (active_value, capability_summary, classify_protocol,
                                   detect_protocol, parse_fastchg_status,
                                   parse_pps_capabilities, path_power_uw)


CAPABILITIES = '''data_size=512
pdo0=0x0201912c fixed=5000mV 3000mA
pdo4=0x000641f4 fixed=20000mV 5000mA
pdo5=0xc0dc3264 pps=5000-11000mV 5000mA
'''


def test_active_usb_type_uses_bracketed_value():
    assert active_value('Unknown [SDP] DCP CDP PD_PPS') == 'SDP'
    assert active_value('source [sink]') == 'sink'
    assert active_value(None) is None
    assert active_value('PD') == 'PD'
    assert active_value('Unknown SDP DCP') is None


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


def test_protocol_uses_kernel_owner_instead_of_stale_sdp_or_capabilities():
    adapter = {'online': 1, 'voltage_now': 8900000, 'active_type': 'SDP',
               'fastchg': {'owner': 'pps', 'generation': 1, 'fault': 0,
                            'input_known': 1, 'input_present': 1}}
    assert classify_protocol(adapter, {'supported': True},
                             parse_pps_capabilities(CAPABILITIES), 2) == 'USB PD PPS · 双泵'
    assert classify_protocol(adapter, {'supported': True},
                             parse_pps_capabilities(None), 1) == 'USB PD PPS · 单泵'
    assert classify_protocol(adapter, {'supported': True},
                             parse_pps_capabilities(None), 0) == 'USB PD PPS · 协商 / 恢复中'


def test_pumps_and_capabilities_alone_do_not_prove_the_protocol():
    adapter = {'online': 1, 'voltage_now': 9011000, 'active_type': 'SDP'}
    empty = parse_pps_capabilities(
        'data_size=512\n' + ''.join(f'pdo{i}=0x00000000 fixed=0mV 0mA\n'
                                     for i in range(7)))
    assert classify_protocol(adapter, {'supported': True}, empty, 2) == '电荷泵工作 · 协议待确认'
    assert classify_protocol(adapter, {'supported': True},
                             parse_pps_capabilities(CAPABILITIES), 2) == '电荷泵工作 · 协议待确认'


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
                             {'supported': True}, parse_pps_capabilities(CAPABILITIES), 0) == 'USB PD PPS · 活动状态未确认'


@pytest.mark.parametrize('owner, pumps, expected', [
    ('vooc', 2, 'VOOC / SVOOC · 双泵'),
    ('vooc', 1, 'VOOC / SVOOC · 单泵'),
    ('vooc', 0, 'VOOC / SVOOC · 协商 / 恢复中'),
    ('pd', 0, 'USB PD 固定电压'),
    ('idle', 0, 'USB PD · PPS 可用，未启用'),
])
def test_owner_overrides_pps_capability_including_when_buck_is_suspended(owner, pumps, expected):
    adapter = {'online': 0, 'voltage_now': 8900000, 'active_type': 'PD_PPS',
               'fastchg': {'owner': owner, 'generation': 5, 'fault': 0,
                            'input_known': 1, 'input_present': 1, 'vooc_pending': 0}}
    assert classify_protocol(adapter, {}, parse_pps_capabilities(CAPABILITIES), pumps) == expected


def test_disconnected_source_overrides_stale_pumps_and_owner():
    adapter = {'online': 1, 'voltage_now': 8900000,
               'fastchg': {'owner': 'pps', 'generation': 5, 'fault': 0,
                            'input_known': 1, 'input_present': 0}}
    assert classify_protocol(adapter, {}, parse_pps_capabilities(CAPABILITIES), 2) == '未接入'


def test_fault_and_negotiation_are_not_active_fast_charge():
    adapter = {'online': 1, 'fastchg': {'owner': 'pps', 'fault': 1}}
    assert detect_protocol(adapter, {}, {}, 2)['protocol_kind'] == 'fault'
    adapter['fastchg'] = {'owner': 'idle', 'vooc_pending': 1}
    assert classify_protocol(adapter, {}, {}, 0) == '快充协商中'
    adapter['protocol_transition'] = True
    adapter['fastchg'] = {'owner': 'vooc'}
    assert classify_protocol(adapter, {}, {}, 2) == '充电状态切换中'


def test_missing_telemetry_and_pd_support_are_not_proof_of_connection_or_contract():
    assert classify_protocol({}, {'supported': True}, {}, 0) == '接入状态未知'
    assert classify_protocol({'online': 1}, {'supported': True}, {}, 0) == '已接入 · 协议未识别'
    assert classify_protocol({'online': 1}, {}, parse_pps_capabilities(CAPABILITIES), 0) == \
        'USB PD 能力已报告 · 活动协议待确认'


def test_otg_requires_a_partner_and_does_not_count_as_charging():
    assert detect_protocol({'online': 0}, {'power_role': 'source', 'partner_present': True},
                           {}, 0)['protocol_kind'] == 'otg'
    assert classify_protocol({'online': 0}, {'power_role': 'source', 'partner_present': False},
                             {}, 0) == '未接入'


def test_high_voltage_dcp_is_not_guessed_as_a_private_protocol():
    assert classify_protocol({'online': 1, 'active_type': 'DCP', 'voltage_now': 9000000},
                             {}, {}, 0) == 'USB DCP 专用充电 · 高压协议待确认'


@pytest.mark.parametrize('reported', ['BrickID', 'BRICKID', 'Apple_Brick_ID'])
def test_brick_id_spelling_matches_kernel_reports(reported):
    assert classify_protocol({'online': 1, 'active_type': reported}, {}, {}, 0) == 'Apple Brick ID'


def test_fastchg_status_validates_kernel_fields():
    parsed = parse_fastchg_status('owner=vooc generation=5 fault=0 input_known=1 input_present=1 vooc_pending=0')
    assert parsed['owner'] == 'vooc'
    assert parsed['generation'] == 5
    assert parsed['input_present'] == 1


@pytest.mark.parametrize('raw', [None, '', 'owner=vooc',
    'owner=invalid generation=1 fault=0 input_known=1 input_present=1',
    'owner=pd generation=abc fault=0 input_known=1 input_present=1',
    'owner=pd generation=-1 fault=0 input_known=1 input_present=1',
    'owner=pd generation=1 fault=2 input_known=1 input_present=1',
    'owner=pd generation=1 fault=0 input_known=1',
])
def test_malformed_fastchg_status_is_not_trusted(raw):
    assert parse_fastchg_status(raw) == {}


def test_pdo_bits_override_text_and_unsupported_apdos_are_not_pps():
    capabilities = parse_pps_capabilities('pdo0=0x0001912c fixed=9000mV 5000mA\n'
                                          'pdo1=0xc0dc3264\n'
                                          'pdo2=0xd0dc3264 pps=5000-11000mV 5000mA\n'
                                          'pdo3=0x10001912c fixed=5000mV 3000mA')
    assert capabilities['fixed'] == [{'min_mv': 5000, 'max_mv': 5000, 'current_ma': 3000}]
    assert capabilities['pps'] == [{'min_mv': 5000, 'max_mv': 11000, 'current_ma': 5000}]


def test_path_power_ignores_missing_or_zero_telemetry():
    assert path_power_uw(8900000, 700000) == 6230000
    assert path_power_uw(0, 700000) is None
    assert path_power_uw(8900000, None) is None
