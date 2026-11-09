import json
import os
from pathlib import Path
import sys

from PySide6.QtCore import QObject, QProcess, QTimer, Qt, Signal
from PySide6.QtWidgets import QAbstractItemView, QComboBox, QHBoxLayout, QLabel, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget


class SensorClient(QObject):
    inventory_received = Signal(list)
    message_received = Signal(dict)
    error_received = Signal(str)
    ended = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.process, self.mode = None, None
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.timeout)

    def list_sensors(self):
        self.start(['--list'], 'list')

    def observe(self, identifier):
        self.start(['--sensor', identifier], 'watch')

    def start(self, arguments, mode):
        self.stop()
        process = QProcess(self)
        self.process, self.mode = process, mode
        state = {'buffer': b''}
        process.readyReadStandardOutput.connect(lambda: self.read(process, state))
        process.readyReadStandardError.connect(lambda: self.read_error(process))
        process.finished.connect(lambda code, status: self.finished(process, state, code))
        process.errorOccurred.connect(lambda error: self.failed(process, error))
        process.start(sys.executable, ['-I', str(Path(__file__).with_name('sensor_worker.py')), *arguments, '--parent', str(os.getpid())])
        if mode == 'list' and self.process is process:
            self.timer.start(5000)

    def read(self, process, state):
        if process is not self.process:
            return
        state['buffer'] += bytes(process.readAllStandardOutput())
        if len(state['buffer']) > 256 * 1024:
            self.error_received.emit('传感器输出超过缓冲上限，观察已停止')
            self.stop()
            self.ended.emit()
            return
        while b'\n' in state['buffer']:
            payload, state['buffer'] = state['buffer'].split(b'\n', 1)
            try:
                message = json.loads(payload)
                if message['event'] == 'inventory':
                    self.inventory_received.emit(message['sensors'])
                    for warning in message.get('warnings', []):
                        self.message_received.emit({'event': 'warning', 'message': warning})
                elif message['event'] == 'error':
                    self.error_received.emit(message['message'])
                else:
                    self.message_received.emit(message)
            except (ValueError, TypeError, KeyError):
                self.error_received.emit('传感器进程返回了无效数据')

    def finished(self, process, state, code):
        if process is not self.process:
            return
        self.read(process, state)
        if process is not self.process:
            return
        self.timer.stop()
        self.process, self.mode = None, None
        process.deleteLater()
        if code:
            self.error_received.emit(f'传感器进程退出，返回值 {code}')
        self.ended.emit()

    def read_error(self, process):
        if process is self.process:
            self.error_received.emit(bytes(process.readAllStandardError())[:2048].decode('utf-8', errors='replace'))

    def failed(self, process, error):
        if process is self.process and error == QProcess.ProcessError.FailedToStart:
            self.error_received.emit('无法启动传感器读取进程：' + process.errorString())
            self.stop()
            self.ended.emit()

    def timeout(self):
        self.error_received.emit('枚举传感器超时；未开始观察')
        self.stop()
        self.ended.emit()

    def stop(self):
        self.timer.stop()
        process, self.process, self.mode = self.process, None, None
        if process:
            if process.state() != QProcess.ProcessState.NotRunning:
                process.terminate()
                if not process.waitForFinished(1500):
                    process.kill()
                    process.waitForFinished(1000)
            process.deleteLater()


class SensorPanel(QWidget):
    def __init__(self, report, demo=False, parent=None):
        super().__init__(parent)
        self.report, self.demo = report, demo
        self.observing, self.last_error = False, None
        self.client = SensorClient(self)
        self.client.inventory_received.connect(self.listed)
        self.client.message_received.connect(self.received)
        self.client.error_received.connect(self.failed)
        self.client.ended.connect(self.ended)
        layout = QVBoxLayout(self)
        self.selector = QComboBox()
        self.selector.setMinimumWidth(350)
        self.selector.currentIndexChanged.connect(self.selection_changed)
        layout.addWidget(self.selector)
        buttons = QHBoxLayout()
        self.refresh_button = QPushButton('刷新传感器列表')
        self.start_button = QPushButton('开始观察所选传感器')
        self.stop_button = QPushButton('停止该传感器')
        for button in (self.refresh_button, self.start_button, self.stop_button):
            buttons.addWidget(button)
        self.refresh_button.setEnabled(not demo)
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.refresh_button.clicked.connect(self.refresh)
        self.start_button.clicked.connect(self.start)
        self.stop_button.clicked.connect(self.stop)
        layout.addLayout(buttons)
        self.note = QLabel('只列出系统已公开的传感器接口；未列出不代表物理硬件不存在。')
        self.note.setTextFormat(Qt.TextFormat.PlainText)
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        self.state = QLabel('演示模式：不访问传感器' if demo else '选择一个传感器，手动开始观察')
        self.state.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.state)
        self.table = QTableWidget(0, 2)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setHorizontalHeaderLabels(['项目', '当前读数'])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMinimumHeight(180)
        layout.addWidget(self.table)
        self.values = {}

    def refresh(self):
        if not self.demo:
            self.stop()
            self.state.setText('正在列举接口；不会启动所有传感器')
            self.client.list_sensors()

    def listed(self, sensors):
        previous = self.selector.currentData()
        self.selector.blockSignals(True)
        self.selector.clear()
        for sensor in sensors:
            self.selector.addItem(sensor['title'], sensor)
        if previous:
            for index in range(self.selector.count()):
                if self.selector.itemData(index)['id'] == previous['id']:
                    self.selector.setCurrentIndex(index)
                    break
        self.selector.blockSignals(False)
        self.selection_changed()
        if not sensors:
            self.state.setText('未发现系统已公开的传感器接口')

    def selection_changed(self):
        if self.client.mode == 'watch':
            self.stop()
        self.values.clear()
        self.table.setRowCount(0)
        sensor = self.selector.currentData()
        self.note.setText(sensor['note'] if sensor else '无可用接口')
        self.state.setText('未开始观察')
        self.start_button.setEnabled(bool(sensor) and not self.demo)

    def start(self):
        sensor = self.selector.currentData()
        if self.demo or not sensor:
            return
        self.last_error, self.observing = None, True
        self.values.clear()
        self.table.setRowCount(0)
        self.state.setText('正在准备：' + sensor['title'])
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.client.observe(sensor['id'])

    def received(self, message):
        sensor = self.selector.currentData()
        if message['event'] == 'warning':
            self.report('sensor-warning', message)
            return
        if not self.observing or not sensor:
            return
        if message['event'] == 'ready' and message['sensor']['id'] == sensor['id']:
            self.state.setText('观察中：' + sensor['title'])
        elif message['event'] == 'sample' and message['id'] == sensor['id']:
            self.values.update(message['values'])
            self.values.update({name: '读取错误：' + error for name, error in message.get('errors', {}).items()})
            self.table.setRowCount(len(self.values))
            for row, (name, value) in enumerate(self.values.items()):
                self.table.setItem(row, 0, QTableWidgetItem(name))
                self.table.setItem(row, 1, QTableWidgetItem(str(value)))
            self.table.resizeColumnToContents(0)
            origin = '接口初值，等待后续更新' if message['origin'] == 'initial' else '最新读数'
            self.state.setText(origin + ' · ' + message['time'])
            self.report('sensor-sample', message)

    def failed(self, message):
        self.last_error = message
        self.state.setText('传感器接口：' + message)
        self.report('sensor-error', message)

    def ended(self):
        if self.observing:
            self.observing = False
            self.stop_button.setEnabled(False)
            self.start_button.setEnabled(bool(self.selector.currentData()) and not self.demo)
            if not self.last_error:
                self.state.setText('已停止')

    def stop(self):
        self.client.stop()
        self.observing = False
        self.stop_button.setEnabled(False)
        self.start_button.setEnabled(bool(self.selector.currentData()) and not self.demo)
        self.state.setText('已停止')
