from array import array
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import shutil
import signal
import sys
import tempfile
import time
import wave

from PySide6.QtCore import QProcess, QStandardPaths, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QPlainTextEdit,
    QPushButton, QVBoxLayout, QWidget,
)


TESTS = {
    'display': ('屏幕色卡', '点击色卡切换红、绿、蓝、白、黑和灰色；观察坏点、条纹及闪烁，不切换显示模式。'),
    'touch': ('触屏与笔尖', '在画布上用手指划线，再用笔书写；观察落点、边缘与压力。不会改变校准和扫描模式。'),
    'keyboard': ('键盘与触控板', '先点击输入区域取得焦点，再测试 Esc、F1–F12、Fn 组合和触控板。桌面保留的快捷键可能无法被本窗口收到；不拦截全局输入。'),
    'speaker': ('扬声器', '点击播放后，以当前系统音量播放两秒低幅度左右声道音调。请先将系统音量调低；不会修改系统音量。'),
    'microphone': ('麦克风', '手动开始、停止录音并回放，显示最近12秒逐秒 RMS；电平不代表人声质量合格。临时录音上限64 MiB，关闭窗口删除。'),
    'camera': ('摄像头', '使用 Qt 多媒体摄像头路径实时预览，可选择设备和驱动公布的分辨率/帧率。只列出实际枚举到的设备；不会安装桥接或修改默认相机。'),
    'audio-nodes': ('音视频节点', '只读列举 PipeWire、ALSA、libcamera 和 V4L2 节点。列举成功不等于实际采集可用。'),
    'network': ('网络与蓝牙', '只读查看当前网络接口和蓝牙控制器。不扫描、不配对、不重连，也不上传数据。'),
    'power': ('充电与电池', '查看电量、电流、温度及固件诊断。不改变阀门、供电模式或充电协议；观察时长由你决定。'),
    'storage': ('存储设备', '只读查看块设备；不写盘、不跑压力测试，不作 MCQ 稳定性结论。'),
    'sensors': ('传感器', '选择某个传感器后单独开始、停止观察。不启动其他传感器，不安排动作顺序，也不改校准或采样率。'),
}

COMMANDS = {
    'audio-nodes': [('wpctl', 'status'), ('arecord', '-l'), ('aplay', '-l'),
                    ('cam', '-l'), ('v4l2-ctl', '--list-devices')],
    'network': [('nmcli', 'device', 'status'), ('bluetoothctl', 'show'), ('ip', '-brief', 'address')],
    'storage': [('lsblk', '-o', 'NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS')],
}


def microphone_levels(filename):
    with wave.open(str(filename), 'rb') as stream:
        if stream.getsampwidth() != 2 or stream.getnchannels() != 1 or stream.getframerate() <= 0:
            raise ValueError('录音格式不是单声道 PCM16，无法计算电平')
        rate = stream.getframerate()
        start_second = max(0, math.ceil(stream.getnframes() / rate) - 12)
        stream.setpos(start_second * rate)
        levels = []
        for second in range(12):
            payload = stream.readframes(rate)
            if not payload:
                break
            samples = array('h', payload)
            if sys.byteorder != 'little':
                samples.byteswap()
            rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples)) / 32768
            levels.append({'second': start_second + second, 'seconds': round(len(samples) / rate, 3),
                           'rms': round(rms, 6), 'dbfs': round(20 * math.log10(max(rms, 1e-9)), 1),
                           'peak': round(max(abs(sample) for sample in samples) / 32768, 6)})
        return levels


def speaker_wave(filename):
    samples = array('h')
    rate = 48000
    for frame in range(rate * 2):
        position = frame % rate
        fade = min(1, position / 1000, (rate - position) / 1000)
        sample = int(2600 * fade * math.sin(2 * math.pi * 440 * frame / rate))
        samples.extend((sample, 0) if frame < rate else (0, sample))
    if sys.byteorder != 'little':
        samples.byteswap()
    with wave.open(str(filename), 'wb') as stream:
        stream.setparams((2, 2, rate, 0, 'NONE', 'not compressed'))
        stream.writeframes(samples.tobytes())


class InputCanvas(QWidget):
    def __init__(self, kind, report):
        super().__init__()
        self.kind, self.report = kind, report
        self.lines = []
        self.last_point = None
        self.color_index = 0
        self.colors = ['#e74040', '#34b858', '#326fe5', '#ffffff', '#000000', '#808080']
        self.setMinimumHeight(280)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents)
        self.setMouseTracking(True)

    def paintEvent(self, event):
        painter = QPainter(self)
        palette = self.palette()
        if self.kind == 'display':
            background = QColor(self.colors[self.color_index])
            foreground = QColor('#ffffff' if self.color_index in (0, 1, 2, 4) else '#000000')
        else:
            background = palette.color(QPalette.ColorRole.Base)
            foreground = palette.color(QPalette.ColorRole.Text)
        painter.fillRect(self.rect(), background)
        painter.setPen(foreground)
        painter.drawText(16, 28, '点击切换色卡' if self.kind == 'display' else '点击此处，然后书写 / 按键 / 滑动触控板')
        pen_color = palette.color(QPalette.ColorRole.Highlight)
        for start, end, pressure in self.lines:
            painter.setPen(QPen(pen_color, max(1, pressure * 8)))
            painter.drawLine(start, end)

    def point(self, position, pressure=0.5):
        if self.last_point is not None:
            self.lines.append((self.last_point, position, pressure))
            self.lines = self.lines[-4000:]
        self.last_point = position
        self.update()

    def event(self, event):
        if event.type() in (event.Type.TouchBegin, event.Type.TouchUpdate, event.Type.TouchEnd):
            if self.kind == 'display':
                if event.type() == event.Type.TouchBegin:
                    self.color_index = (self.color_index + 1) % len(self.colors)
                    self.report('color', self.colors[self.color_index])
                    self.update()
                event.accept()
                return True
            points = event.points()
            if points:
                self.point(points[0].position())
                self.report('touch', {'points': len(points), 'x': round(points[0].position().x(), 1),
                                      'y': round(points[0].position().y(), 1)})
            if event.type() == event.Type.TouchEnd:
                self.last_point = None
            event.accept()
            return True
        return super().event(event)

    def mousePressEvent(self, event):
        self.setFocus()
        if self.kind == 'display':
            self.color_index = (self.color_index + 1) % len(self.colors)
            self.report('color', self.colors[self.color_index])
            self.update()
        else:
            self.last_point = event.position()
            self.report('button', {'button': event.button().name})

    def mouseReleaseEvent(self, event):
        self.last_point = None

    def mouseMoveEvent(self, event):
        if event.buttons():
            self.point(event.position())
        self.report('pointer', {'x': round(event.position().x(), 1), 'y': round(event.position().y(), 1)})

    def tabletEvent(self, event):
        self.point(event.position(), event.pressure())
        self.report('pen-tip', {'x': round(event.position().x(), 1), 'y': round(event.position().y(), 1),
                                'pressure': round(event.pressure(), 3)})
        if event.type() == event.Type.TabletRelease:
            self.last_point = None
        event.accept()

    def keyPressEvent(self, event):
        self.report('key', {'qt_key': event.key(), 'scan': event.nativeScanCode(),
                            'virtual': event.nativeVirtualKey(), 'repeat': event.isAutoRepeat(),
                            'modifiers': event.modifiers().value})
        event.accept()

    def keyReleaseEvent(self, event):
        self.report('key-release', {'qt_key': event.key(), 'scan': event.nativeScanCode()})
        event.accept()


class HardwareTestDialog(QDialog):
    def __init__(self, kind, snapshot, demo=False, parent=None):
        super().__init__(parent)
        self.kind, self.snapshot, self.demo = kind, snapshot, demo
        self.records, self.counts = [], {}
        self.started = time.monotonic()
        self.running = not demo
        self.process = None
        self.process_role = None
        self.commands = list(COMMANDS.get(kind, []))
        self.camera, self.capture, self.video = None, None, None
        self.frames, self.last_frame_at = 0, None
        self.microphone_ready = False
        self.temporary = None
        self.setWindowTitle('Caihong 硬件测试 · ' + TESTS[kind][0])
        self.resize(850, 700)
        layout = QVBoxLayout(self)
        instruction = QLabel(TESTS[kind][1])
        instruction.setWordWrap(True)
        layout.addWidget(instruction)
        self.status = QLabel('演示模式：不打开硬件' if demo else '自由观察，操作和结束时间由你决定')
        layout.addWidget(self.status)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.document().setMaximumBlockCount(250)
        self.actions = []
        if kind in ('display', 'touch', 'keyboard'):
            self.canvas = InputCanvas(kind, self.record)
            layout.addWidget(self.canvas, 1)
            self.add_button(layout, '全屏 / 退出全屏', self.toggle_fullscreen)
        if kind in ('speaker', 'microphone'):
            self.audio_start = self.add_button(layout, '播放低幅度左右声道音调' if kind == 'speaker' else '开始录音', self.start_audio)
            if kind == 'microphone':
                self.audio_stop = self.add_button(layout, '停止录音', self.stop_recording)
                self.audio_stop.setEnabled(False)
                self.replay = self.add_button(layout, '回放录音', self.replay_audio)
                self.replay.setEnabled(False)
        if kind == 'camera':
            self.devices, self.formats = QComboBox(), QComboBox()
            layout.addWidget(self.devices)
            layout.addWidget(self.formats)
            self.devices.currentIndexChanged.connect(self.camera_formats)
            self.add_button(layout, '开始 / 切换预览', self.start_camera)
        if kind == 'sensors':
            from .sensor_panel import SensorPanel
            self.sensor_panel = SensorPanel(self.record, demo, self)
            layout.addWidget(self.sensor_panel, 1)
        layout.addWidget(self.output, 1)
        controls = QHBoxLayout()
        actions = ([] if kind == 'sensors' else [('停止观察', self.stop)]) + [('保存记录…', self.export_report), ('关闭', self.close)]
        for title, callback in actions:
            button = QPushButton(title)
            button.clicked.connect(callback)
            controls.addWidget(button)
        layout.addLayout(controls)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.process_timer = QTimer(self)
        self.process_timer.setSingleShot(True)
        self.process_timer.timeout.connect(self.process_timeout)
        self.kill_timer = QTimer(self)
        self.kill_timer.setSingleShot(True)
        self.kill_timer.timeout.connect(self.kill_process)
        if not demo:
            self.timer.start(1000)
            QTimer.singleShot(0, self.begin)

    def add_button(self, layout, title, callback):
        button = QPushButton(title)
        button.clicked.connect(callback)
        button.setEnabled(not self.demo)
        layout.addWidget(button)
        self.actions.append(button)
        return button

    def record(self, kind, value):
        self.counts[kind] = self.counts.get(kind, 0) + 1
        if kind in ('pointer', 'touch', 'pen-tip') and self.counts[kind] % 10 != 1:
            return
        entry = {'elapsed': round(time.monotonic() - self.started, 3), 'kind': kind, 'value': value}
        self.records.append(entry)
        self.records = self.records[-1000:]
        self.output.appendPlainText(json.dumps(entry, ensure_ascii=False))

    def begin(self):
        if not self.running:
            return
        if self.kind == 'camera':
            self.enumerate_cameras()
        elif self.kind == 'sensors':
            self.sensor_panel.refresh()
        elif self.commands:
            self.next_command()
        elif self.kind == 'power':
            self.record('battery', self.snapshot())

    def tick(self):
        elapsed = int(time.monotonic() - self.started)
        self.status.setText(f'已观察 {elapsed} 秒 · 事件计数 {self.counts}')
        if self.kind == 'power':
            self.record('battery', self.snapshot())
        if self.process_role == 'record' and self.audio_stop.isEnabled() and self.audio_file.exists() and self.audio_file.stat().st_size >= 64 * 1024 * 1024:
            self.record('info', '达到64 MiB临时录音空间上限，已请求停止录音')
            self.stop_recording()

    def toggle_fullscreen(self):
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def start_process(self, arguments, role, seconds=8):
        if not self.running or self.process is not None:
            return False
        program = shutil.which(arguments[0])
        if program is None:
            self.record('unavailable', '缺少依赖：' + arguments[0])
            return False
        self.process, self.process_role = QProcess(self), role
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self.read_process)
        self.process.finished.connect(self.process_finished)
        self.process.errorOccurred.connect(self.process_error)
        self.process.start(program, arguments[1:])
        if seconds is not None:
            self.process_timer.start(seconds * 1000)
        return True

    def read_process(self):
        if self.process:
            payload = bytes(self.process.readAllStandardOutput())
            self.record('process-output', payload[:8192].decode('utf-8', errors='replace'))

    def process_error(self, error):
        self.record('process-error', error.name)
        if error == QProcess.ProcessError.FailedToStart:
            self.process_finished(-1, QProcess.ExitStatus.CrashExit)

    def process_timeout(self):
        if self.process:
            if self.process_role == 'record' and self.process.processId():
                try:
                    os.kill(self.process.processId(), signal.SIGINT)
                except ProcessLookupError:
                    pass
            else:
                self.process.terminate()
            self.kill_timer.start(1500)

    def kill_process(self):
        if self.process:
            self.process.kill()

    def process_finished(self, code, status):
        if self.process is None:
            return
        self.process_timer.stop()
        self.kill_timer.stop()
        self.read_process()
        role = self.process_role
        self.process.deleteLater()
        self.process, self.process_role = None, None
        self.record('process-exit', {'role': role, 'code': code, 'status': status.name})
        if role == 'record':
            try:
                levels = microphone_levels(self.audio_file)
                self.microphone_ready = bool(levels)
                self.record('microphone-rms', levels)
                self.replay.setEnabled(self.running and self.microphone_ready)
            except (OSError, ValueError, EOFError, wave.Error) as error:
                self.record('error', str(error))
        if hasattr(self, 'audio_start'):
            self.audio_start.setEnabled(self.running)
        if hasattr(self, 'audio_stop'):
            self.audio_stop.setEnabled(False)
        if role == 'diagnostic' and self.running:
            QTimer.singleShot(0, self.next_command)

    def next_command(self):
        if not self.running:
            return
        while self.commands:
            command = self.commands.pop(0)
            self.record('command', list(command))
            if self.start_process(list(command), 'diagnostic'):
                return
        self.record('info', '只读列举结束；节点存在或命令成功不等于硬件测试通过')

    def prepare_audio(self):
        if self.temporary is None:
            root = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation))
            root.mkdir(parents=True, exist_ok=True)
            self.temporary = tempfile.TemporaryDirectory(prefix='hardware-test-', dir=root)
        self.audio_file = Path(self.temporary.name) / 'test.wav'

    def start_audio(self):
        if not self.running or self.process:
            return
        self.prepare_audio()
        if self.kind == 'speaker':
            speaker_wave(self.audio_file)
            started = self.start_process(['pw-play', str(self.audio_file)], 'play')
        else:
            self.microphone_ready = False
            self.replay.setEnabled(False)
            started = self.start_process(['pw-record', '--rate=48000', '--channels=1', '--format=s16', str(self.audio_file)], 'record', None)
            self.audio_stop.setEnabled(started)
        self.audio_start.setEnabled(not started)

    def stop_recording(self):
        if self.process_role == 'record' and self.audio_stop.isEnabled():
            self.audio_stop.setEnabled(False)
            self.process_timeout()

    def replay_audio(self):
        if self.microphone_ready:
            self.start_process(['pw-play', str(self.audio_file)], 'play', None)

    def enumerate_cameras(self):
        try:
            from PySide6.QtMultimedia import QMediaDevices
            for device in QMediaDevices.videoInputs():
                self.devices.addItem(device.description(), device)
            if self.devices.count() == 0:
                self.record('unavailable', 'Qt 未枚举到摄像头；请用“音视频节点”分别检查 PipeWire / libcamera / V4L2')
        except (ImportError, RuntimeError) as error:
            self.record('unavailable', str(error))

    def camera_formats(self):
        self.formats.clear()
        device = self.devices.currentData()
        if device is not None:
            for camera_format in device.videoFormats():
                size = camera_format.resolution()
                self.formats.addItem(f'{size.width()}×{size.height()} · {camera_format.minFrameRate():g}–{camera_format.maxFrameRate():g} fps · {camera_format.pixelFormat().name}', camera_format)

    def start_camera(self):
        if not self.running or self.devices.currentData() is None:
            return
        try:
            from PySide6.QtMultimedia import QCamera, QMediaCaptureSession
            from PySide6.QtMultimediaWidgets import QVideoWidget
            if self.camera:
                self.camera.stop()
                self.capture.setCamera(None)
                self.camera.deleteLater()
            if self.video is None:
                self.video = QVideoWidget()
                self.video.setMinimumHeight(240)
                self.layout().insertWidget(3, self.video, 1)
                self.capture = QMediaCaptureSession(self)
                self.capture.setVideoOutput(self.video)
                self.video.videoSink().videoFrameChanged.connect(self.camera_frame)
            self.camera = QCamera(self.devices.currentData(), self)
            if self.formats.currentData() is not None:
                self.camera.setCameraFormat(self.formats.currentData())
            self.camera.errorOccurred.connect(lambda error, message: self.record('camera-error', message))
            self.capture.setCamera(self.camera)
            self.frames, self.last_frame_at = 0, None
            self.camera.start()
        except (ImportError, RuntimeError) as error:
            self.record('camera-error', str(error))

    def camera_frame(self, frame):
        if not self.running or not frame.isValid():
            return
        now = time.monotonic()
        self.frames += 1
        if self.last_frame_at is None:
            self.last_frame_at = now
        if self.frames % 30 == 1:
            self.record('camera-frame', {'frames': self.frames, 'size': [frame.width(), frame.height()],
                                         'observed_fps': round((self.frames - 1) / max(now - self.last_frame_at, 0.001), 1)})

    def stop(self):
        self.running = False
        self.timer.stop()
        self.process_timer.stop()
        self.kill_timer.stop()
        for button in self.actions:
            button.setEnabled(False)
        if self.process:
            self.process.terminate()
            if not self.process.waitForFinished(1200):
                self.process.kill()
                self.process.waitForFinished(1000)
        if self.camera:
            self.camera.stop()
        if hasattr(self, 'sensor_panel'):
            self.sensor_panel.stop()
        if hasattr(self, 'canvas'):
            self.canvas.setEnabled(False)
        self.status.setText('已停止')

    def report(self):
        return {'test': self.kind, 'title': TESTS[self.kind][0], 'demo': self.demo,
                'time': datetime.now(timezone.utc).isoformat(), 'running': self.running,
                'counts': self.counts, 'records': self.records}

    def export_report(self):
        filename, selected = QFileDialog.getSaveFileName(self, '保存报告（可能包含设备地址，请检查后分享）', 'caihong-' + self.kind + '.json', 'JSON (*.json)')
        if filename:
            try:
                Path(filename).write_text(json.dumps(self.report(), ensure_ascii=False, indent=2) + '\n')
            except OSError as error:
                self.record('error', str(error))

    def closeEvent(self, event):
        self.stop()
        if self.temporary:
            self.temporary.cleanup()
            self.temporary = None
        super().closeEvent(event)

    def reject(self):
        self.close()
