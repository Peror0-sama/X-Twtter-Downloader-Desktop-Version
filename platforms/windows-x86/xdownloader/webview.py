"""随应用分发的 WebView2 宿主；凭据只通过私有匿名管道传递。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from .core import DownloadError, make_session

_prepared = set()
_prepare_lock = threading.Lock()


def browser_paths():
    base = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))
    folder = base / 'third_party'
    host = folder / 'webview_host' / 'XDownloader.WebHost.exe'
    runtimes = sorted((folder / 'webview2-runtime').glob('*/msedgewebview2.exe'))
    if not host.is_file() or not runtimes:
        raise DownloadError('browser_missing', '内置网页组件缺失，请重新解压完整软件包')
    runtime = runtimes[-1].parent
    # Windows 10 的固定版本运行时要求 AppContainer 对运行时文件具有只读执行权限。
    # 不禁用浏览器沙箱，也不给账号资料或下载目录扩大权限。
    if os.name == 'nt' and sys.getwindowsversion().build < 22000:
        with _prepare_lock:
            if runtime not in _prepared:
                result = subprocess.run(['icacls.exe', str(runtime), '/grant',
                    '*S-1-15-2-2:(OI)(CI)(RX)', '*S-1-15-2-1:(OI)(CI)(RX)'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
                if result.returncode:
                    raise DownloadError('browser_permissions', '无法准备内置网页运行权限，请将软件放入当前用户可写的本地目录')
                _prepared.add(runtime)
    return host, runtime


class BrowserProcess:
    def __init__(self, config):
        host, runtime = browser_paths()
        # 与下载器一致地校验代理地址，禁止把任意文本拼入 Chromium 参数。
        mode, proxy = config.get('proxy_mode', 'system'), config.get('proxy', '')
        with make_session(mode, proxy):
            pass
        if mode == 'manual' and any(c.isspace() or c in '"\'' for c in proxy):
            raise DownloadError('proxy', '代理地址不能包含空格或引号')
        self.profile = Path(tempfile.mkdtemp(prefix='XDownloader-WebView2-'))
        self.events = queue.Queue()
        self.lock = threading.RLock()
        self.closed = False
        options = dict(config, runtime=str(runtime), profile=str(self.profile))
        try:
            environment = {k:v for k,v in os.environ.items() if not k.startswith('WEBVIEW2_')}
            self.process = subprocess.Popen(
                [str(host)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding='utf-8', bufsize=1,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                env=environment,
            )
            self.send(options)
        except Exception:
            shutil.rmtree(self.profile, ignore_errors=True)
            raise DownloadError('browser_start', '内置网页启动失败，请检查组件是否完整') from None
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            for line in self.process.stdout:
                if len(line) > 16 * 1024 * 1024:
                    continue
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        self.events.put(value)
                except ValueError:
                    continue
        finally:
            self.events.put({'type': 'exited'})

    def send(self, value):
        with self.lock:
            if self.closed or self.process.poll() is not None:
                return
            try:
                self.process.stdin.write(json.dumps(value, ensure_ascii=True) + '\n')
                self.process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.send({'op': 'close'})
            self.closed = True
            try:
                self.process.stdin.close()
                self.process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                self.process.kill()
                self.process.wait(timeout=5)
            # 只清理本次创建的临时浏览器资料，不触碰用户下载或账号文件。
            for _ in range(10):
                shutil.rmtree(self.profile, ignore_errors=True)
                if not self.profile.exists():
                    break
                time.sleep(0.1)
