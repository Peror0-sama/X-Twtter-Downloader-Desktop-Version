from __future__ import annotations

from pathlib import Path
import time
import os
import subprocess
import sys

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QActionGroup, QIcon, QColor, QBrush, QPainter, QDesktopServices, QTextCursor, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
    QPushButton, QSizePolicy, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QSystemTrayIcon, QSplitter, QTabWidget, QTableWidget, QTableWidgetItem, QTreeWidget, QTreeWidgetItem, QTreeWidgetItemIterator, QVBoxLayout, QWidget,
)

from .account import AccountStore, set_browser_resolver
from .browser_resolver import BrowserResolver
from .core import DownloadError, make_session, parse_input, read_txt, split_concatenated_urls
from .engine import Engine
from .storage import Store
from . import __version__

STATUS = {"queued": "等待中", "paused": "已暂停", "waiting_choice": "等待选择格式", "resolving": "解析中", "downloading": "下载中", "converting": "转换 GIF 中", "done": "已完成", "failed": "失败 · 可重试", "deleting": "正在移除"}
KINDS = {"video": "视频", "animated_gif": "动图", "photo": "图片"}
FAILURE_LABELS = {
    'deleted': '已删除', 'private': '无访问权限', 'sensitive': '内容限制',
    'suspended': '账号停用', 'no_media': '无可下载媒体', 'not_found': '无法找到',
    'unavailable': '原因未确认', 'account_parse': '接口等待超时', 'parse': '解析未完成',
    'external_card': '外链卡片 · 警告', 'rules': 'X 规则限制', 'page_load': '页面加载失败',
    'payload_parse': '接口解析失败', 'capture': '接口读取失败', 'api_http': '接口请求失败', 'media_http': '媒体请求失败',
    'login_required': '需要登录', 'login_expired': '登录失效', 'rate_limit': '请求限流',
    'access_denied': '访问被拒绝', 'account_access': '访问被拒绝', 'withheld': '地区限制',
    'network': '网络故障', 'timeout': '网络超时', 'proxy': '代理故障', 'partial': '部分文件失败',
}


def failure_label(tweet):
    return '失败 · ' + FAILURE_LABELS.get(tweet.get('error_code'), '下载未完成')

# 显式设置整块背景和文字，避免 Windows 深色主题与应用浅色样式混用。
LIGHT_SURFACES = """
    QDialog,QMessageBox { background:#f3f5f9; color:#203047; }
    QDialog QLabel { color:#203047; background:transparent; }
    QTableWidget { color:#203047; background:white; alternate-background-color:#f4f7fb;
        gridline-color:#dfe5ee; border:1px solid #dfe5ee; selection-background-color:#dbeafe; selection-color:#164d9e; }
    QTableWidget::item { padding:6px; }
    QHeaderView { background:#f5f7fa; color:#53647a; }
    QHeaderView::section,QTableCornerButton::section { background:#f5f7fa; color:#53647a; border:0; padding:9px 4px; }
    QDialog QComboBox { color:#203047; background:white; padding:5px 8px; min-height:20px; }
    QDialog QComboBox QAbstractItemView { color:#203047; background:white; selection-background-color:#dbeafe; selection-color:#164d9e; }
    QDialog QPushButton { color:#203047; background:#f6f8fc; border:1px solid #dce3ed; border-radius:7px; padding:7px 8px; }
    QMenu { color:#203047; background:white; border:1px solid #dfe5ee; }
    QMenu::item:selected { background:#e8f1ff; color:#164d9e; }
    QMenu::item:disabled { color:#788496; }
    QToolTip { color:#203047; background:#f6f8fc; border:1px solid #dce3ed; }
    QSplitter::handle { background:#dfe5ee; }
"""


def application_icon():
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return QIcon(str(base / "assets" / "peror0_sama.ico"))


def human_size(size):
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
        size /= 1024


PROGRESS_ROLE = Qt.ItemDataRole.UserRole + 2


def file_percent(media):
    status = media['status']
    if status == 'done':
        return 100
    if status in ('queued', 'waiting_choice'):
        return 0
    if status == 'converting' or media.get('_phase') in ('merging', 'verifying'):
        return 99
    if media.get('_segments_total'):
        return min(99, max(0, media.get('_segments_done', 0) * 100 // media['_segments_total']))
    if media.get('total'):
        return min(99, max(0, media.get('downloaded', 0) * 100 // media['total']))
    return None


def queue_progress(media, status):
    """多文件按各文件完成比例汇总；总量未知时不编造百分比。"""
    color = '#efb3bc' if status == 'failed' else '#a8ddca' if status == 'done' else '#d3d9e3' if status == 'paused' else '#a9cdfa'
    if status in ('queued', 'waiting_choice', 'resolving', 'deleting'):
        return (None if status in ('resolving', 'deleting') else 0, STATUS[status], color)
    values = [file_percent(m) for m in media]
    value = 100 if status == 'done' else min(99, sum(values) // len(values)) if values and all(v is not None for v in values) else None
    if status == 'paused':
        label = '已暂停' if value is None else f'{value}% · 已暂停'
    elif status == 'failed':
        label = '失败' if value is None else f'{value}% · 失败'
    elif status == 'done':
        size = sum(m.get('total', 0) for m in media)
        label = '100% · ' + (human_size(size) if size else '已完成')
    elif any(m['status'] == 'converting' for m in media):
        label = '99% · 转换中' if value == 99 else (f'{value}% · 转换中' if value is not None else '转换中')
    elif any(m.get('_phase') == 'merging' and m['status'] != 'done' for m in media):
        label = f'{value}% · 合并中' if value is not None else '合并中'
    elif any(m.get('_phase') == 'verifying' and m['status'] != 'done' for m in media):
        label = f'{value}% · 校验中' if value is not None else '校验中'
    elif value is None:
        label = '已下载 ' + human_size(sum(m.get('downloaded', 0) for m in media)) if media else '等待下载'
    else:
        label = f'{value}%'
        if len(media) > 1:
            label += f" · {sum(m['status']=='done' for m in media)}/{len(media)} 文件"
    return value, label, color


def set_queue_progress(item, media, status):
    info = queue_progress(media, status)
    item.setText(2, info[1])
    item.setData(2, PROGRESS_ROLE, info)


class QueueProgressDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        info = index.data(PROGRESS_ROLE)
        if not info:
            return super().paint(painter, option, index)
        background = QStyleOptionViewItem(option)
        self.initStyleOption(background, index)
        background.text = ''
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, background, painter, option.widget)
        rect = option.rect.adjusted(6, 0, -6, 0)
        rect.setHeight(22)
        rect.moveCenter(option.rect.center())
        value, label, color = info
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor('#eaf0f7'))
        painter.drawRoundedRect(rect, 5, 5)
        painter.save()
        if value is not None:
            fill = rect.adjusted(0, 0, 0, 0)
            fill.setWidth(round(rect.width() * value / 100))
            painter.setClipRect(fill)
        painter.setBrush(QBrush(QColor(color), Qt.BrushStyle.BDiagPattern) if value is None else QBrush(QColor(color)))
        painter.drawRoundedRect(rect, 5, 5)
        painter.restore()
        painter.setPen(QColor('#203047'))
        text = option.fontMetrics.elidedText(label, Qt.TextElideMode.ElideRight, max(1, rect.width() - 8))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
        painter.restore()


class URLInput(QPlainTextEdit):
    def apply_input(self, action):
        cursor = self.textCursor()
        cursor.beginEditBlock()
        try:
            action()
            self.adapt_urls()
        finally:
            cursor.endEditBlock()

    def insertPlainText(self, text):
        self.apply_input(lambda: super(URLInput, self).insertPlainText(text))

    def insertFromMimeData(self, source):
        self.apply_input(lambda: super(URLInput, self).insertFromMimeData(source))

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.StandardKey.Undo) or event.matches(QKeySequence.StandardKey.Redo):
            super().keyPressEvent(event)
        else:
            self.apply_input(lambda: super(URLInput, self).keyPressEvent(event))

    def inputMethodEvent(self, event):
        self.apply_input(lambda: super(URLInput, self).inputMethodEvent(event))

    def setPlainText(self, text):
        super().setPlainText(split_concatenated_urls(text))

    def adapt_urls(self):
        text = self.toPlainText()
        if split_concatenated_urls(text) == text:
            return
        # 按文档 UTF-16 位置插入，保留光标/选区；自动分行与本次输入共同撤销。
        positions = []
        offset = 0
        for index, char in enumerate(text):
            if index and text[index-1] not in '\r\n' and text.startswith('https://', index):
                positions.append(offset)
            offset += 2 if ord(char) > 0xffff else 1
        saved = self.textCursor()
        edit = QTextCursor(self.document())
        blocked = self.blockSignals(True)
        try:
            edit.beginEditBlock()
            for position in reversed(positions):
                edit.setPosition(position)
                edit.insertText('\n')
            edit.endEditBlock()
            self.setTextCursor(saved)
        finally:
            self.blockSignals(blocked)


class DropArea(QFrame):
    files = Signal(list)
    clicked = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("dropArea")
        self.setAcceptDrops(True)
        self.setFixedHeight(60)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QVBoxLayout(self)
        label = QLabel("＋  拖入 TXT 文件\n或点击选择 · 支持多个文件")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(label)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and all(u.isLocalFile() for u in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        self.files.emit([u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()])
        event.acceptProposedAction()


class ImportSignals(QObject):
    finished = Signal(object)


class ImportWorker(QRunnable):
    def __init__(self, store, mode, text="", files=None):
        super().__init__()
        self.store, self.mode, self.text, self.files = store, mode, text, files or []
        self.signals = ImportSignals()

    def run(self):
        urls, errors = parse_input(self.text) if self.text else ([], [])
        for value in self.files:
            path = Path(value)
            try:
                found, problems = parse_input(read_txt(path), path.name)
                urls.extend(found)
                errors.extend(problems)
            except (DownloadError, OSError) as exc:
                errors.append(f"{path.name}：{exc}")
        try:
            added, duplicate = self.store.add_urls(urls, self.mode)
            self.signals.finished.emit({"added": added, "duplicate": duplicate, "errors": errors})
        except Exception as exc:
            self.signals.finished.emit({"added": [], "duplicate": 0, "errors": [f"导入失败（{type(exc).__name__}）"]})


class NetworkWorker(QRunnable):
    def __init__(self, url, mode, proxy, cookies, directory):
        super().__init__()
        self.args = (url, mode, proxy, cookies, directory)
        self.signals = ImportSignals()

    def run(self):
        from .network import probe_network
        self.signals.finished.emit(probe_network(*self.args))


class DownloadReport(QDialog):
    def __init__(self, tweets, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f'下载报告 · {len(tweets)} 条推文')
        self.setStyleSheet(LIGHT_SURFACES)
        self.resize(1120, 640)
        self.setMinimumSize(860, 480)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('每条推文占一行，失败与警告详情见“说明”。'))
        self.tabs = QTabWidget()
        self.tables = {}
        for title, group in (
            ('失败', [t for t in tweets if t['status']=='failed']),
            ('警告', [t for t in tweets if t['status']=='done' and t.get('warning')]),
            ('成功', [t for t in tweets if t['status']=='done' and not t.get('warning')]),
        ):
            table = QTableWidget(len(group), 4)
            table.setHorizontalHeaderLabels(['推文 URL', '作者', '已下载 / 总数', '说明'])
            table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
            table.setAlternatingRowColors(True)
            table.setWordWrap(True)
            table.setColumnWidth(0, 360)
            table.setColumnWidth(1, 170)
            table.setColumnWidth(2, 105)
            table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
            for i, tweet in enumerate(group):
                done = sum(m['status']=='done' for m in tweet['media'])
                reason = tweet['error'] if title=='失败' else tweet.get('warning') or '下载完成'
                if title=='失败' and tweet.get('warning'):
                    reason += '\n警告：'+tweet['warning']
                values = [tweet['url'], '@'+tweet['username']+(' · '+tweet['display_name'] if tweet['display_name'] else ''),
                          f"{done} / {len(tweet['media'])}", reason]
                for col, value in enumerate(values):
                    cell = QTableWidgetItem(value)
                    cell.setToolTip(value)
                    table.setItem(i, col, cell)
            table.resizeRowsToContents()
            self.tables[title] = table
            self.tabs.addTab(table, f'{title}（{len(group)}）')
        layout.addWidget(self.tabs)
        layout.addWidget(QLabel('本报告显示打开时的历史记录；已清除或超过保留期限的记录不包含在内。'))
        close = QPushButton('关闭报告')
        close.clicked.connect(self.close)
        layout.addWidget(close)


class IndividualGifDialog(QDialog):
    def __init__(self, tweet, parent=None):
        super().__init__(parent)
        self.setWindowTitle('逐个选择动图格式')
        self.setStyleSheet(LIGHT_SURFACES)
        self.resize(760, 360)
        self.setMinimumSize(560, 260)
        layout = QVBoxLayout(self)
        label = QLabel(f"@{tweet['username']}\n{tweet['url']}")
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(label)
        layout.addWidget(QLabel('为每个动图选择格式。确认后，不允许更改。'))
        table = QTableWidget(len(tweet['media']), 2)
        table.setHorizontalHeaderLabels(['动图', '保存格式'])
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.verticalHeader().hide()
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.choices = {}
        for index, media in enumerate(tweet['media']):
            table.setItem(index, 0, QTableWidgetItem(f"动图 {media['seq']}"))
            choice = QComboBox()
            choice.addItem('保留 MP4', 'mp4')
            choice.addItem('转换为 GIF', 'gif')
            table.setCellWidget(index, 1, choice)
            self.choices[media['media_id']] = choice
            table.setRowHeight(index, max(44, choice.sizeHint().height() + 4))
        layout.addWidget(table, 1)
        row = QHBoxLayout()
        later = QPushButton('稍后选择'); later.clicked.connect(self.reject)
        start = QPushButton('开始下载'); start.clicked.connect(self.accept)
        row.addStretch(); row.addWidget(later); row.addWidget(start)
        layout.addLayout(row)

    def selections(self):
        return {mid: choice.currentData() for mid, choice in self.choices.items()}


class MainWindow(QMainWindow):
    def __init__(self, store: Store, interactive_close=True):
        super().__init__()
        self.store = store
        self.interactive_close = interactive_close
        self.in_tray = False
        self.exit_requested = False
        self.exit_task_ids = set()
        self.tray_tasks = set()
        self.tray_wait_notified = False
        self.hidden_dialogs = []
        self.account = AccountStore(store.data)
        self.browser_resolver = BrowserResolver(self)
        set_browser_resolver(self.browser_resolver)
        self.engine = Engine(store, self.account.cookies)
        self.import_pool = QThreadPool(self)
        self.import_pool.setMaxThreadCount(1)
        self.import_jobs = []
        self.closing = False
        self.login_dialog = None
        self.network_job = None
        self.format_dialog = None
        self.report_dialog = None
        self.dismissed_batches = set()
        self.dismissed_individual = set()
        self.last_history_cleanup = 0.0
        self.last_snapshot = None
        self.setWindowTitle(f"XDownloader {__version__} · X 媒体下载器")
        self.resize(1380, 820)
        self.setMinimumSize(1120, 720)
        self.build_ui()
        self.setup_tray()
        if self.interactive_close:
            QApplication.instance().setQuitOnLastWindowClosed(False)
        self.account_changed()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(500)
        self.tick()

    def build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(16, 14, 16, 12)
        outer.setSpacing(10)
        banner = QFrame()
        banner.setObjectName("banner")
        banner.setFixedHeight(76)
        header = QHBoxLayout(banner)
        header.setContentsMargins(20, 12, 20, 12)
        logo = QLabel()
        logo.setPixmap(application_icon().pixmap(42, 42))
        logo.setObjectName("logo")
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setFixedSize(42, 42)
        header.addWidget(logo)
        brand = QVBoxLayout()
        brand.setSpacing(2)
        title = QLabel("XDownloader")
        title.setObjectName("title")
        brand.addWidget(title)
        subtitle = QLabel("X 媒体下载器  ·  图片 / 视频 / 动图")
        subtitle.setObjectName("bannerSubtitle")
        brand.addWidget(subtitle)
        header.addLayout(brand)
        version = QLabel(f"v{__version__}")
        version.setObjectName("versionBadge")
        header.addWidget(version)
        self.settings_button = QPushButton("设置")
        self.settings_menu = QMenu(self)
        heading = self.settings_menu.addAction("关闭窗口时")
        heading.setEnabled(False)
        group = QActionGroup(self.settings_menu)
        group.setExclusive(True)
        self.close_actions = {}
        for mode, label in [("ask", "每次询问"), ("tray", "最小化到托盘"), ("exit", "直接关闭")]:
            action = self.settings_menu.addAction(label)
            action.setCheckable(True)
            group.addAction(action)
            action.setChecked(mode == self.store.setting("close_action", "ask"))
            action.triggered.connect(lambda checked=False, value=mode: self.set_close_action(value))
            self.close_actions[mode] = action
        self.settings_button.setMenu(self.settings_menu)
        header.addWidget(self.settings_button)
        header.addStretch()
        self.total_label = QLabel("准备就绪")
        self.total_label.setObjectName("summaryBadge")
        header.addWidget(self.total_label)
        self.report_button = QPushButton('查看下载报告')
        self.report_button.setEnabled(False)
        self.report_button.setToolTip('全部任务处理结束后可查看，按失败、警告、成功分类，每条推文一行。')
        self.report_button.clicked.connect(self.show_report)
        header.addWidget(self.report_button)
        outer.addWidget(banner)
        columns = QHBoxLayout()
        columns.setSpacing(12)
        outer.addLayout(columns, 1)

        left = QFrame()
        left.setObjectName("card")
        left.setFixedWidth(310)
        column = QVBoxLayout(left)
        column.setContentsMargins(14, 12, 14, 12)
        column.setSpacing(6)
        heading = QLabel("添加下载")
        heading.setObjectName("section")
        column.addWidget(heading)
        self.input = URLInput()
        self.input.setObjectName("urlInput")
        self.input.setPlaceholderText("https://x.com/用户名/status/推文ID\n\n多个链接可换行输入")
        self.input.setFixedHeight(78)
        column.addWidget(self.input)
        self.add_button = QPushButton("加入下载队列")
        self.add_button.setObjectName("primary")
        self.add_button.clicked.connect(self.import_text)
        column.addWidget(self.add_button)
        self.drop = DropArea()
        self.drop.clicked.connect(self.choose_files)
        self.drop.files.connect(self.import_files)
        column.addWidget(self.drop)
        self.import_label = QLabel()
        self.import_label.setObjectName("muted")
        self.import_label.setWordWrap(True)
        column.addWidget(self.import_label)

        settings = QTabWidget()
        settings.setObjectName("settingsTabs")
        settings.setFixedHeight(184)
        column.addWidget(settings)
        access_page = QWidget()
        access_layout = QVBoxLayout(access_page)
        access_layout.setContentsMargins(10, 10, 10, 10)
        access_layout.setSpacing(8)
        settings.addTab(access_page, "访问方式")
        self.account_checkbox = QCheckBox("使用已登录的 X 账号")
        self.account_checkbox.setChecked(self.store.setting("use_account") == "1")
        self.account_checkbox.toggled.connect(lambda checked: self.store.set_setting("use_account", "1" if checked else "0"))
        access_layout.addWidget(self.account_checkbox)
        self.account_label = QLabel()
        self.account_label.setWordWrap(True)
        self.account_label.setObjectName("muted")
        access_layout.addWidget(self.account_label)
        auth_row = QHBoxLayout()
        login = QPushButton("登录 X")
        login.clicked.connect(self.open_login)
        auth_row.addWidget(login)
        self.logout_button = QPushButton("清除登录")
        self.logout_button.clicked.connect(self.logout)
        auth_row.addWidget(self.logout_button)
        access_layout.addLayout(auth_row)
        access_layout.addStretch()

        network_page = QWidget()
        network_layout = QVBoxLayout(network_page)
        network_layout.setContentsMargins(10, 8, 10, 8)
        network_layout.setSpacing(5)
        settings.addTab(network_page, "网络连接")
        self.proxy_mode = QComboBox()
        self.proxy_mode.addItem("使用系统代理", "system")
        self.proxy_mode.addItem("手动设置代理", "manual")
        self.proxy_mode.addItem("直接连接", "direct")
        self.proxy_mode.setCurrentIndex(max(0, self.proxy_mode.findData(self.store.setting("proxy_mode", "system"))))
        self.proxy_mode.currentIndexChanged.connect(self.proxy_mode_changed)
        network_layout.addWidget(self.proxy_mode)
        self.proxy_input = QLineEdit(self.store.setting("proxy_url"))
        self.proxy_input.setPlaceholderText("http://127.0.0.1:7890")
        network_layout.addWidget(self.proxy_input)
        apply = QPushButton("应用网络设置")
        apply.clicked.connect(self.save_network)
        network_row = QHBoxLayout()
        network_row.addWidget(apply)
        self.network_button = QPushButton("测试网络")
        self.network_button.clicked.connect(self.test_network)
        network_row.addWidget(self.network_button)
        network_layout.addLayout(network_row)
        self.proxy_mode_changed()
        note = QLabel("需能访问 X 及媒体服务器。中国大陆直连可能不可用，不限定美国网络。")
        note.setWordWrap(True)
        note.setObjectName("muted")
        network_layout.addWidget(note)
        column.addStretch()
        folder = QPushButton("打开下载目录")
        folder.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.store.download))))
        destination_row = QHBoxLayout()
        destination_row.addWidget(folder)
        choose = QPushButton("选择下载目录")
        choose.clicked.connect(self.choose_download_directory)
        destination_row.addWidget(choose)
        column.addLayout(destination_row)
        columns.addWidget(left)

        self.queue_splitter = QSplitter(Qt.Orientation.Vertical)
        self.queue_splitter.setChildrenCollapsible(False)
        self.queue_splitter.setHandleWidth(8)
        queues = QWidget()
        queue_columns = QHBoxLayout(queues)
        queue_columns.setContentsMargins(0, 0, 0, 0)
        queue_columns.setSpacing(12)
        self.active, self.active_label = self.create_queue(queue_columns, "进行中", False)
        self.completed, self.completed_label = self.create_queue(queue_columns, "已完成", True)
        self.queue_splitter.addWidget(queues)
        details = QFrame()
        details.setObjectName('card')
        detail_layout = QVBoxLayout(details)
        detail_layout.setContentsMargins(12, 8, 12, 10)
        detail_layout.addWidget(QLabel('详细信息'))
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMinimumHeight(100)
        self.detail.setPlaceholderText("选择任务查看完整 URL、保存文件名及失败原因。失败任务保留在“进行中”，可重试。")
        detail_layout.addWidget(self.detail)
        self.queue_splitter.addWidget(details)
        self.queue_splitter.setStretchFactor(0, 1)
        self.queue_splitter.setStretchFactor(1, 0)
        self.queue_splitter.setSizes([450, 190])
        columns.addWidget(self.queue_splitter, 1)
        self.folder_label = QLabel()
        self.folder_label.setObjectName("muted")
        self.folder_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.folder_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.update_folder_label()
        outer.addWidget(self.folder_label)
        self.setStyleSheet("""
            QMainWindow { background:#f3f5f9; }
            QWidget { font-family:'Microsoft YaHei UI','Segoe UI'; font-size:12px; color:#203047; }
            QFrame#banner { background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #12233f,stop:1 #244c76); border:1px solid #294b6d; border-radius:12px; }
            QLabel#title { font-size:23px; font-weight:700; color:white; }
            QLabel#bannerSubtitle { color:#b9cee6; font-size:11px; }
            QLabel#logo { color:white; background:#367bd7; border:1px solid #6a9ce0; border-radius:11px; font-size:28px; font-weight:700; }
            QLabel#versionBadge { color:#d9eaff; background:#31587f; border:1px solid #527399; border-radius:9px; padding:4px 9px; }
            QLabel#summaryBadge { color:#e6f0ff; background:#294d72; border:1px solid #527399; border-radius:9px; padding:9px 14px; }
            QLabel#section { font-size:16px; font-weight:650; }
            QLabel#muted { color:#6d7b8d; font-size:11px; }
            QFrame#card { background:white; border:1px solid #dfe5ee; border-radius:12px; }
            QPlainTextEdit,QLineEdit,QComboBox { background:white; border:1px solid #d6deea; border-radius:7px; padding:6px; selection-background-color:#dbeafe; }
            QPlainTextEdit:focus,QLineEdit:focus { border:1px solid #2876e2; }
            QPushButton { background:#f6f8fc; border:1px solid #dce3ed; border-radius:7px; padding:7px 8px; }
            QPushButton:hover { background:#eaf1fd; border-color:#9dbce9; }
            QPushButton:disabled { color:#98a2b0; }
            QPushButton#primary { background:#1765d8; color:white; border:0; font-weight:650; }
            QPushButton#primary:hover { background:#1055bb; }
            QFrame#dropArea { border:2px dashed #b8cceb; border-radius:9px; background:#f5f9ff; }
            QTreeWidget { border:0; background:white; alternate-background-color:#f9fbfe; outline:0; }
            QTreeWidget::item { min-height:38px; border-bottom:1px solid #edf1f6; }
            QTreeWidget::item:selected { background:#e8f1ff; color:#164d9e; }
            QHeaderView::section { background:#f5f7fa; border:0; padding:9px 4px; color:#68778a; }
            QTabWidget::pane { border:1px solid #dfe5ee; border-radius:7px; background:#fbfcff; }
            QTabBar::tab { background:#edf2f8; border:0; padding:7px 16px; color:#62758d; }
            QTabBar::tab:selected { background:#e4efff; color:#1765d8; font-weight:600; }
            QCheckBox { spacing:7px; }
        """ + LIGHT_SURFACES)

    def create_queue(self, columns, title, completed):
        panel = QFrame()
        panel.setObjectName("card")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 16, 14, 14)
        header = QHBoxLayout()
        label = QLabel(title + "  0")
        label.setObjectName("section")
        header.addWidget(label)
        header.addStretch()
        layout.addLayout(header)
        tree = QTreeWidget()
        tree.setColumnCount(3)
        tree.setHeaderLabels(["账号 / 推文 / 媒体", "状态", "进度 / 大小"])
        tree.setAlternatingRowColors(True)
        tree.setRootIsDecorated(True)
        tree.setColumnWidth(0, 215)
        tree.setColumnWidth(1, 110)
        tree.setColumnWidth(2, 150)
        tree.setItemDelegateForColumn(2, QueueProgressDelegate(tree))
        tree.header().setStretchLastSection(False)
        tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        tree.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        tree.itemSelectionChanged.connect(lambda: self.show_details(tree))
        tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tree.customContextMenuRequested.connect(lambda point: self.show_queue_menu(tree, point, completed))
        tree.itemDoubleClicked.connect(lambda item, _: self.double_click_item(item, completed))
        if not completed:
            tree.setExpandsOnDoubleClick(False)
            tree.itemClicked.connect(self.click_queue_item)
        layout.addWidget(tree, 1)
        if not completed:
            pause_row = QHBoxLayout()
            self.pause_all_button = QPushButton('全部暂停')
            self.pause_all_button.clicked.connect(lambda: self.pause_queue_items({t['id'] for t in self.store.snapshot()}))
            self.resume_all_button = QPushButton('全部恢复')
            self.resume_all_button.clicked.connect(lambda: self.resume_queue_items({t['id'] for t in self.store.snapshot()}))
            pause_row.addWidget(self.pause_all_button)
            pause_row.addWidget(self.resume_all_button)
            layout.addLayout(pause_row)
        row = QHBoxLayout()
        if completed:
            open_file = QPushButton("打开所选文件")
            open_file.clicked.connect(lambda: self.open_selected(tree))
            row.addWidget(open_file)
            clear = QPushButton("清除下载历史")
            clear.setToolTip("移除已完成和失败记录，保留下载文件与未结束任务。记录在结束 72 小时后也会自动清理。")
            clear.clicked.connect(self.clear_history)
            row.addWidget(clear)
        else:
            retry = QPushButton("重试所选失败")
            retry.clicked.connect(lambda: self.retry_selected(tree))
            row.addWidget(retry)
            retry_all = QPushButton("重试全部失败")
            retry_all.clicked.connect(self.retry_all)
            row.addWidget(retry_all)
        if not completed:
            self.clear_failed_button = QPushButton("清除所有失败")
            self.clear_failed_button.setEnabled(False)
            self.clear_failed_button.clicked.connect(self.clear_failed)
            row.addWidget(self.clear_failed_button)
        layout.addLayout(row)
        columns.addWidget(panel, 1)
        return tree, label

    def auth_mode(self):
        return "account" if self.account_checkbox.isChecked() else "anonymous"

    def update_folder_label(self):
        text = "待下载文件保存位置：" + str(self.store.download)
        self.folder_label.setText(text)
        self.folder_label.setToolTip(text + "\n选择后立即生效，包含队列中尚未开始下载的文件。已开始下载的文件保留原位置。")

    def choose_download_directory(self):
        directory = QFileDialog.getExistingDirectory(self, "选择下载目录（立即用于尚未开始下载的文件）", str(self.store.download))
        if not directory:
            return
        try:
            self.store.set_download_directory(directory)
        except (OSError, ValueError):
            QMessageBox.warning(self, "无法使用该目录", "目录不可写或不可用，请选择其他目录。原下载设置保持不变。")
            return
        self.update_folder_label()
        self.detail.setPlainText("下载目录已立即生效：" + str(self.store.download) + "\n无需重启。新任务及队列中尚未开始下载的文件使用此目录；已开始下载的文件保留原位置。")
        self.last_snapshot = None
        self.tick()

    def proxy_mode_changed(self):
        if hasattr(self, "proxy_input"):
            self.proxy_input.setEnabled(self.proxy_mode.currentData() == "manual")

    def save_network(self):
        mode, proxy = self.proxy_mode.currentData(), self.proxy_input.text().strip()
        try:
            from urllib.parse import urlsplit
            if mode == "manual" and (urlsplit(proxy).username or urlsplit(proxy).password):
                raise DownloadError("proxy", "此版本手动代理仅支持无账号密码的代理地址")
            with make_session(mode, proxy):
                pass
        except (DownloadError, ValueError) as exc:
            QMessageBox.warning(self, "网络设置", str(exc))
            return False
        self.store.set_setting("proxy_mode", mode)
        self.store.set_setting("proxy_url", proxy)
        self.detail.setPlainText("网络设置已保存，将用于新开始和重试的任务。已有登录窗口请关闭后重新打开。")
        return True

    def account_changed(self):
        ready = bool(self.account.cookies().get("auth_token"))
        self.account_checkbox.setEnabled(ready)
        self.logout_button.setEnabled(ready)
        if not ready:
            self.account_checkbox.setChecked(False)
        self.account_label.setText(self.account.error or ("登录状态已保存" if ready else "登录后可下载更多内容"))

    def test_network(self):
        if self.network_job or not self.save_network():
            return
        from .network import DEFAULT_TEST_URL
        urls, _ = parse_input(self.input.toPlainText())
        url = urls[0].url if urls else DEFAULT_TEST_URL
        cookies = self.account.cookies() if self.auth_mode() == "account" else {}
        self.network_job = NetworkWorker(url, self.store.setting("proxy_mode", "system"), self.store.setting("proxy_url"), cookies, self.store.download)
        self.network_job.signals.finished.connect(self.network_finished)
        self.network_button.setEnabled(False)
        self.network_button.setText("测试中…")
        self.detail.setPlainText(f"正在检查目录、X 访问和实际媒体传输…\n测试 URL：{url}\n使用输入框第一个有效 URL；输入框为空时使用内置双动图样例。")
        QThreadPool.globalInstance().start(self.network_job)

    def network_finished(self, result):
        self.network_job = None
        self.network_button.setEnabled(True)
        self.network_button.setText("测试网络")
        self.detail.setPlainText(result["message"] + "\n测试 URL：" + result["url"])
        if not self.closing:
            QMessageBox.information(self, "网络测试通过" if result["ok"] else "网络测试未通过", result["message"])

    def open_login(self):
        if self.login_dialog:
            self.login_dialog.show()
            self.login_dialog.raise_()
            return
        if not self.save_network():
            return
        from .login import LoginDialog
        self.login_dialog = LoginDialog(self.account, self.store.setting("proxy_mode", "system"), self.store.setting("proxy_url"), self)
        self.login_dialog.saved.connect(self.login_saved)
        self.login_dialog.finished.connect(self.login_closed)
        self.login_dialog.show()

    def login_closed(self, _):
        dialog = self.login_dialog
        self.login_dialog = None
        if dialog:
            dialog.shutdown()
            dialog.deleteLater()

    def login_saved(self):
        self.account_changed()
        self.account_checkbox.setChecked(True)
        self.detail.setPlainText("登录状态已加密保存。新任务使用登录模式；之前失败的匿名任务可点击重试，以当前访问方式重新处理。")

    def logout(self):
        if self.engine.busy():
            QMessageBox.information(self, "仍有任务运行", "请等待当前任务结束后清除登录，避免正在运行的账号任务继续使用已读取的凭据。")
            return
        if self.login_dialog:
            self.login_dialog.close()
        self.account.clear()
        self.account_changed()
        self.detail.setPlainText("已清除本软件保存的登录信息，现为匿名模式。")

    def import_text(self):
        if not self.input.toPlainText().strip():
            self.input.setFocus()
            return
        self.start_import(text=self.input.toPlainText())
        self.input.clear()

    def choose_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "选择 URL 文本文件", "", "文本文件 (*.txt)")
        if files:
            self.import_files(files)

    def import_files(self, files):
        if files:
            self.start_import(files=files)

    def start_import(self, text="", files=None):
        if self.closing:
            return
        worker = ImportWorker(self.store, self.auth_mode(), text, files)
        self.import_jobs.append(worker)
        worker.signals.finished.connect(lambda result: self.import_finished(worker, result))
        self.import_label.setText("正在读取并检查全部 URL…")
        self.import_pool.start(worker)

    def import_finished(self, worker, result):
        self.import_jobs.remove(worker)
        message = f"已加入 {len(result['added'])} 条 · 重复 {result['duplicate']} 条 · 无效 {len(result['errors'])} 条"
        self.import_label.setText(message)
        if result["errors"]:
            # 完整导入错误落到本地报告；界面只展示前 200 条，避免超大文本卡顿。
            report = self.store.data / "last-import-errors.txt"
            report.write_text("\n".join(result["errors"]), encoding="utf-8")
            self.detail.setPlainText(message + "\n" + "\n".join(result["errors"][:200]) + f"\n完整错误：{report}")
        else:
            self.detail.setPlainText(message)
        self.tick()

    def retry_selected(self, tree):
        ids = set()
        for item in tree.selectedItems():
            key = item.data(0, Qt.ItemDataRole.UserRole)
            if key[0] == "@account":
                ids.update(item.child(i).data(0, Qt.ItemDataRole.UserRole)[0] for i in range(item.childCount()))
            else:
                ids.add(key[0])
        for tweet_id in ids:
            tweet = self.store.tweet(tweet_id)
            if tweet and tweet["status"] == "failed":
                self.engine.retry(tweet_id, self.auth_mode())
        self.tick()

    def retry_all(self):
        for tweet in self.store.snapshot():
            if tweet["status"] == "failed":
                self.store.update_tweet(tweet["id"], status="queued", error="", error_code="", auth_mode=self.auth_mode())
        self.tick()

    def item_ids(self, item):
        key = item.data(0, Qt.ItemDataRole.UserRole)
        if key[0] == '@account':
            return {item.child(i).data(0, Qt.ItemDataRole.UserRole)[0] for i in range(item.childCount())}
        return {key[0]}

    def make_queue_menu(self, tree, item, completed):
        key = item.data(0, Qt.ItemDataRole.UserRole)
        is_author = key[0] == '@account'
        selected = tree.selectedItems() if item in tree.selectedItems() else [item]
        # 作者操作只作用于右键作者；单项操作不混入多选中的作者分组。
        selected = [item] if is_author else [i for i in selected if i.data(0, Qt.ItemDataRole.UserRole)[0] != '@account']
        ids = set().union(*(self.item_ids(i) for i in selected))
        menu = QMenu(tree)
        if completed:
            clicked_ids = self.item_ids(item)
            mid = key[1] if key[0] != '@account' else ''
            menu.addAction('打开文件所在目录', lambda: self.open_completed_target(clicked_ids, mid, False))
            open_file = menu.addAction('打开该作者所有文件' if is_author else '打开文件',
                                      lambda: self.open_completed_target(clicked_ids, mid, True, is_author))
            files = [m for tid in clicked_ids for m in self.store.media(tid) if m['status']=='done' and (not mid or m['media_id']==mid)]
            open_file.setEnabled(bool(files) if is_author else len(files)==1)
            if not is_author and len(files)!=1:
                open_file.setToolTip('此项目有多个文件，请展开后右键选择具体文件。')
            menu.addSeparator()
            menu.addAction('删除该作者所有已完成记录' if is_author else '删除已完成记录', lambda: self.delete_queue_items(ids, True))
        else:
            menu.addAction('开始下载该作者所有' if is_author else '开始下载', lambda: self.start_queue_items(ids))
            rows = [self.store.tweet(tid) for tid in ids]
            if any(t and t['status'] in ('queued','resolving','downloading','waiting_choice') for t in rows):
                menu.addAction('暂停该作者所有' if is_author else '暂停下载', lambda: self.pause_queue_items(ids))
            if any(t and t['status'] == 'paused' for t in rows):
                menu.addAction('恢复该作者所有' if is_author else '恢复下载', lambda: self.resume_queue_items(ids))
            menu.addSeparator()
            menu.addAction('删除该作者所有下载计划' if is_author else '删除下载计划', lambda: self.delete_queue_items(ids, False))
        return menu

    def show_queue_menu(self, tree, point, completed):
        item = tree.itemAt(point)
        if item is None:
            return
        if not item.isSelected():
            tree.clearSelection()
            item.setSelected(True)
        menu = self.make_queue_menu(tree, item, completed)
        try:
            menu.exec(tree.viewport().mapToGlobal(point))
        finally:
            menu.deleteLater()

    def start_queue_items(self, ids):
        count, waiting = 0, set()
        for tweet_id in ids:
            row = self.store.tweet(tweet_id)
            if not row or row['status'] in ('done','deleting'):
                continue
            if row['status'] == 'waiting_choice':
                waiting.add(row['batch_id'])
            else:
                count += bool(self.engine.retry(tweet_id, self.auth_mode()))
        self.detail.setPlainText(f'已启动或安排重试 {count} 条推文。正在运行的任务不会重复启动。')
        if waiting:
            self.prompt_gif_format(manual=True, batch_ids=waiting, tweet_ids=set(ids))
        self.last_snapshot = None
        self.tick()

    def delete_queue_items(self, ids, completed):
        count = self.engine.delete_completed(ids) if completed else self.engine.delete_plans(ids)
        self.detail.setPlainText(f'已移除或安排移除 {count} 条' + ('已完成记录' if completed else '下载计划') + '。已下载文件和部分下载文件保留。正在运行的任务将在停止后移除。')
        self.last_snapshot = None
        self.tick()

    def pause_queue_items(self, ids):
        count = sum(bool(self.engine.pause(tid)) for tid in ids)
        self.detail.setPlainText(f'已暂停 {count} 条下载计划。点击暂停项目可恢复。')
        self.last_snapshot = None
        self.tick()

    def resume_queue_items(self, ids):
        count = sum(bool(self.engine.resume(tid)) for tid in ids)
        self.detail.setPlainText(f'已恢复 {count} 条下载计划。')
        self.last_snapshot = None
        self.tick()

    def click_queue_item(self, item, column):
        if QApplication.keyboardModifiers() != Qt.KeyboardModifier.NoModifier:
            return
        # 点击展开箭头仅展开；点击作者行则切换该作者的任务。
        from PySide6.QtGui import QCursor
        if self.active.viewport().mapFromGlobal(QCursor.pos()).x() < self.active.visualItemRect(item).left():
            return
        ids = self.item_ids(item)
        rows = [self.store.tweet(tid) for tid in ids]
        if any(t and t['status'] in ('queued','resolving','downloading','waiting_choice') for t in rows):
            self.pause_queue_items(ids)
        elif any(t and t['status'] == 'paused' for t in rows):
            self.resume_queue_items(ids)

    def double_click_item(self, item, completed):
        if completed:
            self.open_item(item, True)
        else:
            ids = self.item_ids(item)
            self.start_queue_items(ids)

    def open_completed_target(self, tweet_ids, mid='', launch_file=False, launch_all=False):
        media = [m for tid in sorted(tweet_ids) for m in self.store.media(tid) if m['status']=='done' and (not mid or m['media_id']==mid)]
        paths = list(dict.fromkeys(self.store.media_path(m) for m in media))
        if not paths:
            QMessageBox.information(self, '没有可打开的文件', '记录中没有已完成的媒体文件。')
            return
        if launch_file:
            if not launch_all and len(paths)!=1:
                QMessageBox.information(self, '请选择具体文件', '请展开推文并右键选择需要打开的文件。')
                return
            failures = []
            for path in paths:
                if not path.is_file():
                    failures.append(f'{path.name}：文件可能已被移动或删除。')
                elif not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
                    failures.append(f'{path.name}：系统未能打开文件，请为该格式设置默认应用。')
            if failures:
                message = '\n'.join(failures)
                self.detail.setPlainText(message)
                QMessageBox.warning(self, '部分文件无法打开' if launch_all else '无法打开文件', message)
        else:
            directories = {p.parent for p in paths}
            if len(directories)==1:
                self.open_directory(next(iter(directories)))
            else:
                QMessageBox.information(self, '多个保存位置', '该项目的文件位于多个目录，请展开后选择具体文件。')

    def show_report(self):
        tweets = self.store.snapshot()
        if not tweets or self.engine.busy() or self.import_jobs or any(t['status'] not in ('done','failed') for t in tweets):
            QMessageBox.information(self, '报告尚未就绪', '请等待全部任务处理结束。等待动图格式选择的任务需先选择格式。')
            return
        if self.report_dialog is not None:
            self.report_dialog.close()
        dialog = DownloadReport(tweets, self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.report_dialog = dialog
        dialog.finished.connect(lambda _: setattr(self, 'report_dialog', None))
        dialog.show()

    def clear_history(self):
        answer = QMessageBox.question(self, "清除下载历史", "清除所有已完成和失败的任务记录？\n已下载文件会保留，未结束的任务不受影响。\n清除后再次导入同一推文可以重新下载。",
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        count = self.engine.clear_history()
        self.detail.setPlainText(f"已清除 {count} 条历史记录，已下载文件保留。未结束的任务仍保留并去重。")
        self.last_snapshot = None
        self.tick()

    def clear_failed(self):
        failed = [t for t in self.store.snapshot() if t['status'] == 'failed']
        if not failed:
            return
        answer = QMessageBox.question(self, "清除所有失败", f"清除 {len(failed)} 条失败任务？\n已下载文件会保留。",
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        # 确认后重新读取状态，只移除当前仍失败的任务。
        ids = {t['id'] for t in self.store.snapshot() if t['status'] == 'failed'}
        count = self.engine.delete_plans(ids)
        self.detail.setPlainText(f"已清除或安排清除 {count} 条失败任务，下载文件保留。")
        self.last_snapshot = None
        self.tick()

    def prompt_gif_format(self, manual=False, batch_ids=None, tweet_ids=None):
        batches = self.store.waiting_batches()
        if self.format_dialog or self.closing or self.in_tray:
            return
        batch = next((b for b in batches if (batch_ids is None or b['id'] in batch_ids)
                      and (manual or b["id"] not in self.dismissed_batches)), None)
        if not batch:
            tweet = next((t for t in self.store.waiting_individual()
                          if (batch_ids is None or t['batch_id'] in batch_ids)
                          and (tweet_ids is None or t['id'] in tweet_ids)
                          and (manual or (t['batch_id'],t['id']) not in self.dismissed_individual)), None)
            if tweet:
                box = IndividualGifDialog(tweet, self)
                self.format_dialog = box
                box.finished.connect(lambda result: self.individual_format_finished(box, tweet, result))
                box.show()
            return
        box = QMessageBox(self)
        box.setWindowTitle("本批次发现动图")
        box.setText("动图保存为哪种格式？")
        box.setInformativeText("可整批统一，也可逐个选择。视频和图片不变。选择后，不允许更改。\n转换GIF需要额外时间与空间，同时受到GIF格式限制。")
        keep = box.addButton("保留 MP4（推荐）", QMessageBox.ButtonRole.AcceptRole)
        convert = box.addButton("转换为 GIF", QMessageBox.ButtonRole.ActionRole)
        individual = box.addButton("逐个选择", QMessageBox.ButtonRole.ActionRole)
        later = box.addButton("稍后选择", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(keep)
        box.setEscapeButton(later)
        box.setWindowModality(Qt.WindowModality.NonModal)
        self.format_dialog = box
        box.finished.connect(lambda _: self.format_finished(box, batch["id"], keep, convert, individual))
        box.show()

    def format_finished(self, box, batch_id, keep, convert, individual):
        clicked = box.clickedButton()
        self.format_dialog = None
        if not self.closing:
            if clicked in (keep, convert, individual):
                mode = 'individual' if clicked is individual else "mp4" if clicked is keep else "gif"
                self.store.choose_batch_format(batch_id, mode)
                self.detail.setPlainText('本批次动图将逐个选择保存格式。' if mode == 'individual' else
                                         "本批次动图将" + ("保留 MP4。" if mode == "mp4" else "转换为 GIF。") + "普通视频和图片不受影响。")
            else:
                self.dismissed_batches.add(batch_id)
            self.last_snapshot = None
        box.deleteLater()
        if not self.closing:
            self.tick()

    def individual_format_finished(self, box, tweet, result):
        self.format_dialog = None
        if not self.closing:
            if result == QDialog.DialogCode.Accepted:
                try:
                    saved = self.store.choose_media_formats(tweet['id'], box.selections())
                    self.detail.setPlainText('动图格式已保存，开始下载。' if saved else '任务状态已变化，请查看下载列表。')
                except ValueError:
                    self.detail.setPlainText('动图列表已变化，请重新选择格式。')
            else:
                self.dismissed_individual.add((tweet['batch_id'],tweet['id']))
            self.last_snapshot = None
        box.deleteLater()
        if not self.closing:
            self.tick()

    def tick(self):
        if self.closing:
            if not self.engine.busy() and not self.import_jobs and not self.network_job:
                self.engine.shutdown()
                self.close()
            return
        if time.monotonic() - self.last_history_cleanup >= 60:
            count = self.engine.clear_history(expired_only=True)
            self.last_history_cleanup = time.monotonic()
            if count:
                self.detail.setPlainText(f"已自动清理 {count} 条超过 72 小时的历史记录，下载文件保留。")
        self.prompt_gif_format()
        snapshot = self.store.snapshot()
        transfer = self.engine.progress_snapshot({(t['id'],m['media_id']) for t in snapshot for m in t['media']})
        for tweet in snapshot:
            for media in tweet['media']:
                media.update(transfer.get((tweet['id'], media['media_id']), {}))
                if tweet['status'] == 'paused' and media['status'] != 'done':
                    media.update(status='paused', speed=0)
        self.check_tray_completion(snapshot)
        self.clear_failed_button.setEnabled(any(t['status'] == 'failed' for t in snapshot))
        self.pause_all_button.setEnabled(any(t['status'] in ('queued','resolving','downloading','waiting_choice') for t in snapshot))
        self.resume_all_button.setEnabled(any(t['status'] == 'paused' for t in snapshot))
        self.report_button.setEnabled(bool(snapshot) and not self.engine.busy() and not self.import_jobs and all(t['status'] in ('done','failed') for t in snapshot))
        # 仅向执行池预派发少量任务，其余持久化在数据库里，避免巨量 Future 占用内存。
        with self.engine.lock:
            capacity = max(0, 4 - len(self.engine.pending))
            pending = set(self.engine.pending)
        for tweet in snapshot:
            if capacity and tweet["status"] == "queued" and tweet["id"] not in pending:
                self.engine.enqueue(tweet["id"])
                capacity -= 1
        while not self.engine.events.empty():
            self.engine.events.get_nowait()
        if snapshot == self.last_snapshot:
            return
        self.last_snapshot = snapshot
        active = [t for t in snapshot if t["status"] != "done"]
        done = [t for t in snapshot if t["status"] == "done"]
        failed = sum(t["status"] == "failed" for t in active)
        self.total_label.setText(f"{len(snapshot)} 条推文  ·  {len(done)} 完成  ·  {failed} 失败")
        self.active_label.setText(f"进行中  {len(active)}" + (f"  / 失败 {failed}" if failed else ""))
        warnings = sum(bool(t.get('warning')) for t in done)
        self.completed_label.setText(f"已完成  {len(done)}" + (f' / 警告 {warnings}' if warnings else ''))
        self.populate(self.active, active)
        self.populate(self.completed, done)

    def populate(self, tree, tweets):
        selected = {tuple(item.data(0, Qt.ItemDataRole.UserRole)) for item in tree.selectedItems()}
        # 只恢复用户已展开的节点；新账号、新推文及跨栏完成的任务默认折叠。
        expanded = set()
        iterator = QTreeWidgetItemIterator(tree)
        while iterator.value():
            item = iterator.value()
            if item.isExpanded():
                expanded.add(tuple(item.data(0, Qt.ItemDataRole.UserRole)))
            iterator += 1
        scroll = tree.verticalScrollBar().value()
        tree.blockSignals(True)
        tree.clear()
        groups = {}
        for tweet in tweets:
            groups.setdefault(tweet["username"].casefold(), []).append(tweet)
        for account, group in groups.items():
            display = next((t["display_name"] for t in reversed(group) if t["display_name"]), "")
            label = f"@{group[0]['username']}" + (f" · {display}" if display else "")
            media = [m for t in group for m in t["media"]]
            done = sum(m["status"] == "done" for m in media)
            failed = sum(t["status"] == "failed" for t in group)
            warnings = sum(bool(t.get('warning')) for t in group)
            status = (f"{failed} 条失败" if failed else "等待选择格式" if any(t["status"] == "waiting_choice" for t in group) else
                      "已完成" if all(t["status"] == "done" for t in group) else "已暂停" if all(t['status']=='paused' for t in group) else "部分暂停" if any(t['status']=='paused' for t in group) else "进行中")
            if warnings and status == '已完成':
                status = '已完成 · 警告'
            account_item = QTreeWidgetItem([label, status, f"{len(group)} 帖 · {done}/{len(media)} 文件"])
            aggregate_status = 'failed' if failed else 'done' if all(t['status']=='done' for t in group) else 'paused' if all(t['status']=='paused' for t in group) else 'downloading'
            progress_media = media + [dict(status=t['status'], downloaded=0, total=0) for t in group if not t['media']]
            set_queue_progress(account_item, progress_media, aggregate_status)
            value, _, color = account_item.data(2, PROGRESS_ROLE)
            summary = (f'{value}%' if value is not None else '失败' if failed else '已暂停' if aggregate_status == 'paused' else '处理中') + f' · {len(group)} 帖'
            account_item.setText(2, summary)
            account_item.setData(2, PROGRESS_ROLE, (value, summary, color))
            key = ("@account", account)
            account_item.setData(0, Qt.ItemDataRole.UserRole, key)
            detail = label + f"\n共 {len(group)} 条推文，{len(media)} 个媒体，{failed} 条失败\n" + "\n".join(t["url"] + ("\n" + t["error"] if t["error"] else "") for t in group)
            detail += ''.join('\n警告：'+t['warning'] for t in group if t.get('warning'))
            account_item.setData(0, Qt.ItemDataRole.UserRole + 1, detail)
            for c in range(3):
                account_item.setToolTip(c, detail)
            account_item.setForeground(1, QColor("#cc3848" if failed else '#a66408' if warnings else "#178264" if status == "已完成" else "#203047"))
            tree.addTopLevelItem(account_item)
            account_item.setExpanded(key in expanded)
            account_item.setSelected(key in selected)
            for tweet in group:
                self.add_tweet_item(account_item, tweet, selected, expanded)
        tree.verticalScrollBar().setValue(scroll)
        tree.blockSignals(False)

    def add_tweet_item(self, account_item, tweet, selected, expanded):
        done = sum(m["status"] == "done" for m in tweet["media"])
        label = f"推文 {tweet['id']}"
        parent = QTreeWidgetItem([label, STATUS[tweet["status"]], f"{done}/{len(tweet['media'])} 文件" if tweet["media"] else "—"])
        set_queue_progress(parent, tweet['media'], tweet['status'])
        parent.setData(0, Qt.ItemDataRole.UserRole, (tweet["id"], ""))
        detail = f"{tweet['url']}\n访问方式：{'已登录账号' if tweet['auth_mode'] == 'account' else '匿名'}\n状态：{STATUS[tweet['status']]}\n{tweet['error']}"
        if tweet.get('warning'):
            detail += '\n警告：'+tweet['warning']
            if tweet['status'] == 'done':
                parent.setText(1, '已完成 · 警告')
        parent.setData(0, Qt.ItemDataRole.UserRole + 1, detail)
        for c in range(3):
            parent.setToolTip(c, detail)
        if tweet["status"] == "failed":
            parent.setText(1, failure_label(tweet))
            parent.setForeground(1, QColor("#cc3848"))
        elif tweet["status"] == "done":
            parent.setForeground(1, QColor('#a66408' if tweet.get('warning') else "#178264"))
        account_item.addChild(parent)
        parent.setExpanded((tweet["id"], "") in expanded)
        parent.setSelected((tweet["id"], "") in selected)
        for media in tweet["media"]:
            if media["status"] == "done":
                progress = human_size(media["total"])
            elif media["total"]:
                progress = f"{min(100,media['downloaded'] * 100 // media['total'])}%"
            else:
                progress = human_size(media["downloaded"]) if media["downloaded"] else "—"
            child = QTreeWidgetItem([f"{media['seq']}  {KINDS.get(media['kind'], '媒体')} · {media['ext'].upper()}", STATUS[media["status"]], progress])
            set_queue_progress(child, [media], media['status'])
            if media['status'] == 'downloading' and media.get('_phase') in ('merging', 'verifying'):
                child.setText(1, '合并中' if media['_phase']=='merging' else '校验中')
            child.setData(0, Qt.ItemDataRole.UserRole, (tweet["id"], media["media_id"]))
            media_detail = f"{tweet['url']}\n文件：{media['filename']}\n状态：{STATUS[media['status']]}  {progress}\n{media['error']}"
            media_detail += "\n保存位置：" + str(self.store.media_path(media))
            if tweet.get('warning'):
                media_detail += '\n警告：'+tweet['warning']
            if media["speed"]:
                media_detail += "\n速度：" + human_size(media["speed"]) + "/s"
            child.setData(0, Qt.ItemDataRole.UserRole + 1, media_detail)
            for c in range(3):
                child.setToolTip(c, media_detail)
            if media["status"] == "failed":
                child.setForeground(1, QColor("#cc3848"))
            parent.addChild(child)
            child.setSelected((tweet["id"], media["media_id"]) in selected)

    def show_details(self, tree):
        if tree.selectedItems():
            self.detail.setPlainText("\n\n".join(item.data(0, Qt.ItemDataRole.UserRole + 1) for item in tree.selectedItems()))

    def open_selected(self, tree):
        if tree.selectedItems():
            self.open_item(tree.selectedItems()[0], True)

    def open_item(self, item, completed):
        if not completed:
            self.detail.setPlainText(item.data(0, Qt.ItemDataRole.UserRole + 1))
            return
        tweet_id, mid = item.data(0, Qt.ItemDataRole.UserRole)
        if tweet_id == "@account":
            self.detail.setPlainText(item.data(0, Qt.ItemDataRole.UserRole + 1))
            rows = [self.store.tweet(item.child(i).data(0, Qt.ItemDataRole.UserRole)[0]) for i in range(item.childCount())]
            directories = set()
            for row in rows:
                if row:
                    media = self.store.media(row['id'])
                    directories.update(self.store.download_directory(m) for m in (media or [row]))
            if len(directories) == 1:
                self.open_directory(next(iter(directories)))
            elif len(directories) > 1:
                QMessageBox.information(self, "多个保存位置", "该账号的文件保存于多个文件夹，请展开并选择具体推文或文件。")
            return
        if not mid:
            tweet = self.store.tweet(tweet_id)
            if tweet:
                media = self.store.media(tweet_id)
                directories = {self.store.download_directory(m) for m in (media or [tweet])}
                if len(directories) == 1:
                    self.open_directory(next(iter(directories)))
                else:
                    QMessageBox.information(self, "多个保存位置", "该推文的文件保存于多个文件夹，请展开并选择具体文件。")
            return
        media = next((m for m in self.store.media(tweet_id) if m["media_id"] == mid), None)
        if not media:
            return
        path = self.store.media_path(media)
        if not path.is_file():
            QMessageBox.warning(self, "文件不存在", "文件可能已被移动或删除。")
            return
        from .linux_support import reveal_file
        reveal_file(path)

    def open_directory(self, directory):
        if not directory.is_dir():
            QMessageBox.warning(self, "目录不存在", "原保存目录已移动、删除或暂时不可用。")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))

    def set_close_action(self, mode):
        self.store.set_setting("close_action", mode)
        self.close_actions[mode].setChecked(True)

    def setup_tray(self):
        icon = application_icon()
        QApplication.instance().setWindowIcon(icon)
        self.setWindowIcon(icon)
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip("XDownloader")
        menu = QMenu(self)
        menu.addAction("打开 XDownloader", self.restore_from_tray)
        menu.addSeparator()
        menu.addAction("直接关闭", self.exit_from_tray)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.tray_activated)
        self.tray.messageClicked.connect(self.restore_from_tray)

    def tray_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.restore_from_tray()

    def restore_from_tray(self):
        self.in_tray = False
        self.tray_tasks.clear()
        self.showNormal()
        self.raise_()
        self.activateWindow()
        for dialog in self.hidden_dialogs:
            if dialog in (self.format_dialog, self.login_dialog, self.report_dialog):
                dialog.show()
        self.hidden_dialogs.clear()
        self.tray.hide()

    def minimize_to_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            QMessageBox.warning(self, "托盘不可用", "系统托盘不可用，请在设置中选择直接关闭。")
            return
        self.in_tray = True
        self.tray_wait_notified = False
        self.tray_tasks = {t["id"] for t in self.store.snapshot() if t["status"] not in ("done", "failed")}
        self.hidden_dialogs = []
        for dialog in (self.format_dialog, self.login_dialog, self.report_dialog):
            if dialog and dialog.isVisible():
                self.hidden_dialogs.append(dialog)
                dialog.hide()
        self.tray.show()
        self.hide()

    def check_tray_completion(self, snapshot):
        if not self.in_tray:
            return
        unfinished = [t for t in snapshot if t["status"] not in ("done", "failed")]
        self.tray_tasks.update(t["id"] for t in unfinished)
        if any(t["status"] == "waiting_choice" for t in unfinished) and not self.tray_wait_notified:
            self.tray_wait_notified = True
            self.tray.showMessage("动图等待选择", "请打开下载器选择保存格式，其余任务会继续下载。")
        if self.tray_tasks and not unfinished and not self.engine.busy() and not self.import_jobs:
            completed = [t for t in snapshot if t["id"] in self.tray_tasks]
            success = sum(t["status"] == "done" for t in completed)
            failed = sum(t["status"] == "failed" for t in completed)
            self.tray.showMessage("下载任务已结束", f"完成 {success} 条，失败 {failed} 条。点击查看。")
            self.tray_tasks.clear()

    def ask_close_action(self):
        dialog = QMessageBox(self)
        dialog.setWindowTitle("关闭 XDownloader")
        dialog.setText("关闭窗口后如何处理？")
        dialog.setInformativeText("本次选择会记住，可在版本号右侧的设置中更改。")
        tray = dialog.addButton("最小化到托盘", QMessageBox.ButtonRole.ActionRole)
        direct = dialog.addButton("直接关闭", QMessageBox.ButtonRole.DestructiveRole)
        dialog.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        dialog.exec()
        return "tray" if dialog.clickedButton() == tray else "exit" if dialog.clickedButton() == direct else None

    def exit_from_tray(self):
        self.exit_requested = True
        self.close()

    def closeEvent(self, event):
        if not self.closing:
            if self.interactive_close:
                mode = "exit" if self.exit_requested else self.store.setting("close_action", "ask")
                remember = mode not in ("tray", "exit")
                if remember:
                    mode = self.ask_close_action()
                if mode is None:
                    event.ignore()
                    return
                if mode == "tray":
                    if remember:
                        self.set_close_action(mode)
                    self.minimize_to_tray()
                    event.ignore()
                    return
                snapshot = self.store.snapshot()
                if self.engine.busy() or any(t["status"] in ("resolving", "downloading", "converting") for t in snapshot):
                    answer = QMessageBox.question(self, "正在下载", "正在下载，是否确认关闭？\n未完成的临时文件将清除，任务保留在下载队列。", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
                    if answer != QMessageBox.StandardButton.Yes:
                        self.exit_requested = False
                        event.ignore()
                        return
                if remember:
                    self.set_close_action(mode)
            self.closing = True
            self.exit_task_ids = {t["id"] for t in self.store.snapshot() if t["status"] not in ("done", "failed", "deleting")}
            self.browser_resolver.shutdown()
            set_browser_resolver(None)
            self.engine.request_stop()
            if self.format_dialog:
                self.format_dialog.close()
        if self.engine.busy() or self.import_jobs or self.network_job:
            self.centralWidget().setEnabled(False)
            self.total_label.setText("正在保存队列并关闭，等待网络请求结束…")
            event.ignore()
            return
        self.timer.stop()
        self.engine.shutdown()
        errors = self.engine.cleanup_interrupted(self.exit_task_ids)
        if errors and self.interactive_close:
            QMessageBox.warning(self, "部分临时文件未能清除", "文件可能被占用，任务已保留。\n" + "\n".join(errors))
        if self.login_dialog:
            self.login_dialog.close()
        self.tray.hide()
        event.accept()
        if self.interactive_close:
            QTimer.singleShot(0, QApplication.instance().quit)
