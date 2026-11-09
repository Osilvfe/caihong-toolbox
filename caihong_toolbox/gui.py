import argparse
import copy
from datetime import datetime, timezone
from functools import lru_cache, partial
import json
import os
from pathlib import Path
import sys

from PySide6.QtCore import QCoreApplication, QEvent, QSettings, Qt, QTimer
from PySide6.QtDBus import QDBusConnection, QDBusMessage, QDBusPendingCallWatcher
from PySide6.QtGui import (QFont, QFontDatabase, QFontInfo, QFontMetrics,
                           QIcon, QPalette)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFormLayout, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QScroller, QSizePolicy,
    QSpinBox, QStackedWidget, QStyledItemDelegate, QVBoxLayout, QWidget,
)

from . import __version__
from .hardware import read_snapshot, validate_request
from .testing import HardwareTestDialog, TESTS


STYLE = '''
QLabel#brand { font-size: @brand@pt; font-weight: 700; color: palette(accent); }
QLabel#title { font-size: @title@pt; font-weight: 700; }
QLabel#heading { font-size: @heading@pt; font-weight: 600; }
QLabel#muted { color: palette(placeholder-text); }
QLabel#metric { font-size: @metric@pt; font-weight: 600; }
QLabel#badge { background: palette(alternate-base); color: palette(accent); border: 1px solid palette(midlight); border-radius: 10px; }
QWidget#sidebar { background: palette(alternate-base); border-right: 1px solid palette(midlight); }
QFrame#card { background: palette(base); border: 1px solid palette(midlight); border-radius: 14px; }
QLabel#notice { background: palette(alternate-base); color: palette(text); border: 1px solid palette(midlight); border-radius: 9px; padding: 12px; }
QLabel#warning { background: palette(alternate-base); color: palette(text); border: 1px solid palette(accent); border-radius: 9px; padding: 12px; font-weight: 600; }
QListWidget { background: transparent; border: 0; outline: 0; }
QCheckBox { spacing: 10px; min-height: 34px; }
QScrollArea { border: 0; background: transparent; }
QProgressBar { border: 1px solid palette(midlight); border-radius: 4px; height: 7px; background: palette(base); }
QProgressBar::chunk { background: palette(highlight); border-radius: 4px; }
QPlainTextEdit { border: 1px solid palette(midlight); border-radius: 8px; padding: 12px; }
'''


def label(value, name=None):
    widget = QLabel(value)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    if name:
        widget.setObjectName(name)
    return widget


def formatted(value, divisor=1, suffix='', precision=1):
    return '—' if value is None else f'{value / divisor:.{precision}f}{suffix}'


THEME_EVENTS = frozenset((
    QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange,
    QEvent.Type.FontChange, QEvent.Type.ApplicationFontChange,
    QEvent.Type.StyleChange,
))

@lru_cache(maxsize=1)
def simplified_chinese_families():
    families = set(QFontDatabase.families())
    return tuple(name for name in ('Noto Sans CJK SC', 'Source Han Sans SC',
                                   'Noto Sans SC', 'WenQuanYi Zen Hei')
                 if name in families)


def follow_system_font(font):
    followed = QFont(font)
    families = list(followed.families()) or [followed.family()]
    for family in simplified_chinese_families():
        if family not in families:
            families.append(family)
    followed.setFamilies(families)
    return followed


PALETTE_ROLES = (
    QPalette.ColorRole.Window, QPalette.ColorRole.WindowText,
    QPalette.ColorRole.Base, QPalette.ColorRole.AlternateBase,
    QPalette.ColorRole.Text, QPalette.ColorRole.Button,
    QPalette.ColorRole.ButtonText, QPalette.ColorRole.Highlight,
    QPalette.ColorRole.HighlightedText, QPalette.ColorRole.Midlight,
    QPalette.ColorRole.Link, QPalette.ColorRole.Accent,
    QPalette.ColorRole.PlaceholderText,
)


def theme_signature(application):
    palette = application.palette()
    return (
        application.font().toString(),
        application.style().objectName(),
        tuple(palette.color(role).rgba() for role in PALETTE_ROLES),
        follow_system_font(application.font()).families(),
    )


def font_ratio_sizes(font):
    points = font.pointSizeF()
    if points <= 0:
        points = QFontInfo(font).pointSizeF()
    points = max(points, 6.0)
    return {name: max(points * ratio, 6.0)
            for name, ratio in (('brand', 1.875), ('title', 2.175),
                                ('heading', 1.3), ('metric', 2.4))}


class NavigationDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        size.setHeight(max(48, option.fontMetrics.height() + 22))
        return size


class Metric(QFrame):
    def __init__(self, title, caption):
        super().__init__()
        self.setObjectName('card')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.addWidget(label(title, 'muted'))
        self.value = label('—', 'metric')
        layout.addWidget(self.value)
        self.caption = label(caption, 'muted')
        layout.addWidget(self.caption)


def demo_snapshot():
    return {
        'demo': True, 'service': False,
        'device': {'supported': True, 'model': 'OnePlus Pad Pro · Caihong',
                   'kernel': '7.3 · 演示数据', 'architecture': 'aarch64'},
        'battery': {'capacity': 76, 'temp': 325, 'voltage_now': 4020000, 'current_now': 580000,
                    'power_uw': 2331600, 'status': 'Charging', 'state_of_health': 100, 'cycle_count': 1,
                    'charge_full': 9492000, 'charge_full_design': 9492000, 'capacity_ratio': 100,
                    'charge_behaviour': 'auto', 'charge_control_start_threshold': 95,
                    'charge_control_end_threshold': 100, 'control_available': True,
                    'controller': {'raw_soc': '73', 'pending': '0', 'last_rc': '0'}},
        'adapter': {'online': 1, 'usb_type': 'Unknown [SDP] DCP PD_PPS',
                    'active_type': 'SDP', 'voltage_now': 8900000, 'current_now': 700000,
                    'voltage_max': 9400000, 'current_max': 5000000,
                    'input_current_limit': 2000000, 'buck_power_uw': 6230000,
                    'pump_power_uw': 24500000, 'input_power_uw': 30730000,
                    'pumps_online': 2, 'pps_current_limit_ma': 5000,
                    'protocol': 'USB PD PPS · 双泵',
                    'capability_summary': 'PPS 5.0–11.0 V / 5.0 A；PPS 5.0–20.0 V / 5.0 A；固定最高 20.0 V / 5.0 A',
                    'pd': {'supported': True, 'revision': '3.0', 'power_role': 'sink',
                           'data_role': 'device', 'orientation': 'normal',
                           'operation_mode': 'usb_power_delivery'},
                    'pumps': [{'name': 'sc8547-1-6f', 'online': 1, 'power_uw': 12300000},
                              {'name': 'sc8547-0-6f', 'online': 1, 'power_uw': 12200000}]},
        'keyboard': {'available': True, 'pogo_attached': 0, 'pogo_touchpad': None, 'media_keys': 0,
                     'compensation_gate': 0, 'compensation_running': False, 'touchpad_target': None},
        'pen': {'connected': True, 'controller_available': True, 'scan_mode': 1,
                'vibrator_request': 0, 'writing': None, 'feedback_available': True,
                'hardware_support': True, 'paired': True},
        'services': {'error': None, 'items': [
            {'unit': 'caihong-keyboard.service', 'title': '键盘自动兼容',
             'description': '管理蓝牙键盘的 F4 补丁和触控板偏好。', 'requires': 'keyboard',
             'protected': False, 'manageable': True, 'enabled': True, 'active': True,
             'state': 'active/running', 'unit_file_state': 'enabled'},
            {'unit': 'caihong-pen-autoconnect.timer', 'title': '手写笔自动重连',
             'description': '按记录恢复已配对笔的连接和扫描模式。', 'requires': 'pen',
             'protected': False, 'manageable': True, 'enabled': True, 'active': True,
             'state': 'active/waiting', 'unit_file_state': 'enabled'},
            {'unit': 'caihong-ssc.service', 'title': '传感器子系统',
             'description': '运行 Qualcomm Sensor Subsystem 监听器。', 'requires': None,
             'protected': False, 'manageable': True, 'enabled': True, 'active': True,
             'state': 'active/running', 'unit_file_state': 'enabled'},
            {'unit': 'caihong-toolbox.service', 'title': '工具箱后台',
             'description': '提供只读状态和经 Polkit 授权的设置接口。', 'requires': None,
             'protected': True, 'manageable': False, 'enabled': True, 'active': True,
             'state': 'active/running', 'unit_file_state': 'static'},
        ]},
        'diagnostics': {'说明': '界面演示，不读取设备、不调用后台、不发送任何设置请求。'},
    }


class Window(QMainWindow):
    def __init__(self, demo=False):
        super().__init__()
        self.demo = demo
        self.snapshot = {}
        self.snapshot_time = None
        self.busy = False
        self.polling = False
        self.dirty = set()
        self.syncing = False
        self.generation = 0
        self.buttons = {}
        self.pen_session = None
        self.pen_seen = 0
        self.pen_last_action = None
        self.test_dialog = None
        self.service_cards = {}
        self.service_dirty = set()
        self.service_syncing = False
        self.accessory_syncing = False
        self.keyboard_visible = True
        self.pen_visible = True
        self.preferences = None if demo else QSettings('Caihong', 'Toolbox')
        self.setWindowTitle('Caihong Toolbox')
        self.setMinimumSize(820, 590)
        self.resize(1100, 800)
        icon = QIcon.fromTheme('caihong-toolbox')
        if icon.isNull():
            icon = QIcon(str(Path(__file__).resolve().parent.parent / 'resources/caihong-toolbox.svg'))
        self.setWindowIcon(icon)
        self._applying_theme = False
        self._theme_signature = None
        application = QApplication.instance()
        application.fontChanged.connect(self.apply_theme)
        application.paletteChanged.connect(self.apply_theme)
        self.apply_theme()
        shell = QWidget()
        shell.setObjectName('shell')
        self.setCentralWidget(shell)
        horizontal = QHBoxLayout(shell)
        horizontal.setContentsMargins(0, 0, 0, 0)
        horizontal.setSpacing(0)
        sidebar = QWidget()
        sidebar.setObjectName('sidebar')
        sidebar.setFixedWidth(205)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(18, 28, 18, 22)
        side.addWidget(label('Caihong', 'brand'))
        side.addWidget(label('TOOLBOX', 'muted'))
        side.addSpacing(25)
        self.navigation = QListWidget()
        self.navigation.setItemDelegate(NavigationDelegate(self.navigation))
        for title, icon_name in (('设备总览', 'computer'), ('充电与电池', 'battery'),
                                 ('键盘与触控板', 'input-keyboard'), ('手写笔', 'draw-freehand'),
                                 ('硬件测试', 'applications-engineering'),
                                 ('服务管理', 'preferences-system-services'),
                                 ('诊断信息', 'document-properties')):
            self.navigation.addItem(QListWidgetItem(QIcon.fromTheme(icon_name), title))
        side.addWidget(self.navigation)
        side.addWidget(label('为 Caihong 而造\n设置有依据，状态可追溯', 'muted'))
        side.addSpacing(12)
        side.addWidget(label('本地预览版  ' + __version__, 'muted'))
        horizontal.addWidget(sidebar)
        content = QVBoxLayout()
        content.setContentsMargins(28, 24, 28, 20)
        top = QHBoxLayout()
        self.connection = label('正在读取设备状态…', 'badge')
        self.connection.setWordWrap(False)
        self.connection.setContentsMargins(12, 7, 12, 7)
        self.connection.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        top.addWidget(self.connection)
        top.addStretch()
        refresh = QPushButton('刷新状态')
        refresh.clicked.connect(self.refresh)
        top.addWidget(refresh)
        content.addLayout(top)
        self.pages = QStackedWidget()
        content.addWidget(self.pages, 1)
        self.result = label('读取无需提权；修改设置时由系统弹出管理员授权。', 'muted')
        content.addWidget(self.result)
        horizontal.addLayout(content, 1)
        self.build_overview()
        self.build_charge()
        self.build_keyboard()
        self.build_pen()
        self.build_tests()
        self.build_services()
        self.build_diagnostics()
        for control, section in ((self.bypass, 'charge'), (self.media, 'keyboard'), (self.writing, 'pen')):
            control.toggled.connect(partial(self.mark_dirty, section))
        for control, section in ((self.start_limit, 'charge'), (self.end_limit, 'charge'), (self.level, 'pen')):
            control.valueChanged.connect(partial(self.mark_dirty, section))
        self.tool.currentIndexChanged.connect(partial(self.mark_dirty, 'pen'))
        self.touchpad.currentIndexChanged.connect(partial(self.mark_dirty, 'keyboard'))
        self.navigation.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.navigation.setCurrentRow(0)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        if not demo:
            self.timer.start(3000)
        QTimer.singleShot(0, self.refresh)

    def page(self, title, subtitle):
        area = QScrollArea()
        area.setWidgetResizable(True)
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 20, 5, 12)
        layout.setSpacing(16)
        layout.addWidget(label(title, 'title'))
        layout.addWidget(label(subtitle, 'muted'))
        area.setWidget(widget)
        QScroller.grabGesture(area.viewport(), QScroller.ScrollerGestureType.TouchGesture)
        self.pages.addWidget(area)
        return layout

    def apply_theme(self):
        if self._applying_theme:
            return
        application = QApplication.instance()
        signature = theme_signature(application)
        if signature == self._theme_signature:
            return
        self._applying_theme = True
        try:
            self._theme_signature = signature
            font = follow_system_font(application.font())
            sizes = font_ratio_sizes(font)
            sheet = STYLE
            for name, value in sizes.items():
                sheet = sheet.replace('@%s@' % name, f'{value:.1f}')
            self.setStyleSheet('')
            self.setFont(font)
            self.setStyleSheet(sheet)
            height = max(44, QFontMetrics(font).height() + 20)
            for button in getattr(self, 'buttons', {}).values():
                emphasis = QFont(font)
                emphasis.setWeight(QFont.Weight.DemiBold)
                button.setFont(emphasis)
                button.setMinimumHeight(height)
            diagnostics = getattr(self, 'diagnostics', None)
            if diagnostics is not None:
                diagnostics.setFont(follow_system_font(QFontDatabase.systemFont(
                    QFontDatabase.SystemFont.FixedFont)))
            navigation = getattr(self, 'navigation', None)
            if navigation is not None:
                navigation.scheduleDelayedItemsLayout()
        finally:
            self._applying_theme = False

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in THEME_EVENTS:
            self.apply_theme()

    def card(self, parent, title, description):
        frame = QFrame()
        frame.setObjectName('card')
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        layout.addWidget(label(title, 'heading'))
        layout.addWidget(label(description, 'muted'))
        parent.addWidget(frame)
        return layout

    def button(self, parent, title, action, callback):
        button = QPushButton(title)
        button.setObjectName('primary')
        font = follow_system_font(QApplication.font())
        font.setWeight(QFont.Weight.DemiBold)
        button.setFont(font)
        button.setMinimumHeight(max(44, QFontMetrics(font).height() + 20))
        button.clicked.connect(callback)
        button.setEnabled(False)
        parent.addWidget(button, alignment=Qt.AlignmentFlag.AlignLeft)
        self.buttons[action] = button
        return button

    def build_overview(self):
        layout = self.page('设备总览', '在一个地方了解设备，也清楚哪些读数仍需验证。')
        self.device_info = label('—', 'notice')
        layout.addWidget(self.device_info)
        grid = QGridLayout()
        grid.setSpacing(14)
        self.capacity = Metric('当前电量', '桌面电量与固件原始 SOC 可能不同')
        self.temperature = Metric('电池温度', '来自电池固件，不替代温控保护')
        self.health = Metric('固件报告健康度', '不是独立测量的容量健康结论')
        self.source = Metric('适配器状态', '不根据功率猜测 VOOC 是否运行')
        for index, metric in enumerate((self.capacity, self.temperature, self.health, self.source)):
            grid.addWidget(metric, index // 2, index % 2)
        layout.addLayout(grid)
        self.charge_progress = QProgressBar()
        self.charge_progress.setTextVisible(False)
        self.charge_progress.setMaximumHeight(7)
        layout.addWidget(self.charge_progress)
        self.overview_detail = label('—', 'notice')
        layout.addWidget(self.overview_detail)
        layout.addWidget(label('初版仅接入有依据的设备接口。不自动刷内核，不改笔扫描模式，不主动配对或重连附件。', 'muted'))
        layout.addStretch()

    def build_charge(self):
        layout = self.page('充电与电池', '控制充电行为，保留安全边界与原始依据。')
        self.battery_details = label('—', 'notice')
        layout.addWidget(self.battery_details)
        power = self.card(layout, '功率与协议', '只读汇总供电路径、USB-PD/PPS 能力与当前活动电荷泵；不修改协商结果。')
        grid = QGridLayout()
        grid.setSpacing(12)
        self.input_power = Metric('估算输入功率', '活动 USB/buck 与电荷泵路径的电压×电流求和，不是外部功率计')
        self.battery_power = Metric('电池端功率', '固件报告的电池侧功率，不等于适配器输入功率')
        self.charge_protocol = Metric('识别到的协议', '综合 USB 类型、PD 能力与活动电荷泵；未知时不猜测')
        self.pps_limit = Metric('PPS 电流上限', '适配器在此电压范围公布的 APDO 上限，不代表当前已取满')
        for index, metric in enumerate((self.input_power, self.battery_power,
                                        self.charge_protocol, self.pps_limit)):
            grid.addWidget(metric, index // 2, index % 2)
        power.addLayout(grid)
        self.power_details = label('—', 'notice')
        power.addWidget(self.power_details)
        bypass = self.card(layout, '停充供电', '停止向电池充电，保留适配器供电；不等于电池硬件隔离，负载较高时电池仍可能补电。')
        self.bypass = QCheckBox('请求停充供电')
        bypass.addWidget(self.bypass)
        self.button(bypass, '应用供电模式', 'charge-bypass', lambda: self.perform(
            {'action': 'charge-bypass', 'enabled': self.bypass.isChecked()}))
        limits = self.card(layout, '充电阀门 · 实验', '按固件原始 SOC 判断；完整跨阈值、睡眠及长时稳定性尚未验证。停止阈值为 100 时不额外限充。')
        form = QFormLayout()
        self.start_limit, self.end_limit = QSpinBox(), QSpinBox()
        for control in (self.start_limit, self.end_limit):
            control.setRange(0, 100)
            control.setSuffix(' %')
        self.start_limit.setValue(95)
        self.end_limit.setValue(100)
        form.addRow('低于此值恢复充电', self.start_limit)
        form.addRow('达到此值停止充电', self.end_limit)
        limits.addLayout(form)
        self.button(limits, '应用阈值…', 'charge-limit', self.apply_limits)
        self.button(limits, '恢复自动充电 / 95–100%', 'charge-reset', lambda: self.perform({'action': 'charge-reset'}))
        layout.addWidget(label('修改只应用于当前运行期，不保存开机策略。失败可能已部分应用，不能将失败提示当作自动回滚。', 'warning'))
        layout.addStretch()

    def build_keyboard(self):
        layout = self.page('键盘与触控板', '兼顾磁吸与蓝牙连接，保留手动断开的选择。')
        self.keyboard_details = label('—', 'notice')
        layout.addWidget(self.keyboard_details)
        compensation = self.card(layout, '蓝牙 F4 自动兼容', '系统默认启用：蓝牙连接就绪后自动接管。标准 F 键优先时，单按 F4 保持触控板目标，Fn+F4 切换；图标优先时两者互换。磁吸时使用原有 Pogo 路径；不主动配对或重连。')
        compensation.addWidget(label('已知限制：按 F4 后仍有约 0.7 秒键盘端触控上报空档。后台退出时请求恢复触控板开启。', 'warning'))
        settings = self.card(layout, '输入偏好', '媒体优先仅调整已适配蓝牙键盘的顶排；不覆盖桌面快捷键，不伪造输入。')
        self.media = QCheckBox('标准 F 键优先，Fn 执行图标功能')
        self.media.setChecked(True)
        settings.addWidget(self.media)
        self.button(settings, '应用顶排偏好', 'keyboard-media', lambda: self.perform(
            {'action': 'keyboard-media', 'enabled': not self.media.isChecked()}))
        self.touchpad = QComboBox()
        self.touchpad.addItems(['触控板开启', '触控板关闭'])
        settings.addWidget(self.touchpad)
        self.button(settings, '设置触控板目标状态', 'keyboard-touchpad', lambda: self.perform(
            {'action': 'keyboard-touchpad', 'enabled': self.touchpad.currentIndex() == 0}))
        layout.addStretch()

    def build_pen(self):
        layout = self.page('手写笔', '恢复纸感反馈，也让尚未完成的功能保持透明。')
        self.pen_details = label('—', 'notice')
        layout.addWidget(self.pen_details)
        writing = self.card(layout, '书写纸感', '原厂笔端 BLE 配置 + 触控器门控。仅铅笔三级已完成物理对照，其他组合来自原厂协议，尚待实测。')
        self.writing = QCheckBox('启用书写纸感')
        writing.addWidget(self.writing)
        form = QFormLayout()
        self.tool = QComboBox()
        for title, code in (('铅笔', 'pencil'), ('橡皮', 'eraser'), ('圆珠笔', 'ballpoint'), ('钢笔', 'fountain')):
            self.tool.addItem(title, code)
        self.level = QSpinBox()
        self.level.setRange(1, 5)
        self.level.setValue(3)
        form.addRow('触感工具', self.tool)
        form.addRow('反馈强度', self.level)
        writing.addLayout(form)
        self.button(writing, '应用书写反馈', 'pen-writing', self.apply_writing)
        tests = QPushButton('打开手写笔功能测试 → 硬件测试')
        tests.clicked.connect(lambda: self.navigation.setCurrentRow(4))
        layout.addWidget(tests, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(label('短振、笔尖画板与笔身触控观察已移至“硬件测试”页；本页只保留书写反馈设置。', 'muted'))
        layout.addWidget(label('断联、休眠或控制器状态失效时撤销书写反馈，不自动恢复。关闭窗口停止笔身观察，但不会关闭此前显式启用的书写反馈。', 'notice'))
        layout.addStretch()

    def build_pen_tests(self, layout):
        self.pen_tests_widget = QWidget()
        target = QVBoxLayout(self.pen_tests_widget)
        target.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.pen_tests_widget)
        target.addWidget(label('手写笔测试', 'notice'))
        feedback = self.card(target, '功能短振', '请先移开磁吸、握住笔，并让笔尖靠近屏幕唤醒。只发送已验证的原厂固定短振包，不开放任意波形。')
        self.button(feedback, '短振一次', 'pen-feedback', lambda: self.perform({'action': 'pen-feedback'}))
        touch = self.card(target, '笔身触控 · 实验观察与动作', '已收到过专用双击通知，但识别尚不稳定。请在笔身前端平面触控区轻双击，逐步调整位置和节奏；尚无证据证明此前只是点击位置不对。笔尖书写与笔身触控是两条不同路径。')
        touch.addWidget(label('开始前请将笔从磁吸取下并唤醒。窗口将订阅厂商电量、双击及可用的调试通知；核对新鲜固件回复后发送真实离座和普通模式请求。不重连、不改扫描或振动，不伪造吸附状态回滚。', 'warning'))
        self.pen_undocked = QCheckBox('我确认笔已取下，允许本次观察初始化')
        touch.addWidget(self.pen_undocked)
        form = QFormLayout()
        self.pen_action = QComboBox()
        for title, action in (('仅计数与界面提示', 'observe'), ('短振一次', 'feedback'), ('切换书写纸感（当前工具与强度）', 'writing-toggle')):
            self.pen_action.addItem(title, action)
        if self.preferences:
            index = self.pen_action.findData(self.preferences.value('pen/double_action', 'observe'))
            self.pen_action.setCurrentIndex(max(0, index))
        form.addRow('双击行为', self.pen_action)
        touch.addLayout(form)
        save = QPushButton('保存双击行为偏好')
        save.setEnabled(not self.demo)
        save.clicked.connect(self.save_pen_action)
        touch.addWidget(save)
        self.button(touch, '开始笔身观察', 'pen-touch-start', self.start_pen_touch)
        touch.addWidget(label('自由观察：没有倒计时、动作顺序或自动分段。双击、滑动和静置都由你决定。开始观察不会修改滑动开关；需要时手动应用，停止或关闭窗口时会请求关闭本窗口启用的滑动。', 'notice'))
        self.pen_slide_enabled = QCheckBox('启用笔身滑动')
        self.pen_slide_enabled.toggled.connect(partial(self.mark_dirty, 'slide'))
        touch.addWidget(self.pen_slide_enabled)
        self.button(touch, '应用滑动开关', 'pen-slide-set', self.apply_pen_slide)
        self.pen_stop = QPushButton('停止笔身观察')
        self.pen_stop.setEnabled(False)
        self.pen_stop.clicked.connect(self.stop_pen_touch)
        touch.addWidget(self.pen_stop)
        self.pen_touch_status = label('未启动；打开页面不会订阅或初始化笔。', 'notice')
        touch.addWidget(self.pen_touch_status)
        self.pen_action_result = label('动作在本窗口观察期间生效，不后台常驻、不注入桌面快捷键。', 'muted')
        touch.addWidget(self.pen_action_result)
        self.pen_touch_log = QPlainTextEdit()
        self.pen_touch_log.setReadOnly(True)
        self.pen_touch_log.setMinimumHeight(170)
        self.pen_touch_log.document().setMaximumBlockCount(256)
        touch.addWidget(self.pen_touch_log)
        copy_log = QPushButton('复制笔身观察记录')
        copy_log.clicked.connect(lambda: QApplication.clipboard().setText(json.dumps(
            self.snapshot.get('pen', {}).get('touch', {}), ensure_ascii=False, indent=2)))
        touch.addWidget(copy_log)
        touch.addWidget(label('专用双击通知 01 可触发上面定义的动作；film 只作原始日志。滑动显示本支笔的滚轮/按键，不自动判定方向，也不配置滑动动作。只想看日志时请选择“仅计数与界面提示”。', 'muted'))

    def build_tests(self):
        layout = self.page('硬件测试', '按需启动、自由观察、随时停止。不预设动作顺序，不自动评定通过或失败。')
        layout.addWidget(label('不会自动写盘压测、切换充电模式、刷内核、重连附件或改显示模式。音视频测试占用对应设备，测试前请关闭其他采集应用。', 'warning'))
        self.test_buttons = {}
        self.test_cards = {}
        for kind, (title, description) in TESTS.items():
            group = self.card(layout, title, description)
            button = QPushButton('打开测试' if not self.demo else '预览测试（不访问硬件）')
            button.clicked.connect(partial(self.open_test, kind))
            group.addWidget(button, alignment=Qt.AlignmentFlag.AlignLeft)
            self.test_buttons[kind] = button
            self.test_cards[kind] = group.parentWidget()
            if kind == 'touch':
                self.build_pen_tests(layout)
        self.pen_settings_button = QPushButton('书写纸感设置 → 手写笔页')
        self.pen_settings_button.clicked.connect(lambda: self.navigation.setCurrentRow(3))
        layout.addWidget(self.pen_settings_button)
        layout.addStretch()

    def build_services(self):
        layout = self.page('服务管理', '只管理 caihong-*.service 与 caihong-*.timer；系统单元不能通过本页操作。')
        layout.addWidget(label('启用会同时启动并设置开机自启；停用会停止当前运行。工具箱后台和启动成功标记受保护，只显示状态。', 'warning'))
        accessories = self.card(layout, '附件入口', '默认按附件证据隐藏未使用的键盘和手写笔页面；可在这里手动覆盖。')
        self.show_keyboard = QCheckBox('显示键盘与触控板入口')
        self.show_pen = QCheckBox('显示手写笔入口')
        self.show_keyboard.toggled.connect(lambda checked: self.set_accessory_override('keyboard', checked))
        self.show_pen.toggled.connect(lambda checked: self.set_accessory_override('pen', checked))
        accessories.addWidget(self.show_keyboard)
        accessories.addWidget(self.show_pen)
        self.services_status = label('正在读取服务状态…', 'notice')
        layout.addWidget(self.services_status)
        self.services_layout = QVBoxLayout()
        self.services_layout.setSpacing(12)
        layout.addLayout(self.services_layout)
        layout.addStretch()

    def update_services(self, payload):
        if not isinstance(payload, dict):
            self.services_status.setText('后台未返回服务状态；当前无法管理服务。')
            return
        error = payload.get('error')
        items = payload.get('items') or []
        if error:
            self.services_status.setText('服务状态不可用：' + error)
        else:
            hidden = sum(1 for item in items
                         if (item.get('requires') == 'keyboard' and not self.keyboard_visible) or
                            (item.get('requires') == 'pen' and not self.pen_visible))
            self.services_status.setText(f'共发现 {len(items)} 个服务/计时器；按附件状态隐藏 {hidden} 个。')
        seen = set()
        for item in items:
            unit = item['unit']
            seen.add(unit)
            if unit not in self.service_cards:
                card = self.card(self.services_layout, item['title'], item['description'])
                state = label('—', 'notice')
                card.addWidget(state)
                enabled = QCheckBox('开机启用并运行')
                enabled.toggled.connect(lambda checked, u=unit: self.service_dirty.add(u)
                                        if not self.service_syncing else None)
                card.addWidget(enabled)
                apply_button = QPushButton('应用开关')
                apply_button.clicked.connect(lambda checked=False, u=unit, c=enabled:
                                             self.apply_service(u, c.isChecked()))
                card.addWidget(apply_button, alignment=Qt.AlignmentFlag.AlignLeft)
                self.service_cards[unit] = {
                    'frame': card.parentWidget(), 'state': state,
                    'enabled': enabled, 'button': apply_button,
                }
            controls = self.service_cards[unit]
            controls['state'].setText(
                f"{item.get('state', 'unknown')}  ·  {item.get('unit_file_state') or '—'}" +
                ('  ·  受保护，只读' if item.get('protected') else
                 '' if item.get('manageable') else '  ·  该单元不能启用/停用'))
            if unit not in self.service_dirty:
                self.service_syncing = True
                controls['enabled'].blockSignals(True)
                controls['enabled'].setChecked(bool(item.get('enabled')))
                controls['enabled'].blockSignals(False)
                self.service_syncing = False
            available = bool(item.get('manageable'))
            controls['enabled'].setEnabled(available)
            controls['button'].setEnabled(available)
            required = item.get('requires')
            visible = not ((required == 'keyboard' and not self.keyboard_visible) or
                           (required == 'pen' and not self.pen_visible))
            controls['frame'].setVisible(visible)
        for unit, controls in self.service_cards.items():
            if unit not in seen:
                controls['frame'].setVisible(False)

    def apply_service(self, unit, enabled):
        self.service_dirty.discard(unit)
        self.perform({'action': 'service-set', 'name': unit, 'enabled': enabled})

    def accessory_override(self, name):
        if not self.preferences:
            return None
        key = 'hardware/show_' + name
        return self.preference_bool(key) if self.preferences.contains(key) else None

    def set_accessory_override(self, name, enabled):
        if self.accessory_syncing:
            return
        if self.preferences:
            self.preferences.setValue('hardware/show_' + name, bool(enabled))
            self.preferences.sync()
        self.update_accessories(self.snapshot.get('keyboard', {}), self.snapshot.get('pen', {}))

    def open_test(self, kind):
        if self.test_dialog:
            self.test_dialog.close()
            self.test_dialog.deleteLater()
        self.test_dialog = HardwareTestDialog(kind, lambda: {
            'sample_time': self.snapshot_time, 'service': bool(self.snapshot.get('service')),
            'battery': copy.deepcopy(self.snapshot.get('battery', {})),
            'adapter': copy.deepcopy(self.snapshot.get('adapter', {})),
            'diagnostics': copy.deepcopy(self.snapshot.get('diagnostics', {})),
        }, self.demo, self)
        self.test_dialog.show()

    def save_pen_action(self):
        if self.preferences:
            self.preferences.setValue('pen/double_action', self.pen_action.currentData())
            self.preferences.sync()
            self.pen_action_result.setText('双击行为偏好已保存；在显式开启的观察期间生效。')

    def start_pen_touch(self):
        if not self.pen_undocked.isChecked():
            self.message('请确认笔已取下', '仅在笔确实离开磁吸并已唤醒时开始，避免发送与真实状态不符的通知。')
            return
        self.perform({'action': 'pen-touch-start', 'undocked': True})

    def apply_pen_slide(self):
        self.perform({'action': 'pen-slide-set', 'enabled': self.pen_slide_enabled.isChecked()})

    def stop_pen_touch(self):
        if not self.demo and self.pen_session:
            self.dbus('ReleasePenTouch')
            self.pen_session = None
            self.pen_stop.setEnabled(False)
            self.timer.setInterval(3000)

    def update_pen_touch(self, pen):
        touch = pen.get('touch', {})
        slide_state = {True: '开启请求已发送', False: '关闭请求已发送'}.get(touch.get('slide_requested'), '未设置或未知')
        self.pen_touch_status.setText(
            f"{touch.get('state', '未启动')} · 专用双击 {touch.get('count', 0)} 次\n"
            f"滑动：{slide_state}（非硬件回读） · 滚轮记录 {touch.get('wheel_events', 0)} 条（含可能的高精度配对记录，不等于动作次数）" +
            ('\n' + touch['error'] if touch.get('error') else ''))
        if 'slide' not in self.dirty:
            blocked = self.pen_slide_enabled.blockSignals(True)
            self.pen_slide_enabled.setChecked(touch.get('slide_requested') is True)
            self.pen_slide_enabled.blockSignals(blocked)
        events = touch.get('events', [])
        if not self.pen_touch_log.hasFocus():
            self.pen_touch_log.setPlainText('\n'.join(
                f"{entry['elapsed']:7.3f}s  {entry['kind']}  {entry['value']}  [{entry['hex']}]" for entry in events))
            self.pen_touch_log.verticalScrollBar().setValue(self.pen_touch_log.verticalScrollBar().maximum())
        ours = bool(self.pen_session and touch.get('session') == self.pen_session)
        self.pen_stop.setEnabled(ours and bool(touch.get('running')))
        if not ours:
            return
        for entry in events:
            if entry['id'] <= self.pen_seen:
                continue
            self.pen_seen = entry['id']
            if entry['kind'] == 'double-tap' and touch.get('ready'):
                self.dispatch_pen_action(entry)
        if not touch.get('running'):
            self.pen_session = None
            self.timer.setInterval(3000)

    def dispatch_pen_action(self, entry):
        if self.demo:
            return
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(entry['time'])).total_seconds()
        except (ValueError, KeyError):
            return
        if age < 0 or age > 4:
            self.pen_action_result.setText('记录到较早的双击，未补执行硬件动作。')
            return
        QApplication.alert(self, 1000)
        self.pen_action_result.setText('收到真实笔身双击 · ' + entry['time'])
        action = self.pen_action.currentData()
        if action == 'observe':
            return
        if self.busy or (self.pen_last_action and
                         (datetime.now(timezone.utc) - self.pen_last_action).total_seconds() < 2):
            self.pen_action_result.setText('已收到双击；操作繁忙或间隔不足两秒，未重复发送动作。')
            return
        self.pen_last_action = datetime.now(timezone.utc)
        if action == 'feedback':
            self.perform({'action': 'pen-feedback'})
        elif action == 'writing-toggle':
            self.writing.setChecked(not bool(self.snapshot['pen'].get('writing')))
            self.apply_writing()

    def build_diagnostics(self):
        layout = self.page('诊断信息', '只读状态；未知保留为空，不用“没有报错”代替硬件验证。')
        layout.addWidget(label('包含内核、控制器和附件状态。复制后对外分享前，请检查其中的设备地址等信息。', 'warning'))
        copy_button = QPushButton('复制当前诊断信息')
        copy_button.clicked.connect(lambda: QApplication.clipboard().setText(
            json.dumps(self.snapshot, ensure_ascii=False, indent=2)))
        layout.addWidget(copy_button, alignment=Qt.AlignmentFlag.AlignLeft)
        self.diagnostics = QPlainTextEdit()
        self.diagnostics.setReadOnly(True)
        self.diagnostics.setMinimumHeight(350)
        self.diagnostics.setFont(follow_system_font(QFontDatabase.systemFont(
            QFontDatabase.SystemFont.FixedFont)))
        layout.addWidget(self.diagnostics, 1)

    def dbus(self, method, request=None):
        message = QDBusMessage.createMethodCall('org.caihong.Toolbox1', '/org/caihong/Toolbox1',
                                                'org.caihong.Toolbox1', method)
        if request is not None:
            message.setArguments([json.dumps(request)])
        timeout = 30000 if method == 'ReleasePenTouch' else 125000 if request else 8000
        pending = QDBusConnection.systemBus().asyncCall(message, timeout)
        watcher = QDBusPendingCallWatcher(pending, self)
        watcher.setProperty('generation', self.generation)
        watcher.finished.connect(lambda completed: self.received(method, request, completed))

    def refresh(self):
        if self.polling:
            return
        if self.demo:
            self.update_status(demo_snapshot())
            return
        self.polling = True
        self.dbus('GetStatus')

    def received(self, method, request, watcher):
        reply = watcher.reply()
        watcher.deleteLater()
        error = reply.errorMessage() if reply.type() == QDBusMessage.MessageType.ErrorMessage else None
        data = None
        if not error:
            try:
                data = json.loads(reply.arguments()[0])
            except (ValueError, IndexError, TypeError):
                error = '后台返回了无效数据'
        if method == 'GetStatus':
            self.polling = False
            if watcher.property('generation') != self.generation:
                self.refresh()
                return
            if error:
                data = read_snapshot()
                data.update(service=False, service_error=error)
            self.update_status(data)
        elif method == 'ReleasePenTouch':
            self.result.setText('停止观察失败：' + error if error else data['message'])
            self.refresh()
        else:
            self.busy = False
            section = 'slide' if request['action'] == 'pen-slide-set' else request['action'].split('-')[0]
            self.dirty.discard(section)
            self.result.setText('操作失败：' + error if error else data['message'])
            if not error and request['action'] == 'pen-touch-start':
                self.pen_session, self.pen_seen = data['session'], 0
                self.timer.setInterval(1000)
            if error:
                self.message('设置未完全确认', error)
            self.refresh()

    def preference_bool(self, key):
        if not self.preferences:
            return False
        value = self.preferences.value(key, False)
        return value is True or str(value).lower() in ('true', '1')

    def remember_accessory(self, key, seen):
        if seen and self.preferences and not self.preference_bool(key):
            self.preferences.setValue(key, True)
            self.preferences.sync()

    def update_accessories(self, keyboard, pen):
        keyboard_seen = self.preference_bool('hardware/seen_keyboard')
        pen_seen = self.preference_bool('hardware/seen_pen')
        keyboard_present = bool(keyboard.get('available')) or keyboard.get('pogo_attached') == 1
        pen_present = bool(pen.get('connected') or pen.get('writing') or pen.get('paired'))
        self.remember_accessory('hardware/seen_keyboard', keyboard_present)
        self.remember_accessory('hardware/seen_pen', pen_present)
        keyboard_override = self.accessory_override('keyboard')
        pen_override = self.accessory_override('pen')
        self.keyboard_visible = (self.demo or keyboard_seen or keyboard_present
                                 if keyboard_override is None else keyboard_override)
        self.pen_visible = (self.demo or pen_seen or pen_present
                            if pen_override is None else pen_override)
        self.accessory_syncing = True
        for checkbox, visible in ((self.show_keyboard, self.keyboard_visible),
                                  (self.show_pen, self.pen_visible)):
            checkbox.blockSignals(True)
            checkbox.setChecked(bool(visible))
            checkbox.blockSignals(False)
        self.accessory_syncing = False
        self.navigation.setRowHidden(2, not self.keyboard_visible)
        self.navigation.setRowHidden(3, not self.pen_visible)
        self.test_cards['keyboard'].setVisible(self.keyboard_visible)
        self.pen_tests_widget.setVisible(self.pen_visible)
        self.pen_settings_button.setVisible(self.pen_visible)
        if self.navigation.currentRow() in (2, 3):
            hidden = (self.navigation.currentRow() == 2 and not self.keyboard_visible or
                      self.navigation.currentRow() == 3 and not self.pen_visible)
            if hidden:
                self.navigation.setCurrentRow(0)

    def update_status(self, snapshot):
        self.snapshot_time = datetime.now(timezone.utc).isoformat()
        self.snapshot = copy.deepcopy(snapshot)
        device, battery = snapshot['device'], snapshot['battery']
        keyboard, pen = snapshot['keyboard'], snapshot['pen']
        supported = device['supported']
        self.update_accessories(keyboard, pen)
        self.update_services(snapshot.get('services'))
        self.connection.setText('演示模式 · 不连接硬件' if self.demo else
                                '设备服务已连接' if snapshot.get('service') else '只读模式 · 设备服务未连接')
        self.device_info.setText(f"{device.get('model') or '未检测到 Caihong 设备'}\n"
                                 f"Linux {device['kernel']}  ·  {device['architecture']}" +
                                 ('' if supported else '\n非 Caihong 设备：所有修改均已禁用。'))
        self.capacity.value.setText(formatted(battery.get('capacity'), suffix='%', precision=0))
        self.temperature.value.setText(formatted(battery.get('temp'), 10, ' °C'))
        self.health.value.setText(formatted(battery.get('state_of_health'), suffix='%', precision=0))
        adapter = snapshot['adapter']
        online = adapter.get('online')
        self.source.value.setText('已接入' if online == 1 else '未接入' if online == 0 else '未知')
        self.charge_progress.setValue(battery.get('capacity') or 0)
        mode = {'auto': '自动充电', 'inhibit-charge': '请求停充供电'}.get(battery.get('charge_behaviour'), '未知')
        controller = battery.get('controller', {})
        accessories = []
        if self.keyboard_visible:
            accessories.append('键盘：' + (
                '磁吸连接' if keyboard.get('pogo_attached') == 1 else
                '蓝牙节点就绪' if keyboard.get('available') else '未就绪'))
        if self.pen_visible:
            accessories.append('手写笔：' + (
                '蓝牙已连接' if pen.get('connected') else '未连接或状态未知'))
        self.overview_detail.setText(
            f"充电策略：{mode}    ·    协议：{adapter.get('protocol', '未识别')}    ·    固件原始 SOC：{controller.get('raw_soc', '—')}%" +
            ('\n' + '    ·    '.join(accessories) if accessories else ''))
        self.battery_details.setText(
            f"固件报告：循环 {battery.get('cycle_count') if battery.get('cycle_count') is not None else '—'} 次  ·  "
            f"满充 {formatted(battery.get('charge_full'), 1000, ' mAh', 0)} / 设计 {formatted(battery.get('charge_full_design'), 1000, ' mAh', 0)}\n"
            f"容量比 {formatted(battery.get('capacity_ratio'), suffix='%')}（非独立健康度测量）  ·  "
            f"电池电流 {formatted(battery.get('current_now'), 1000, ' mA', 0)}  ·  电压 {formatted(battery.get('voltage_now'), 1000000, ' V', 2)}\n"
            f"控制器：pending={controller.get('pending', '—')}  last_rc={controller.get('last_rc', '—')}；正电流为充电，负电流为电池补电。")
        pd = adapter.get('pd', {})
        pump_count, pump_total = adapter.get('pumps_online'), len(adapter.get('pumps') or [])
        buck = formatted(adapter.get('buck_power_uw'), 1000000, ' W')
        pump_power = formatted(adapter.get('pump_power_uw'), 1000000, ' W')
        path_text = (f'buck {buck} + 双泵 {pump_power}' if pump_count == 2 else
                     f'buck {buck} + 单泵 {pump_power}' if pump_count == 1 else f'buck {buck}')
        roles = {'sink': '受电', 'source': '供电', 'host': '主机', 'device': '设备'}
        role = roles.get(pd.get('power_role'), pd.get('power_role') or '—')
        data_role = roles.get(pd.get('data_role'), pd.get('data_role') or '—')
        orientation = {'normal': '正插', 'reverse': '反插'}.get(
            pd.get('orientation'), pd.get('orientation') or '—')
        self.input_power.value.setText(formatted(adapter.get('input_power_uw'), 1000000, ' W'))
        self.battery_power.value.setText(formatted(battery.get('power_uw'), 1000000, ' W'))
        self.charge_protocol.value.setText(adapter.get('protocol', '—'))
        pps_limit = adapter.get('pps_current_limit_ma')
        self.pps_limit.value.setText(formatted(pps_limit, 1000, ' A'))
        if pps_limit is None and 'VOOC' in (adapter.get('protocol') or ''):
            self.pps_limit.value.setText('不适用')
            self.pps_limit.caption.setText('VOOC/SVOOC 使用私有快充协议，不使用 USB PD APDO')
        else:
            self.pps_limit.caption.setText('适配器在此电压范围公布的 APDO 上限，不代表当前已取满')
        self.power_details.setText(
            f"供电路径：{path_text if online == 1 else '未接入'}  ·  活动电荷泵 {pump_count if pump_count is not None else '—'}/{pump_total or '—'}\n"
            f"USB 类型：{adapter.get('active_type') or '—'}（厂商上报）  ·  PD {pd.get('revision') or '—'}  ·  "
            f"供电角色 {role}  ·  数据角色 {data_role}  ·  {orientation}\n"
            f"充电器公开能力：{adapter.get('capability_summary') or '未读取到 PPS/PDO 能力'}\n"
            f"输入上限：{formatted(adapter.get('current_max'), 1000000, ' A')} @ {formatted(adapter.get('voltage_max'), 1000000, ' V')}；"
            f"功率为 sysfs 遥测估算，协议识别不替代外置功率计或示波器验证。")
        running = keyboard.get('compensation_running', False)
        self.keyboard_details.setText(
            f"磁吸状态：{'已连接' if keyboard.get('pogo_attached') == 1 else '已离座' if keyboard.get('pogo_attached') == 0 else '未知'}  ·  "
            f"蓝牙内核接口：{'就绪' if keyboard.get('available') else '不可用'}\n"
            f"系统 F4 自动兼容：{'运行中' if running else '等待蓝牙连接或离座' if keyboard.get('automatic') else '系统服务不可用'}  ·  "
            f"最近 BLE 写返回：{formatted(keyboard.get('last_write_ms'), suffix=' ms')}" +
            (f"\n{keyboard['error']}" if keyboard.get('error') else ''))
        request_state = {-1: '未知', 0: '关闭请求已确认', 1: '开启请求已确认'}.get(pen.get('vibrator_request'), '接口不可用')
        self.pen_details.setText(f"蓝牙：{'已连接' if pen.get('connected') else '未连接或状态未知'}  ·  扫描模式：{pen.get('scan_mode', '—')}\n"
                                f"触控器：{request_state}（不是马达实测状态）" +
                                (f"\n{pen['error']}" if pen.get('error') else ''))
        self.syncing = True
        if 'charge' not in self.dirty:
            self.bypass.setChecked(battery.get('charge_behaviour') == 'inhibit-charge')
            for control, name in ((self.start_limit, 'charge_control_start_threshold'),
                                   (self.end_limit, 'charge_control_end_threshold')):
                if battery.get(name) is not None and 0 <= battery[name] <= 100:
                    control.setValue(battery[name])
        if 'keyboard' not in self.dirty:
            self.media.setChecked(keyboard.get('media_keys') != 1)
        if 'pen' not in self.dirty:
            writing = pen.get('writing')
            self.writing.setChecked(bool(writing))
            if writing:
                self.tool.setCurrentIndex(self.tool.findData(writing['tool']))
                self.level.setValue(writing['level'])
        self.syncing = False
        enabled = supported and bool(snapshot.get('service')) and not self.demo and not self.busy
        for action, button in self.buttons.items():
            if action.startswith('charge-'):
                available = battery.get('control_available', False)
            elif action == 'keyboard-touchpad':
                available = keyboard.get('available') or keyboard.get('pogo_attached') == 1
            elif action.startswith('keyboard-'):
                available = keyboard.get('available', False)
            elif action == 'pen-writing':
                available = pen.get('controller_available') and (pen.get('connected') or pen.get('writing'))
            elif action == 'pen-touch-start':
                available = pen.get('connected') and not pen.get('touch', {}).get('running')
            elif action == 'pen-slide-set':
                touch = pen.get('touch', {})
                available = bool(self.pen_session and touch.get('session') == self.pen_session
                                 and touch.get('ready') and touch.get('running') and touch.get('hid_available'))
            else:
                available = pen.get('feedback_available', False)
            button.setEnabled(bool(enabled and available))
        self.update_pen_touch(pen)
        if not self.diagnostics.hasFocus():
            self.diagnostics.setPlainText(json.dumps(snapshot, ensure_ascii=False, indent=2))
        if self.demo:
            self.result.setText('演示数据 · 所有应用按钮已禁用，不会读取设备或请求管理员授权。')
        elif not snapshot.get('service'):
            self.result.setText('设备服务未安装或不可用；当前只读。请按 README 安装后台与权限策略。')

    def message(self, title, content, confirm=False):
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(content)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel if confirm else QMessageBox.StandardButton.Ok)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel if confirm else QMessageBox.StandardButton.Ok)
        return box.exec() == QMessageBox.StandardButton.Ok

    def apply_limits(self):
        request = {'action': 'charge-limit', 'start': self.start_limit.value(),
                   'end': self.end_limit.value(), 'experimental': True}
        if self.message('确认实验性充电阈值', '此功能尚未完成跨阈值、睡眠与长时验证。使用原始 SOC，可能与桌面电量不同。\n是否只为当前运行期应用？', True):
            self.perform(request)

    def apply_writing(self):
        request = {'action': 'pen-writing', 'enabled': self.writing.isChecked(),
                   'tool': self.tool.currentData(), 'level': self.level.value()}
        if request['enabled'] and (request['tool'], request['level']) != ('pencil', 3):
            if not self.message('未实测的触感组合', '当前仅铅笔三级完成了物理对照。其他工具与等级来自原厂协议，但尚未在此设备上实测。仍要应用吗？', True):
                return
        self.perform(request)

    def perform(self, request):
        if self.demo or self.busy or not self.snapshot.get('service') or not self.snapshot['device']['supported']:
            return
        try:
            validate_request(request)
        except ValueError as error:
            self.message('参数无效', str(error))
            return
        self.busy = True
        self.generation += 1
        for button in self.buttons.values():
            button.setEnabled(False)
        self.result.setText('等待系统授权或设备请求完成…界面不会要求你保存管理员密码。')
        self.dbus('Apply', request)

    def mark_dirty(self, section, *arguments):
        if not self.syncing:
            self.dirty.add(section)

    def closeEvent(self, event):
        self.stop_pen_touch()
        if self.test_dialog:
            self.test_dialog.close()
        self.timer.stop()
        super().closeEvent(event)


def main():
    parser = argparse.ArgumentParser(description='Caihong Toolbox 图形设置工具')
    parser.add_argument('--demo', action='store_true', help='纯界面演示，不读取或修改硬件')
    parser.add_argument('--screenshot', type=Path, help='保存界面预览后退出')
    parser.add_argument('--page', choices=('overview', 'charge', 'keyboard', 'pen', 'tests',
                                           'services', 'diagnostics'), default='overview')
    arguments = parser.parse_args()
    if os.geteuid() == 0 and not arguments.demo:
        parser.error('请以普通桌面用户启动 GUI，修改通过系统 Polkit 授权')
    application = QApplication(sys.argv[:1])
    application.setApplicationName('caihong-toolbox')
    application.setApplicationDisplayName('Caihong Toolbox')
    application.setOrganizationName('Caihong')
    window = Window(arguments.demo)
    window.navigation.setCurrentRow(('overview', 'charge', 'keyboard', 'pen', 'tests',
                                     'services', 'diagnostics').index(arguments.page))
    window.show()
    if arguments.screenshot:
        def capture():
            if not window.grab().save(str(arguments.screenshot)):
                print('无法保存截图', file=sys.stderr)
                application.exit(1)
            else:
                application.quit()
        QTimer.singleShot(800, capture)
    result = application.exec()
    window.close()
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    sys.exit(result)
