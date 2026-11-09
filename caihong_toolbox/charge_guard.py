import logging

from .charge_hold import decide, save_config
from .hardware import Paths, apply_charge, read_snapshot
from .services import CHARGE_HOLD_TIMER, set_internal_service


def enforce(paths=None, snapshot=None):
    """Apply the persisted charge-hold policy once and report what happened."""
    paths = paths or Paths()
    snapshot = snapshot or read_snapshot(paths)
    battery = snapshot.get('battery') or {}
    config = snapshot.get('charge_hold') or {}
    desired = decide(battery.get('capacity'), config.get('enabled'), config.get('level'))
    if desired is None:
        return {'changed': False}
    if not battery.get('control_available'):
        return {'changed': False, 'error': '缺少 Caihong 专用充电接口'}
    if (battery.get('charge_behaviour') == 'inhibit-charge') == desired:
        return {'changed': False}
    apply_charge(paths, {'action': 'charge-bypass', 'enabled': desired})
    return {'changed': True, 'enabled': desired}


def set_charge_hold(paths, enabled, level):
    config = save_config(paths, enabled, level)
    try:
        set_internal_service(CHARGE_HOLD_TIMER, enabled)
    except RuntimeError as error:
        action = '启用' if enabled else '停用'
        return {'message': f'已保存设置,但后台计时器未能{action}:{error}', 'charge_hold': config}
    if not enabled:
        try:
            if read_snapshot(paths)['battery']['control_available']:
                apply_charge(paths, {'action': 'charge-bypass', 'enabled': False})
        except (OSError, RuntimeError):
            pass
        return {'message': '自动维持电量已停用,已恢复自动充电', 'charge_hold': config}
    result = enforce(paths)
    if result.get('error'):
        return {'message': f'自动维持电量已启用({level}%),但当前无法应用:{result["error"]}',
                'charge_hold': config}
    if result.get('changed'):
        state = '当前已切换到旁路供电' if result['enabled'] else '当前已恢复充电'
        return {'message': f'自动维持电量已启用({level}%),{state}', 'charge_hold': config}
    return {'message': f'自动维持电量已启用({level}%),后台将自动维持电量', 'charge_hold': config}


def main():
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    try:
        result = enforce()
    except (OSError, RuntimeError, ValueError) as error:
        logging.warning('charge hold enforcement failed: %s', error)
        return 1
    if result.get('changed'):
        logging.info('charge hold: %s', 'bypass on' if result['enabled'] else 'bypass off')
    return 0
