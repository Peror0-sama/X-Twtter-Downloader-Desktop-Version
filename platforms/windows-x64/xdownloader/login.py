from __future__ import annotations

import queue
import threading
from urllib.parse import urlsplit
from PySide6.QtCore import QEvent, QTimer, Qt, Signal
from PySide6.QtWidgets import QApplication, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout, QWidget
from .core import DownloadError
from .webview import BrowserProcess


class BrowserArea(QWidget):
    resized = Signal()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.resized.emit()


class LockedAddressEdit(QLineEdit):
    """首次点击只揭开覆膜，第二次点击才进入编辑，避免接收网页登录输入。"""
    def __init__(self, url, parent=None):
        super().__init__(url, parent)
        self.page_url = url
        self.edit_state = 'locked'
        self.cover = QPushButton('🔒 网址已锁定 · 点击解锁', self)
        self.cover.setObjectName('addressCover')
        self.cover.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.cover.setAutoDefault(False)
        self.cover.setDefault(False)
        self.cover.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cover.clicked.connect(self.unlock)
        QApplication.instance().installEventFilter(self)
        self.lock()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.cover.setGeometry(self.rect())

    def unlock(self):
        self.edit_state = 'armed'
        self.cover.hide()
        # 保持只读且不取得键盘焦点，下一次实际点击才允许修改。

    def lock(self):
        self.edit_state = 'locked'
        self.setReadOnly(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.clearFocus()
        self.setText(self.page_url)
        self.cover.show()
        self.cover.raise_()

    def set_page_url(self, url):
        self.page_url = url
        if self.edit_state != 'editing':
            self.setText(url)

    def mousePressEvent(self, event):
        if self.edit_state == 'armed' and event.button() == Qt.MouseButton.LeftButton:
            self.edit_state = 'editing'
            self.setReadOnly(False)
            self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
            self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
            self.setFocus(Qt.FocusReason.MouseFocusReason)
        if self.edit_state == 'editing':
            super().mousePressEvent(event)
        else:
            event.accept()

    def mouseDoubleClickEvent(self, event):
        if self.edit_state == 'armed':
            self.mousePressEvent(event)
        elif self.edit_state == 'editing':
            super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        if self.edit_state == 'editing':
            super().keyPressEvent(event)
        else:
            event.accept()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        if self.edit_state == 'editing':
            self.lock()

    def eventFilter(self, watched, event):
        if self.isVisible() and self.edit_state != 'locked':
            if event.type() == QEvent.Type.WindowDeactivate and watched is self.window():
                self.lock()
            elif event.type() == QEvent.Type.MouseButtonPress and isinstance(watched, QWidget) and watched is not self and not self.isAncestorOf(watched):
                self.lock()
        return super().eventFilter(watched, event)


class LoginDialog(QDialog):
    saved = Signal()

    def __init__(self, account, mode, proxy, parent=None):
        super().__init__(parent)
        self.account, self.mode, self.proxy = account, mode, proxy
        self.process = None
        self.setWindowTitle("登录 X · XDownloader")
        self.setObjectName("loginDialog")
        self.setMinimumSize(720, 500)
        self.resize(1000, 780)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        header = QFrame()
        header.setObjectName("loginHeader")
        heading = QHBoxLayout(header)
        heading.setContentsMargins(18, 12, 18, 12)
        title = QLabel("登录 X")
        title.setObjectName("loginTitle")
        heading.addWidget(title)
        heading.addStretch()
        brand = QLabel("XDownloader")
        brand.setObjectName("loginBrand")
        heading.addWidget(brand)
        layout.addWidget(header)
        self.address = LockedAddressEdit('https://x.com/home' if account.cookies() else 'https://x.com/i/flow/login', self)
        self.address.setPlaceholderText('https://x.com/…')
        self.address.returnPressed.connect(self.navigate)
        layout.addWidget(self.address)
        self.view = BrowserArea(self)
        self.view.setObjectName('loginBrowser')
        self.view.resized.connect(self.resize_browser)
        layout.addWidget(self.view, 1)
        row = QHBoxLayout()
        self.status = QLabel('加载中…')
        self.status.setObjectName('loginStatus')
        row.addWidget(self.status, 1)
        reload_button = QPushButton('重新加载')
        reload_button.clicked.connect(lambda: self.send({'op': 'reload'}))
        row.addWidget(reload_button)
        close = QPushButton('关闭')
        close.clicked.connect(self.reject)
        row.addWidget(close)
        self.use = QPushButton('使用此登录')
        self.use.setObjectName('loginPrimary')
        self.use.setEnabled(False)
        self.use.clicked.connect(lambda: self.send({'op': 'cookies'}))
        row.addWidget(self.use)
        layout.addLayout(row)
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
            button.setDefault(False)
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.poll)
        self.finished.connect(self.shutdown)
        self.setStyleSheet("""
            QDialog#loginDialog { background:#f3f5f9; }
            QLabel { font-family:'Microsoft YaHei UI','Segoe UI'; font-size:12px; color:#203047; }
            QFrame#loginHeader { background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #12233f,stop:1 #244c76); border-radius:10px; }
            QLabel#loginTitle { color:white; font-size:22px; font-weight:650; }
            QLabel#loginBrand { color:#b9cee6; font-size:13px; }
            QFrame#loginBrowser { background:white; border:1px solid #dfe5ee; border-radius:8px; }
            QLabel#loginStatus { color:#6d7b8d; }
            QPushButton { font-family:'Microsoft YaHei UI','Segoe UI'; font-size:12px; color:#203047; background:#f6f8fc; border:1px solid #dce3ed; border-radius:7px; padding:9px 16px; }
            QPushButton:hover { background:#eaf1fd; border-color:#9dbce9; }
            QPushButton#loginPrimary { background:#1765d8; color:white; border:1px solid #1765d8; }
            QPushButton#loginPrimary:hover { background:#1055bb; }
            QPushButton#loginPrimary:disabled { color:#98a2b0; background:#edf1f6; border-color:#dce3ed; }
            QLineEdit { background:white; color:#203047; border:1px solid #dce3ed; border-radius:7px; padding:8px 12px; min-height:22px; }
            QPushButton#addressCover { background:rgba(232,240,250,245); color:#536b88; border:1px solid #ccdcee; border-radius:7px; padding:0; }
            QPushButton#addressCover:hover { background:#ddeafa; }
        """)

    def showEvent(self, event):
        super().showEvent(event)
        if self.process is None:
            QTimer.singleShot(0, self.start_browser)

    def start_browser(self):
        if self.process is not None or not self.isVisible():
            return
        try:
            self.process = BrowserProcess(dict(mode='login', parent=int(self.view.winId()),
                url=self.address.text(), proxy_mode=self.mode, proxy=self.proxy,
                cookies=self.account.cookies()))
        except DownloadError as exc:
            self.status.setText(str(exc))
            return
        self.timer.start()

    def send(self, command):
        if self.process:
            self.process.send(command)

    def resize_browser(self):
        ratio = self.view.devicePixelRatioF()
        self.send(dict(op='resize', width=max(1, round(self.view.width()*ratio)),
                       height=max(1, round(self.view.height()*ratio))))

    def navigate(self):
        if self.address.edit_state != 'editing':
            return
        value = self.address.text().strip()
        try:
            u = urlsplit(value)
            valid = u.scheme == 'https' and u.hostname == 'x.com' and u.port in (None, 443) and not u.username and not u.password
        except ValueError:
            valid = False
        if not valid:
            self.status.setText('请输入 https://x.com/ 开头的网址')
            return
        self.status.setText('加载中…')
        self.send(dict(op='navigate', url=value))
        self.address.set_page_url(value)
        self.address.lock()

    def poll(self):
        if not self.process:
            return
        while True:
            try:
                event = self.process.events.get_nowait()
            except queue.Empty:
                break
            kind = event.get('type')
            if kind == 'ready':
                self.resize_browser()
                self.use.setEnabled(True)
            elif kind == 'browser_focus':
                self.address.lock()
            elif kind in ('navigation', 'address'):
                self.address.set_page_url(event.get('url', self.address.page_url))
                if kind == 'navigation':
                    self.status.setText('' if event.get('ok') else '页面加载失败：' + event.get('error', 'unknown'))
            elif kind == 'cookies':
                try:
                    self.account.save(event.get('cookies', {}))
                except (DownloadError, OSError) as exc:
                    QMessageBox.warning(self, '无法保存登录', str(exc))
                else:
                    self.saved.emit()
                    self.accept()
                    return
            elif kind in ('error', 'exited'):
                self.status.setText('内置网页运行失败，请关闭窗口后重试')
                self.use.setEnabled(False)

    def shutdown(self, *_):
        self.timer.stop()
        if self.process:
            process, self.process = self.process, None
            # 等待网页进程退出及删除临时资料可能耗时数秒，不能占用 Qt 界面线程。
            threading.Thread(target=process.close, name='login-browser-cleanup', daemon=False).start()
