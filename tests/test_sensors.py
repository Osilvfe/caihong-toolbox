from unittest.mock import Mock

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from caihong_toolbox.sensor_panel import SensorPanel
from caihong_toolbox.sensors import PROXY_NAME, inventory, read_sensor
from caihong_toolbox.testing import COMMANDS, TESTS, HardwareTestDialog


@pytest.fixture
def root(tmp_path):
    device = tmp_path / 'sys/bus/iio/devices/iio:device0'
    device.mkdir(parents=True)
    for name, value in {'name': 'imu', 'in_accel_x_raw': '10', 'in_accel_y_raw': '20',
                         'in_accel_z_raw': '30', 'in_accel_scale': '0.01',
                         'in_anglvel_x_raw': '90', 'in_anglvel_scale': '0.001'}.items():
        (device / name).write_text(value)
    thermal = tmp_path / 'sys/class/thermal/thermal_zone0'
    thermal.mkdir(parents=True)
    (thermal / 'type').write_text('cpu-test')
    (thermal / 'temp').write_text('37500')
    return tmp_path


def test_storage_and_sensors_are_independent():
    assert TESTS['storage'][0] == '存储设备'
    assert TESTS['sensors'][0] == '传感器'
    assert 'sensors' not in COMMANDS
    assert COMMANDS['storage'][0][0] == 'lsblk'


def test_inventory_reads_names_not_measurements(root, monkeypatch):
    from caihong_toolbox import sensors
    actual, seen = sensors.read_text, []
    def read(path):
        seen.append(path.name)
        return actual(path)
    monkeypatch.setattr(sensors, 'read_text', read)
    devices = inventory(root, {PROXY_NAME: {'HasAmbientLight': True, 'HasAccelerometer': True,
                                           'HasProximity': False}, PROXY_NAME + '.Compass': {'HasCompass': True}})
    assert {sensor['id'] for sensor in devices} == {'proxy:light', 'proxy:orientation', 'proxy:compass',
                                                   'iio:iio:device0:accel', 'iio:iio:device0:anglvel', 'thermal:thermal_zone0'}
    assert set(seen) == {'name', 'type'}


def test_only_selected_sensor_channels_are_read(root, monkeypatch):
    from caihong_toolbox import sensors
    devices = inventory(root)
    selected = next(sensor for sensor in devices if sensor['id'].endswith(':accel'))
    actual, seen = sensors.read_text, []
    def read(path):
        seen.append(path.name)
        return actual(path)
    monkeypatch.setattr(sensors, 'read_text', read)
    result = read_sensor(selected, root)
    assert set(seen) == {'in_accel_x_raw', 'in_accel_y_raw', 'in_accel_z_raw', 'in_accel_scale'}
    assert result['values']['in_accel_x_raw'] == '10'
    assert result['errors'] == {}


def test_single_thermal_reports_raw_and_degrees(root):
    selected = next(sensor for sensor in inventory(root) if sensor['source'] == 'thermal')
    result = read_sensor(selected, root)
    assert result['values'] == {'temp': '37500', '温度 (°C)': 37.5}
    (root / 'sys/class/thermal/thermal_zone0/temp').unlink()
    assert 'temp' in read_sensor(selected, root)['errors']


@pytest.fixture
def application():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def panel(application, monkeypatch):
    widget = SensorPanel(Mock())
    monkeypatch.setattr(widget.client, 'observe', Mock())
    monkeypatch.setattr(widget.client, 'list_sensors', Mock())
    monkeypatch.setattr(widget.client, 'stop', Mock())
    widget.listed([{'id': 'proxy:light', 'title': '环境光', 'source': 'proxy', 'note': '光照'},
                   {'id': 'proxy:compass', 'title': '指南针', 'source': 'proxy', 'note': '角度'}])
    yield widget
    widget.stop()
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_selection_never_automatically_starts_all_sensors(panel):
    panel.client.observe.assert_not_called()
    panel.start()
    panel.client.observe.assert_called_once_with('proxy:light')
    panel.client.mode = 'watch'
    panel.selector.setCurrentIndex(1)
    panel.client.observe.assert_called_once_with('proxy:light')
    assert not panel.observing
    panel.start()
    assert panel.client.observe.call_args.args == ('proxy:compass',)


def test_other_sensor_readings_do_not_replace_selection(panel):
    panel.start()
    panel.received({'event': 'sample', 'id': 'proxy:compass', 'values': {'CompassHeading': 90},
                    'errors': {}, 'origin': 'update', 'time': 'now'})
    assert panel.table.rowCount() == 0
    panel.received({'event': 'sample', 'id': 'proxy:light', 'values': {'LightLevel': 100},
                    'errors': {}, 'origin': 'update', 'time': 'now'})
    assert panel.table.item(0, 1).text() == '100'
    panel.stop()
    assert not panel.observing


def test_sensor_demo_never_spawns_or_reads_hardware(application, monkeypatch):
    from caihong_toolbox.sensor_panel import SensorClient
    monkeypatch.setattr(SensorClient, 'list_sensors', lambda *arguments: pytest.fail('demo enumerated hardware'))
    dialog = HardwareTestDialog('sensors', lambda: {}, demo=True)
    dialog.begin()
    dialog.sensor_panel.refresh()
    dialog.sensor_panel.start()
    assert not dialog.sensor_panel.start_button.isEnabled()
    dialog.close()
    dialog.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
