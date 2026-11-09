import copy
from collections import Counter
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import subprocess
import sys
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QBuffer, QByteArray, QEvent
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QFontMetrics, QImage,
                           QPainter, QPalette)
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QStyleOptionViewItem

from caihong_toolbox.gui import (STYLE, Window, demo_snapshot, follow_system_font,
                                 font_ratio_sizes, theme_signature)


@pytest.fixture(scope='session')
def application():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(application, monkeypatch):
    def forbidden(*arguments, **options):
        raise AssertionError('demo must not contact hardware or D-Bus')
    monkeypatch.setattr('caihong_toolbox.gui.read_snapshot', forbidden)
    monkeypatch.setattr(Window, 'dbus', forbidden)
    widget = Window(demo=True)
    widget.show()
    application.processEvents()
    yield widget
    widget.timer.stop()
    widget.close()
    widget.deleteLater()
    application.processEvents()


def test_demo_pages_are_real_widgets_and_read_only(window, application):
    assert window.pages.count() == 8
    assert '76' in window.capacity.value.text()
    for index in range(7):
        window.navigation.setCurrentRow(index)
        application.processEvents()
        assert window.pages.currentIndex() == index
    assert all(not button.isEnabled() for button in window.buttons.values())
    window.perform({'action': 'pen-feedback'})
    assert not window.busy


def test_theme_follows_palette_without_fixed_colors():
    assert not re.search(r'#[0-9a-fA-F]{3,8}\b|\brgba?\(', STYLE)
    for role in ('accent', 'placeholder-text', 'highlight', 'alternate-base',
                 'base', 'midlight'):
        assert 'palette(' + role + ')' in STYLE
    assert 'palette(mid)' not in STYLE
    assert 'QScrollArea > QWidget > QWidget' not in STYLE
    assert 'QListWidget::item' not in STYLE
    assert 'QPushButton#primary' not in STYLE


def test_resource_page_shows_demo_metrics(window, application):
    window.navigation.setCurrentRow(1)
    application.processEvents()
    assert window.pages.currentIndex() == 1
    assert window.cpu_usage.value.text() == '18.4%'
    assert window.cpu_table.rowCount() == 8
    assert window.thermal_table.rowCount() == 10
    assert '已用' in window.memory_usage.caption.text()
    assert '32.1' not in window.memory_usage.value.text()


def test_connection_badge_stays_on_one_line(window):
    assert window.connection.wordWrap() is False
    assert window.connection.contentsMargins().left() >= 12


def test_navigation_keeps_touch_row_height_without_overriding_style(window):
    option = QStyleOptionViewItem()
    index = window.navigation.model().index(0, 0)
    assert window.navigation.itemDelegate().sizeHint(option, index).height() == 48


def test_palette_change_reapplies_theme(window):
    calls = []
    window.apply_theme = lambda: calls.append(True)
    QApplication.sendEvent(window, QEvent(QEvent.Type.PaletteChange))
    assert calls == [True]


def test_other_machine_cannot_modify(window):
    snapshot = demo_snapshot()
    snapshot['device']['supported'] = False
    snapshot['service'] = True
    window.demo = False
    window.update_status(snapshot)
    assert all(not button.isEnabled() for button in window.buttons.values())


def test_missing_backend_is_read_only(window):
    window.demo = False
    window.update_status(demo_snapshot())
    assert all(not button.isEnabled() for button in window.buttons.values())
    assert '只读' in window.connection.text()


def test_service_manager_lists_and_hides_missing_accessories(window, application):
    window.navigation.setCurrentRow(6)
    application.processEvents()
    assert len(window.service_cards) == 4
    assert not window.service_cards['caihong-keyboard.service']['frame'].isHidden()
    assert not window.service_cards['caihong-pen-autoconnect.timer']['frame'].isHidden()
    snapshot = demo_snapshot()
    snapshot['keyboard'].update(available=False, pogo_attached=0)
    snapshot['pen'].update(connected=False, writing=None, paired=False, hardware_support=False)
    window.demo = False
    window.update_status(snapshot)
    assert window.navigation.isRowHidden(3)
    assert window.navigation.isRowHidden(4)
    assert window.service_cards['caihong-keyboard.service']['frame'].isHidden()
    assert window.service_cards['caihong-pen-autoconnect.timer']['frame'].isHidden()
    assert not window.service_cards['caihong-ssc.service']['frame'].isHidden()
    assert window.test_cards['keyboard'].isHidden()
    assert window.pen_tests_widget.isHidden()


def test_accessory_override_can_reveal_missing_hardware(window):
    preferences = Mock()
    preferences.contains.return_value = True
    preferences.value.return_value = True
    window.preferences = preferences
    window.demo = False
    snapshot = demo_snapshot()
    snapshot['keyboard'].update(available=False, pogo_attached=0)
    snapshot['pen'].update(connected=False, writing=None, paired=False, hardware_support=False)
    window.update_status(snapshot)
    assert not window.navigation.isRowHidden(3)
    assert not window.navigation.isRowHidden(4)
    assert not window.test_cards['keyboard'].isHidden()
    assert not window.pen_tests_widget.isHidden()


def test_pen_tests_are_on_hardware_test_page(window, application):
    window.navigation.setCurrentRow(4)
    application.processEvents()
    assert not window.pen_touch_log.isVisible()
    assert not window.pen_stop.isVisible()
    window.navigation.setCurrentRow(5)
    application.processEvents()
    assert window.pen_touch_log.isVisible()
    assert window.pen_stop.isVisible()
    pen_page_buttons = window.pages.widget(4).findChildren(type(window.pen_stop))
    assert all(button.text() != '开始笔身观察' for button in pen_page_buttons)


def test_charge_page_shows_power_protocol_and_capability(window):
    window.update_status(demo_snapshot())
    assert window.input_power.value.text() == '30.7 W'
    assert window.battery_power.value.text() == '2.3 W'
    assert 'PPS' in window.charge_protocol.value.text()
    assert window.pps_limit.value.text() == '5.0 A'
    assert 'buck 6.2 W + 双泵 24.5 W' in window.power_details.text()
    assert 'PD 3.0' in window.power_details.text()
    assert '正插' in window.power_details.text()


def test_charge_page_does_not_call_vooc_pps(window):
    snapshot = demo_snapshot()
    snapshot['adapter'].update(protocol='VOOC / SVOOC · 双泵',
                               pps_current_limit_ma=None,
                               capabilities={'available': False, 'fixed': [], 'pps': []},
                               capability_summary=None)
    window.update_status(snapshot)
    assert 'VOOC' in window.charge_protocol.value.text()
    assert window.pps_limit.value.text() == '不适用'
    assert '私有快充协议' in window.pps_limit.caption.text()


def test_poll_does_not_overwrite_pending_edits(window):
    window.start_limit.setValue(72)
    window.end_limit.setValue(85)
    snapshot = copy.deepcopy(window.snapshot)
    window.update_status(snapshot)
    assert window.start_limit.value() == 72
    assert window.end_limit.value() == 85


def test_experimental_limit_requires_confirmation(window, monkeypatch):
    calls = []
    monkeypatch.setattr(window, 'message', lambda *arguments: False)
    monkeypatch.setattr(window, 'perform', calls.append)
    window.apply_limits()
    assert calls == []


def test_unverified_pen_combination_requires_confirmation(window, monkeypatch):
    window.writing.setChecked(True)
    window.level.setValue(5)
    calls = []
    monkeypatch.setattr(window, 'message', lambda *arguments: False)
    monkeypatch.setattr(window, 'perform', calls.append)
    window.apply_writing()
    assert calls == []


def test_external_state_changes_update_unedited_controls(window):
    snapshot = copy.deepcopy(window.snapshot)
    snapshot['keyboard']['automatic'] = True
    snapshot['keyboard']['compensation_running'] = True
    snapshot['keyboard']['media_keys'] = 0
    window.update_status(snapshot)
    assert not hasattr(window, 'compensation')
    assert 'keyboard-compensation' not in window.buttons
    assert window.media.isChecked()
    assert '运行中' in window.keyboard_details.text()
    snapshot['keyboard']['compensation_running'] = False
    snapshot['keyboard']['media_keys'] = 1
    window.update_status(snapshot)
    assert not window.media.isChecked()
    assert '等待蓝牙' in window.keyboard_details.text()


def test_preview_process_exits_cleanly(tmp_path):
    output = tmp_path / 'pen.png'
    result = subprocess.run([sys.executable, '-m', 'caihong_toolbox', '--demo',
                             '--page', 'pen', '--screenshot', str(output)],
                            cwd=Path(__file__).resolve().parent.parent,
                            env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen', 'PYTHONDONTWRITEBYTECODE': '1'},
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert output.is_file()


def test_touch_start_requires_real_undocked_confirmation(window, monkeypatch):
    calls = []
    monkeypatch.setattr(window, 'message', lambda *arguments: False)
    monkeypatch.setattr(window, 'perform', calls.append)
    window.start_pen_touch()
    assert calls == []
    window.pen_undocked.setChecked(True)
    window.start_pen_touch()
    assert calls == [{'action': 'pen-touch-start', 'undocked': True}]


def test_raw_film_does_not_trigger_action_and_events_not_replayed(window, monkeypatch):
    calls = []
    monkeypatch.setattr(window, 'dispatch_pen_action', calls.append)
    window.pen_session = 'ours'
    events = [dict(id=1, kind='film', elapsed=1.1, value='film 24', hex=''),
              dict(id=2, kind='double-tap', elapsed=1.2, value='double', hex='01')]
    pen = {'touch': dict(session='ours', ready=True, running=True, events=events)}
    window.update_pen_touch(pen)
    window.update_pen_touch(pen)
    assert calls == [events[1]]
    window.pen_session = 'other'
    window.pen_seen = 0
    window.update_pen_touch(pen)
    assert calls == [events[1]]


def test_live_double_dispatches_only_whitelisted_action(window, monkeypatch):
    calls = []
    monkeypatch.setattr(window, 'perform', calls.append)
    window.demo = False
    window.pen_action.setCurrentIndex(window.pen_action.findData('feedback'))
    window.dispatch_pen_action({'time': datetime.now(timezone.utc).isoformat()})
    window.dispatch_pen_action({'time': datetime.now(timezone.utc).isoformat()})
    assert calls == [{'action': 'pen-feedback'}]


def test_all_test_previews_are_isolated(window, application, monkeypatch):
    def forbidden(*arguments, **options):
        raise AssertionError('preview must never access hardware')
    monkeypatch.setattr('caihong_toolbox.testing.shutil.which', forbidden)
    monkeypatch.setattr('caihong_toolbox.testing.Path.glob', forbidden)
    for kind in window.test_buttons:
        window.open_test(kind)
        application.processEvents()
        assert window.test_dialog.demo
        assert not window.test_dialog.running
        assert not window.test_dialog.timer.isActive()
        assert all(not button.isEnabled() for button in window.test_dialog.actions)
        window.test_dialog.close()


def test_pen_preferences_not_written_in_demo(window):
    assert window.preferences is None
    window.save_pen_action()


def test_slide_is_a_manual_switch_not_a_timed_plan(window, monkeypatch):
    calls = []
    monkeypatch.setattr(window, 'perform', calls.append)
    window.pen_slide_enabled.setChecked(True)
    window.apply_pen_slide()
    window.pen_slide_enabled.setChecked(False)
    window.apply_pen_slide()
    assert calls == [{'action': 'pen-slide-set', 'enabled': True}, {'action': 'pen-slide-set', 'enabled': False}]
    assert not hasattr(window, 'pen_seconds')


def test_double_and_slide_can_be_observed_together(window, monkeypatch):
    calls = []
    monkeypatch.setattr(window, 'dispatch_pen_action', calls.append)
    window.pen_session = 'ours'
    window.update_pen_touch({'touch': dict(session='ours', ready=True, running=True, slide_requested=True,
                                          events=[dict(id=1, kind='double-tap', elapsed=1, value='double', hex='01')])})
    assert len(calls) == 1


def test_authorization_does_not_pause_observation_heartbeat(window, monkeypatch):
    calls = []
    monkeypatch.setattr(window, 'dbus', lambda *arguments: calls.append(arguments))
    window.demo, window.busy, window.polling = False, True, False
    window.refresh()
    assert calls == [('GetStatus',)]


def dominant_color(widget):
    image = widget.grab().toImage()
    colors = Counter(image.pixelColor(x, y).name()
                     for y in range(image.height()) for x in range(image.width()))
    return colors.most_common(1)[0][0]


def test_rendered_fills_follow_runtime_palette_switch(window, application):
    original = QPalette(application.palette())
    card = window.findChildren(QFrame, 'card')[0]
    try:
        for base, window_color in (('#101010', '#181818'), ('#fafafa', '#f0f0f0')):
            palette = QPalette(original)
            palette.setColor(QPalette.ColorRole.Base, QColor(base))
            palette.setColor(QPalette.ColorRole.Window, QColor(window_color))
            application.setPalette(palette)
            application.processEvents()
            assert dominant_color(card) == base
    finally:
        application.setPalette(original)
        application.processEvents()


def test_accent_role_updates_emphasis_text(window, application):
    original = QPalette(application.palette())
    brand = window.findChild(QLabel, 'brand')
    try:
        palette = QPalette(original)
        palette.setColor(QPalette.ColorRole.Accent, QColor('#ff00ff'))
        application.setPalette(palette)
        application.processEvents()
        colors = Counter(brand.grab().toImage().pixelColor(x, y).name()
                         for y in range(brand.grab().toImage().height())
                         for x in range(brand.grab().toImage().width()))
        assert '#ff00ff' in colors
    finally:
        application.setPalette(original)
        application.processEvents()


def test_title_and_button_sizes_follow_system_font(application):
    original = QFont(application.font())
    try:
        font = QFont(original)
        font.setPointSizeF(original.pointSizeF() + 10)
        application.setFont(font)
        widget = Window(demo=True)
        widget.show()
        application.processEvents()
        title = widget.findChildren(QLabel, 'title')[0]
        expected = font_ratio_sizes(font)['title']
        assert title.font().pointSizeF() == pytest.approx(expected, abs=0.3)
        assert all(button.minimumHeight() >= QFontMetrics(font).height() + 20
                   for button in widget.buttons.values())
        option = QStyleOptionViewItem()
        option.font = font
        option.fontMetrics = QFontMetrics(font)
        index = widget.navigation.model().index(0, 0)
        assert widget.navigation.itemDelegate().sizeHint(option, index).height() > 48
        widget.close()
        widget.deleteLater()
        application.processEvents()
    finally:
        application.setFont(original)
        application.processEvents()


def test_existing_window_reapplies_after_system_font_change(window, application):
    original = QFont(application.font())
    title = window.findChildren(QLabel, 'title')[0]
    before = title.font().pointSizeF()
    try:
        font = QFont(original)
        font.setPointSizeF(original.pointSizeF() + 3)
        application.setFont(font)
        application.processEvents()
        assert title.font().pointSizeF() > before
        assert window._theme_signature == theme_signature(application)
    finally:
        application.setFont(original)
        application.processEvents()


def test_cjk_falls_back_to_simplified_chinese(application):
    families = follow_system_font(application.font()).families()
    available = set(__import__('PySide6.QtGui', fromlist=['QFontDatabase']).QFontDatabase.families())
    candidates = set(families) & {'Noto Sans CJK SC', 'Source Han Sans SC', 'Noto Sans SC'}
    if available & candidates:
        assert candidates
    assert not any(name in families for name in ('Noto Sans CJK KR', 'Noto Sans CJK JP',
                                                 'Noto Sans CJK TC'))


def glyph_signature(font):
    face = QFont(font)
    face.setPixelSize(64)
    image = QImage(120, 120, QImage.Format.Format_ARGB32)
    image.fill(QColor('white'))
    painter = QPainter(image)
    painter.setFont(face)
    painter.setPen(QColor('black'))
    painter.drawText(image.rect(), Qt.AlignmentFlag.AlignCenter, '直骨今与')
    painter.end()
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    assert image.save(buffer, 'PNG')
    buffer.close()
    return bytes(data)


def test_cjk_glyphs_use_simplified_forms(application):
    families = set(QFontDatabase.families())
    if 'Noto Sans CJK SC' not in families or 'Noto Sans CJK KR' not in families:
        pytest.skip('需要同时安装 Noto Sans CJK SC 与 KR 才能对照字形')
    window_font = follow_system_font(application.font())
    simplified = glyph_signature(QFont('Noto Sans CJK SC'))
    korean = glyph_signature(QFont('Noto Sans CJK KR'))
    if simplified == korean:
        pytest.skip('当前字体环境没有可区分的 SC/KR 字形')
    assert glyph_signature(window_font) == simplified
