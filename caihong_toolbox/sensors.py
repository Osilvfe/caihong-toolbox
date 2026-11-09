from pathlib import Path
import re


PROXY_NAME = 'net.hadess.SensorProxy'
PROXY_PATH = '/net/hadess/SensorProxy'
PROXY_SENSORS = {
    'orientation': {'title': '屏幕方向（加速度计）', 'interface': PROXY_NAME, 'path': PROXY_PATH,
                    'has': 'HasAccelerometer', 'claim': 'ClaimAccelerometer', 'release': 'ReleaseAccelerometer',
                    'values': ('AccelerometerOrientation', 'AccelerometerTilt'),
                    'note': '此接口提供方向与倾斜状态，不提供加速度 XYZ 原始值。'},
    'light': {'title': '环境光', 'interface': PROXY_NAME, 'path': PROXY_PATH,
              'has': 'HasAmbientLight', 'claim': 'ClaimLight', 'release': 'ReleaseLight',
              'values': ('LightLevel', 'LightLevelUnit'), 'note': '光照值及单位来自系统传感器服务。'},
    'compass': {'title': '指南针', 'interface': PROXY_NAME + '.Compass', 'path': PROXY_PATH + '/Compass',
                'has': 'HasCompass', 'claim': 'ClaimCompass', 'release': 'ReleaseCompass',
                'values': ('CompassHeading',), 'note': '方向角来自系统指南针服务，不修改校准。'},
    'proximity': {'title': '接近传感器', 'interface': PROXY_NAME, 'path': PROXY_PATH,
                  'has': 'HasProximity', 'claim': 'ClaimProximity', 'release': 'ReleaseProximity',
                  'values': ('ProximityNear',), 'note': '仅当系统服务声明可用时列出。'},
}
IIO_NAMES = {'accel': '加速度计', 'anglvel': '陀螺仪', 'magn': '磁力计', 'illuminance': '环境光',
             'proximity': '接近传感器', 'pressure': '气压计', 'temp': '温度', 'humidityrelative': '湿度'}


def read_text(path):
    with path.open() as stream:
        return stream.read(256).strip()


def inventory(root=Path('/'), proxy=None):
    sensors = []
    proxy = proxy or {}
    for key, definition in PROXY_SENSORS.items():
        if proxy.get(definition['interface'], {}).get(definition['has']) is True:
            sensors.append({'id': 'proxy:' + key, 'source': 'proxy', 'title': definition['title'],
                            'note': definition['note']})
    for node in sorted((root / 'sys/bus/iio/devices').glob('iio:device*')):
        try:
            name = read_text(node / 'name')
            channels = {}
            for attribute in node.iterdir():
                match = re.fullmatch(r'in_([a-z]+)[0-9]*(?:_[a-z0-9]+)*_(raw|input)', attribute.name)
                if match:
                    channels.setdefault(match[1], []).append(attribute.name)
            for family, attributes in sorted(channels.items()):
                metadata = [attribute.name for attribute in node.iterdir()
                            if re.fullmatch(r'in_' + re.escape(family) + r'(?:_[a-z0-9]+)*_(scale|offset|sampling_frequency)', attribute.name)]
                if (node / 'sampling_frequency').is_file():
                    metadata.append('sampling_frequency')
                sensors.append({'id': f'iio:{node.name}:{family}', 'source': 'iio', 'node': node.name,
                                'title': f'{IIO_NAMES.get(family, family)} · {name} ({node.name})',
                                'files': sorted(attributes + metadata), 'note': '只读当前通道及量程换算参数，不改采样率或缓冲区。'})
        except OSError:
            continue
    nodes = sorted((root / 'sys/class/thermal').glob('thermal_zone*'), key=lambda node: int(node.name.removeprefix('thermal_zone')))
    for node in nodes:
        try:
            name = read_text(node / 'type')
            if (node / 'temp').is_file():
                sensors.append({'id': 'thermal:' + node.name, 'source': 'thermal', 'node': node.name,
                                'title': f'温度 · {name} ({node.name})', 'note': '只读该温区，不修改温控或阈值。'})
        except OSError:
            continue
    return sensors


def read_sensor(sensor, root=Path('/')):
    values, errors = {}, {}
    if sensor['source'] == 'thermal':
        files, directory = ['temp'], root / 'sys/class/thermal' / sensor['node']
    elif sensor['source'] == 'iio':
        files, directory = sensor['files'], root / 'sys/bus/iio/devices' / sensor['node']
    else:
        raise ValueError('此来源需要系统传感器服务')
    for filename in files:
        try:
            values[filename] = read_text(directory / filename)
        except OSError as error:
            errors[filename] = str(error)
    if sensor['source'] == 'thermal' and 'temp' in values:
        try:
            values['温度 (°C)'] = int(values['temp']) / 1000
        except ValueError:
            errors['temp'] = '返回值不是有效的毫摄氏度整数'
    return {'id': sensor['id'], 'values': values, 'errors': errors, 'origin': 'read'}
