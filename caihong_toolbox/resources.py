import os
from pathlib import Path
import threading


THERMAL_ORDER = ('cpu', 'gpu', 'ddr', 'npu', 'video', 'camera',
                 'battery', 'pmic', 'modem', 'other')


def _text(path):
    try:
        return path.read_text(encoding='utf-8', errors='replace').replace('\x00', ' ').strip()
    except OSError:
        return None


def _number(path):
    value = _text(path)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _cpu_times(root):
    value = _text(root / 'proc/stat')
    result = {}
    for line in (value or '').splitlines():
        parts = line.split()
        if not parts or not parts[0].startswith('cpu'):
            continue
        name = parts[0]
        if name != 'cpu' and not name[3:].isdigit():
            continue
        try:
            fields = [int(item) for item in parts[1:]]
        except ValueError:
            continue
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
        total = sum(fields[:8]) if len(fields) >= 8 else sum(fields)
        result[name] = (total, idle)
    return result


def _usage(previous, current):
    if not previous or not current:
        return None
    total = current[0] - previous[0]
    idle = current[1] - previous[1]
    if total <= 0 or idle < 0:
        return None
    return round(max(0.0, min(100.0, (total - idle) * 100.0 / total)), 1)


def _mhz(path):
    value = _number(path)
    return round(value / 1000) if value and value > 0 else None


def _mhz_from_hz(path):
    value = _number(path)
    return round(value / 1000000) if value and value > 0 else None


def _gpu(root):
    for node in sorted((root / 'sys/class/devfreq').glob('*')):
        name = (_text(node / 'name') or node.name).strip()
        if 'gpu' not in name.lower() and 'gpu' not in node.name.lower():
            continue
        current = _mhz_from_hz(node / 'cur_freq')
        maximum = _mhz_from_hz(node / 'max_freq')
        if current is not None or maximum is not None:
            return {'name': name, 'freq_mhz': current, 'max_mhz': maximum}
    gpuclk = _number(root / 'sys/class/kgsl/kgsl-3d0/gpuclk')
    if gpuclk:
        return {'name': 'kgsl-3d0', 'freq_mhz': round(gpuclk / 1000000),
                'max_mhz': None}
    return None


def _memory(root):
    values = {}
    for line in (_text(root / 'proc/meminfo') or '').splitlines():
        name, _, rest = line.partition(':')
        fields = rest.split()
        if fields:
            try:
                values[name] = int(fields[0])
            except ValueError:
                pass
    total = values.get('MemTotal')
    available = values.get('MemAvailable')
    used = total - available if total and available is not None else None
    return {
        'total_kib': total,
        'available_kib': available,
        'used_kib': used,
        'usage': round(used * 100.0 / total, 1) if used is not None and total else None,
        'swap_total_kib': values.get('SwapTotal'),
        'swap_free_kib': values.get('SwapFree'),
    }


def _load(root):
    fields = (_text(root / 'proc/loadavg') or '').split()
    uptime = (_text(root / 'proc/uptime') or '').split()
    result = {'one': None, 'five': None, 'fifteen': None, 'uptime_s': None}
    if len(fields) >= 3:
        try:
            result['one'], result['five'], result['fifteen'] = (float(item) for item in fields[:3])
        except ValueError:
            pass
    if uptime:
        try:
            result['uptime_s'] = int(float(uptime[0]))
        except ValueError:
            pass
    return result


def _storage(root):
    try:
        stat = os.statvfs(root)
    except OSError:
        return None
    total = stat.f_blocks * stat.f_frsize
    free = stat.f_bfree * stat.f_frsize
    available = stat.f_bavail * stat.f_frsize
    used = total - free if total else None
    return {'total_bytes': total, 'free_bytes': available, 'used_bytes': used,
            'usage': round(used * 100.0 / total, 1) if used is not None and total else None}


def _thermal_category(name):
    value = name.lower()
    if 'gpuss' in value or 'gpu' in value:
        return 'gpu'
    if 'cpuss' in value or value.startswith('cpu'):
        return 'cpu'
    if 'ddr' in value:
        return 'ddr'
    if value.startswith('nsp') or 'npu' in value:
        return 'npu'
    if 'video' in value:
        return 'video'
    if 'camera' in value:
        return 'camera'
    if 'batt' in value:
        return 'battery'
    if value.startswith('pm'):
        return 'pmic'
    if 'modem' in value:
        return 'modem'
    return 'other'


def _thermal(root):
    zones = []
    summary = {}
    for node in sorted((root / 'sys/class/thermal').glob('thermal_zone*'),
                       key=lambda item: int(item.name[12:]) if item.name[12:].isdigit() else 0):
        name = _text(node / 'type') or node.name
        raw = _number(node / 'temp')
        if raw is None or not 0 < raw < 200000:
            continue
        celsius = round(raw / 1000.0, 1)
        category = _thermal_category(name)
        zones.append({'name': name, 'category': category, 'celsius': celsius})
        summary[category] = max(summary.get(category, celsius), celsius)
    ordered = {key: summary[key] for key in THERMAL_ORDER if key in summary}
    return {'summary': ordered, 'zones': zones}


def _cpu_model(root):
    model = _text(root / 'proc/device-tree/model')
    if model:
        return model
    for line in (_text(root / 'proc/cpuinfo') or '').splitlines():
        if ':' not in line:
            continue
        name, _, value = line.partition(':')
        if name.strip() in ('model name', 'Hardware'):
            value = value.strip()
            if value:
                return value
    return None


class ResourceSampler:
    def __init__(self, root=Path('/')):
        self.root = Path(root)
        self.previous = None
        self.lock = threading.Lock()

    def read(self):
        with self.lock:
            current = _cpu_times(self.root)
            previous, self.previous = self.previous, current
            previous = previous or {}
            cores = []
            for name in sorted((item for item in current if item != 'cpu'),
                               key=lambda item: int(item[3:])):
                base = self.root / 'sys/devices/system/cpu' / name / 'cpufreq'
                cores.append({
                    'id': int(name[3:]),
                    'usage': _usage(previous.get(name), current.get(name)),
                    'freq_mhz': _mhz(base / 'scaling_cur_freq'),
                    'max_mhz': _mhz(base / 'cpuinfo_max_freq'),
                })
            governor = _text(self.root / 'sys/devices/system/cpu/cpu0/cpufreq/scaling_governor')
            return {
                'cpu': {'usage': _usage(previous.get('cpu'), current.get('cpu')),
                        'model': _cpu_model(self.root), 'governor': governor,
                        'cores': cores},
                'memory': _memory(self.root),
                'load': _load(self.root),
                'gpu': _gpu(self.root),
                'storage': _storage(self.root),
                'thermal': _thermal(self.root),
            }
