from array import array
import sys
from unittest.mock import Mock
import wave

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QColor, QImage, QKeyEvent, QPalette
from PySide6.QtWidgets import QApplication

from caihong_toolbox.testing import HardwareTestDialog, InputCanvas, microphone_levels, speaker_wave


@pytest.fixture(scope='session')
def application():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def cleanup_widgets(application):
    yield
    for widget in application.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_microphone_reports_silence_each_second(tmp_path):
    filename = tmp_path / 'mic.wav'
    samples = array('h', [0] * 48000 + [12000] * 48000 + [0] * 48000)
    if sys.byteorder != 'little':
        samples.byteswap()
    with wave.open(str(filename), 'wb') as stream:
        stream.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
        stream.writeframes(samples.tobytes())
    levels = microphone_levels(filename)
    assert len(levels) == 3
    assert levels[0]['rms'] == levels[2]['rms'] == 0
    assert levels[1]['rms'] == pytest.approx(12000 / 32768, abs=1e-6)


def test_speaker_is_bounded_and_separates_channels(tmp_path):
    filename = tmp_path / 'speaker.wav'
    speaker_wave(filename)
    with wave.open(str(filename), 'rb') as stream:
        assert stream.getnframes() == 96000
        assert stream.getnchannels() == 2
        samples = array('h', stream.readframes(96000))
        if sys.byteorder != 'little':
            samples.byteswap()
    assert max(abs(sample) for sample in samples) <= 2600
    assert not any(samples[1:96000:2])
    assert not any(samples[96000::2])


def test_escape_and_function_keys_recorded_without_text(application):
    report = Mock()
    canvas = InputCanvas('keyboard', report)
    for key in (Qt.Key.Key_Escape, Qt.Key.Key_F1, Qt.Key.Key_F4, Qt.Key.Key_F12):
        canvas.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier))
    assert [call.args[1]['qt_key'] for call in report.call_args_list] == [Qt.Key.Key_Escape, Qt.Key.Key_F1, Qt.Key.Key_F4, Qt.Key.Key_F12]
    assert all('text' not in call.args[1] for call in report.call_args_list)
    canvas.close()


def test_touch_canvas_follows_palette_but_display_card_stays_exact(application):
    canvas = InputCanvas('touch', Mock())
    palette = QPalette(canvas.palette())
    palette.setColor(QPalette.ColorRole.Base, QColor('#123456'))
    canvas.setPalette(palette)
    canvas.resize(220, 120)
    image = QImage(220, 120, QImage.Format.Format_ARGB32)
    canvas.render(image)
    assert image.pixelColor(180, 100) == QColor('#123456')
    canvas.close()
    display = InputCanvas('display', Mock())
    assert display.colors == ['#e74040', '#34b858', '#326fe5', '#ffffff', '#000000', '#808080']
    display.close()


def test_stop_releases_process_and_camera(application):
    dialog = HardwareTestDialog('camera', lambda: {}, demo=True)
    dialog.process = Mock()
    dialog.camera = Mock()
    dialog.stop()
    dialog.process.terminate.assert_called_once()
    dialog.camera.stop.assert_called_once()
    assert not dialog.running
    dialog.process = None
    dialog.close()


def test_stopped_scheduled_begin_never_runs_commands(application, monkeypatch):
    dialog = HardwareTestDialog('network', lambda: {}, demo=True)
    dialog.stop()
    monkeypatch.setattr(dialog, 'next_command', lambda: pytest.fail('command after stop'))
    dialog.begin()
    dialog.close()


def test_missing_dependency_not_a_pass(application, monkeypatch):
    dialog = HardwareTestDialog('microphone', lambda: {}, demo=True)
    dialog.running = True
    monkeypatch.setattr('caihong_toolbox.testing.shutil.which', lambda program: None)
    assert not dialog.start_process(['pw-record'], 'record')
    assert dialog.records[-1]['kind'] == 'unavailable'
    assert 'verdict' not in dialog.report()
    dialog.close()


def test_observation_does_not_stop_after_one_minute(application):
    dialog = HardwareTestDialog('display', lambda: {}, demo=True)
    dialog.running = True
    dialog.started -= 3600
    dialog.tick()
    assert dialog.running
    assert '剩余' not in dialog.status.text()
    dialog.close()


def test_microphone_start_is_untimed_and_stop_is_manual(application, monkeypatch):
    dialog = HardwareTestDialog('microphone', lambda: {}, demo=True)
    dialog.running = True
    dialog.prepare_audio = Mock()
    dialog.audio_file = 'mock.wav'
    dialog.start_process = Mock(return_value=True)
    dialog.start_audio()
    assert dialog.start_process.call_args.args[-1] is None
    assert dialog.audio_stop.isEnabled()
    dialog.process_role = 'record'
    dialog.process_timeout = Mock()
    dialog.stop_recording()
    dialog.process_timeout.assert_called_once()
    dialog.close()
