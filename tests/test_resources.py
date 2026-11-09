from pathlib import Path

from caihong_toolbox.resources import ResourceSampler


def write(root, name, value):
    path = Path(root) / name.lstrip('/')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'{value}\n')
    return path


def test_resources_report_cpu_memory_thermal_and_gpu(tmp_path):
    write(tmp_path, '/proc/device-tree/model', 'Caihong Test\x00')
    write(tmp_path, '/proc/stat', '\n'.join((
        'cpu  100 0 100 800 0 0 0 0 0 0',
        'cpu0 100 0 100 800 0 0 0 0 0 0',
        'cpu1 100 0 100 800 0 0 0 0 0 0')))
    write(tmp_path, '/proc/meminfo', 'MemTotal: 15515764 kB\nMemAvailable: 10535024 kB\n'
                                      'SwapTotal: 0 kB\nSwapFree: 0 kB')
    write(tmp_path, '/proc/loadavg', '1.51 1.80 1.56 1/964 15576')
    write(tmp_path, '/proc/uptime', '266400.00 1234.00')
    for core in (0, 1):
        base = f'/sys/devices/system/cpu/cpu{core}/cpufreq'
        write(tmp_path, base + '/scaling_cur_freq', 1574400 + core * 100000)
        write(tmp_path, base + '/cpuinfo_max_freq', 2841600)
    write(tmp_path, '/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor', 'schedutil')
    write(tmp_path, '/sys/class/devfreq/3d00000.gpu/name', 'msm_gpu')
    write(tmp_path, '/sys/class/devfreq/3d00000.gpu/cur_freq', 680000000)
    write(tmp_path, '/sys/class/devfreq/3d00000.gpu/max_freq', 903000000)
    for zone, name, temp in (
            (0, 'cpuss0-thermal', 44200), (1, 'gpuss0-thermal', 41200),
            (2, 'qcom-battmgr-bat', 33000), (3, 'pm8550-thermal', 37000)):
        write(tmp_path, f'/sys/class/thermal/thermal_zone{zone}/type', name)
        write(tmp_path, f'/sys/class/thermal/thermal_zone{zone}/temp', temp)

    sampler = ResourceSampler(tmp_path)
    first = sampler.read()
    assert first['cpu']['usage'] is None
    assert first['cpu']['cores'][0]['usage'] is None
    write(tmp_path, '/proc/stat', '\n'.join((
        'cpu  200 0 150 850 0 0 0 0 0 0',
        'cpu0 200 0 150 850 0 0 0 0 0 0',
        'cpu1 200 0 150 850 0 0 0 0 0 0')))
    resources = sampler.read()
    assert resources['cpu']['usage'] == 75.0
    assert resources['cpu']['cores'][0]['usage'] == 75.0
    assert resources['cpu']['cores'][0]['freq_mhz'] == 1574
    assert resources['cpu']['cores'][0]['max_mhz'] == 2842
    assert resources['cpu']['governor'] == 'schedutil'
    assert resources['cpu']['model'] == 'Caihong Test'
    assert resources['memory']['usage'] == 32.1
    assert resources['load'] == {'one': 1.51, 'five': 1.8, 'fifteen': 1.56,
                                 'uptime_s': 266400}
    assert resources['gpu'] == {'name': 'msm_gpu', 'freq_mhz': 680, 'max_mhz': 903}
    assert resources['thermal']['summary'] == {'cpu': 44.2, 'gpu': 41.2,
                                               'battery': 33.0, 'pmic': 37.0}
    assert len(resources['thermal']['zones']) == 4
    assert resources['storage']['total_bytes'] > 0


def test_missing_resource_files_are_unknown(tmp_path):
    resources = ResourceSampler(tmp_path).read()
    assert resources['cpu']['usage'] is None
    assert resources['cpu']['cores'] == []
    assert resources['memory']['total_kib'] is None
    assert resources['thermal'] == {'summary': {}, 'zones': []}
    assert resources['gpu'] is None
