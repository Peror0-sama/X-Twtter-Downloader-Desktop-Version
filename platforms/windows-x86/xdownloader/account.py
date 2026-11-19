"""可选账号支持：Windows 用户级 DPAPI 加密，媒体下载不携带账号凭据。"""
from __future__ import annotations

import ctypes
import json
import os
import threading
import time
from ctypes import wintypes
from pathlib import Path

import requests
from urllib.parse import urljoin, urlsplit

from .core import DownloadError, make_session, parse_page, http_error, network_error, resolve


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(data: bytes, protect: bool) -> bytes:
    if os.name != "nt":
        raise DownloadError("account_storage", "账号加密存储仅支持 Windows")
    source_buffer = ctypes.create_string_buffer(data)
    source = _Blob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = _Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    # CRYPTPROTECT_UI_FORBIDDEN，不绑定机器范围，默认绑定当前 Windows 用户。
    if protect:
        ok = crypt32.CryptProtectData(ctypes.byref(source), "XDownloader", None, None, None, 1, ctypes.byref(result))
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result))
    if not ok:
        raise DownloadError("account_storage", "无法保存或解密登录信息，请在当前 Windows 用户下重新登录")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel32.LocalFree(result.pbData)


class AccountStore:
    REMEMBER_SECONDS = 30 * 24 * 60 * 60

    def __init__(self, directory: Path):
        self.path = directory / "account.bin"
        self.lock = threading.RLock()
        self._cookies = {}
        self.error = ""
        self.expires_at = 0
        if self.path.exists():
            try:
                data = json.loads(_crypt(self.path.read_bytes(), False))
                if data.get('version') == 2:
                    self._cookies = data['cookies']
                    self.expires_at = float(data['expires_at'])
                else:
                    # 兼容旧版加密文件，按原保存时间计算，不因启动而延长。
                    self._cookies = data
                    self.expires_at = self.path.stat().st_mtime + self.REMEMBER_SECONDS
                self.cookies()
            except Exception:
                self._cookies = {}
                self.error = "保存的登录状态无法解密，请重新登录"

    def cookies(self):
        with self.lock:
            if self._cookies and time.time() >= self.expires_at:
                self._cookies.clear()
                self.error = '登录记忆已满 30 天，请重新登录'
            return dict(self._cookies)

    def save(self, cookies):
        filtered = {k: v for k, v in cookies.items() if k in {"auth_token", "ct0", "twid"}}
        if not filtered.get("auth_token"):
            raise DownloadError("login_required", "尚未检测到 X 登录状态，请在页面完成登录")
        with self.lock:
            saved_at = time.time()
            expires_at = saved_at + self.REMEMBER_SECONDS
            encrypted = _crypt(json.dumps(dict(version=2, saved_at=saved_at, expires_at=expires_at, cookies=filtered)).encode(), True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_bytes(encrypted)
            temporary.replace(self.path)
            self._cookies = filtered
            self.expires_at = expires_at
            self.error = ""

    def clear(self):
        with self.lock:
            self.path.unlink(missing_ok=True)
            self._cookies = {}
            self.expires_at = 0


# GUI 提供动态页面解析服务；账号凭据只在本机内存中交给 X 官方页面。
_browser_resolver = None


def set_browser_resolver(resolver):
    global _browser_resolver
    _browser_resolver = resolver


def parse_account_payload(payload, target):
    if not isinstance(payload, dict):
        raise DownloadError('payload_parse', '接口解析失败：X 返回的数据不是预期的对象')
    errors = payload.get('errors') if isinstance(payload, dict) else None
    if errors and not payload.get('data'):
        codes = {str(item.get('code')) for item in errors if isinstance(item, dict)}
        if codes & {'32', '89', '353'}:
            raise DownloadError('login_expired', '登录失效：请在登录窗口重新登录或完成账号验证')
        if codes & {'88', '429'}:
            raise DownloadError('rate_limit', '请求过于频繁：X 暂时限制访问，请稍后手动重试')
        if '63' in codes:
            raise DownloadError('suspended', '作者账号已被停用或封禁，当前无法访问该推文')
        if '179' in codes:
            raise DownloadError('private', '无访问权限：X 明确表示当前账号无权查看此推文')
        if '144' in codes:
            raise DownloadError('not_found', '无法找到推文：X 未提供此内容，但未确认是否已删除')
    if isinstance(payload, dict) and 'data' in payload:
        data = payload.get('data') or {}
        if 'tweetResult' in data:
            result = (data.get('tweetResult') or {}).get('result')
            if not result:
                raise DownloadError('unavailable', '无法访问 · 原因未确认：X 未返回目标推文，不能据此判定已删除或没有权限')
            # 单条接口的不可见结果有时不包含推文 ID，绑定本次请求的目标。
            if not isinstance(result, dict):
                raise DownloadError('payload_parse', '接口解析失败：目标推文数据结构不完整')
            result_id = str(result.get('rest_id') or (result.get('legacy') or {}).get('id_str') or '')
            if result_id and result_id != target.tweet_id:
                raise DownloadError('payload_parse', '接口解析失败：返回的推文 ID 与下载目标不符')
            payload = {'rest_id': target.tweet_id, 'result': result}
        elif not data:
            raise DownloadError('unavailable', '无法访问 · 原因未确认：X 未返回目标推文，不能据此判定已删除或没有权限')
    document = json.dumps(payload, ensure_ascii=False).replace('<', '\\u003c')
    return parse_page('<script type="application/json">' + document + '</script>', target, authenticated=True)


def _authenticated_page(target, mode, proxy, cookies, stop=None):
    url = target.url
    try:
        with make_session(mode, proxy) as session:
            for name in ('auth_token', 'ct0', 'twid'):
                if cookies.get(name):
                    session.cookies.set(name, cookies[name], domain='x.com', path='/', secure=True)
            for _ in range(5):
                if stop and stop.is_set():
                    raise DownloadError('interrupted', '任务已暂停，下次启动可恢复')
                with session.get(url, timeout=(10, 25), stream=True, allow_redirects=False) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        next_url = urljoin(url, response.headers.get('Location', ''))
                        parts = urlsplit(next_url)
                        if parts.scheme != 'https' or parts.hostname != 'x.com' or parts.username or parts.password or parts.port not in (None,443):
                            raise DownloadError('account_access', 'X 返回外部跳转，请在登录窗口检查账号状态')
                        if '/login' in parts.path or '/onboarding/' in parts.path:
                            raise DownloadError('login_expired', 'X 要求重新登录或验证，请打开登录窗口自行完成后重试')
                        url = next_url
                        continue
                    if response.status_code == 401:
                        raise DownloadError('login_expired', 'X 登录状态无效或已过期，请重新登录')
                    if response.status_code == 403:
                        raise DownloadError('account_access', '当前账号无法访问此内容，或 X 要求额外验证')
                    if response.status_code != 200:
                        raise http_error(response.status_code)
                    chunks, size = [], 0
                    for chunk in response.iter_content(65536):
                        if stop and stop.is_set():
                            raise DownloadError('interrupted', '任务已暂停，下次启动可恢复')
                        size += len(chunk)
                        if size > 8 * 1024 * 1024:
                            raise DownloadError('page_size', '推文页面超过解析大小限制')
                        chunks.append(chunk)
                    return parse_page(b''.join(chunks).decode('utf-8', 'replace'), target, authenticated=True)
            raise DownloadError('account_access', 'X 页面重复跳转，请在登录窗口检查账号状态')
    except requests.RequestException as exc:
        raise network_error(exc, '推文页面请求失败') from None


def resolve_authenticated(target, mode, proxy, cookies, stop=None):
    if not cookies.get('auth_token'):
        raise DownloadError('login_required', '请先登录 X')
    if stop and stop.is_set():
        raise DownloadError('interrupted', '任务已暂停，下次启动可恢复')
    if target.kind == 'broadcast':
        # 公开广播的回放及作者数据在无登录的页面中返回；登录页面可能只有应用外壳。
        # 这里只读取官方公开数据，不发送凭据；受限内容仍交给下面的登录流程。
        try:
            return resolve(target, mode, proxy)
        except DownloadError as exc:
            if exc.code not in {'parse', 'sensitive', 'private', 'login_required', 'access_denied', 'not_found', 'page_size'}:
                raise
    try:
        return _authenticated_page(target, mode, proxy, cookies, stop)
    except DownloadError as exc:
        if exc.code not in {'parse', 'sensitive', 'private', 'login_required', 'login_expired', 'account_access', 'page_size', 'timeout', 'network'}:
            raise
        if _browser_resolver is None:
            if exc.code == 'parse':
                raise DownloadError('account_parse', 'X 页面需要动态加载，请在桌面应用中使用登录模式') from None
            raise
        return _browser_resolver.resolve(target, mode, proxy, dict(cookies), stop)
