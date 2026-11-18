"""用 X 官方页面自身返回的数据解析目标推文，不依赖第三方提取器。"""
from __future__ import annotations

import json
import threading
import time

from PySide2.QtCore import QObject

from .account import parse_account_payload
from .core import DownloadError, parse_page


def capture_script(tweet_id):
    # 仅观察本次目标的官方 GraphQL 响应；不读取 Cookie、请求头或其他推文响应。
    return r'''
(() => {
    if (location.hostname !== 'x.com') return;
    const id = __TARGET__;
    const pending = [];
    Object.defineProperty(window, '__xdResponses', {value: pending});
    function endpoint(value) {
        try {
            const u = new URL(value, location.href);
            return u.origin === 'https://x.com' && u.pathname.includes('/graphql/') ? u : null;
        } catch (_) { return null; }
    }
    function matches(value) {
        try {
            let v = typeof value === 'string' ? JSON.parse(value) : value;
            if (v && v.variables !== undefined) v = typeof v.variables === 'string' ? JSON.parse(v.variables) : v.variables;
            return !!v && String(v.focalTweetId || v.tweetId || v.tweet_id || '') === id;
        } catch (_) { return false; }
    }
    async function relevant(url, body) {
        if (!url) return false;
        // URL 已明确指定目标时以它为准，避免正文冲突而误收其他推文。
        if (url.searchParams.has('variables')) return matches(url.searchParams.get('variables'));
        try {
            if (body instanceof URLSearchParams || (typeof FormData !== 'undefined' && body instanceof FormData)) return matches(body.get('variables'));
            if (typeof Blob !== 'undefined' && body instanceof Blob) {
                if (body.size > 65536) return false;
                body = await body.text();
            }
            if (typeof body !== 'string' || body.length > 65536) return false;
            if (matches(body)) return true;
            return matches(new URLSearchParams(body).get('variables'));
        } catch (_) { return false; }
    }
    function save(status, raw) {
        if (pending.length >= 4 || raw.length > 8388608) return;
        try { pending.push({status: status, payload: JSON.parse(raw)}); } catch (_) {}
    }
    const fetchOriginal = window.fetch;
    window.fetch = function(...args) {
        const input = args[0], init = args[1];
        const url = endpoint(typeof input === 'string' ? input : input?.url || input?.href);
        let wanted = Promise.resolve(false);
        if (url) {
            if (url.searchParams.has('variables') || init?.body !== undefined) wanted = relevant(url, init?.body);
            else if (typeof Request !== 'undefined' && input instanceof Request) {
                // 仅克隆官方目标接口的 Request，不能消费网页将要发送的正文。
                try { wanted = input.clone().text().then(body => relevant(url, body)).catch(() => false); } catch (_) {}
            }
        }
        const result = fetchOriginal.apply(this, args);
        // 不等待正文识别，也不改变网页取得的 Promise/Response 或请求参数。
        if (url) result.then(response => wanted.then(yes => {
            if (yes) return response.clone().text().then(raw => save(response.status, raw));
        })).catch(() => {});
        return result;
    };
    const openOriginal = XMLHttpRequest.prototype.open;
    const sendOriginal = XMLHttpRequest.prototype.send;
    const requests = new WeakMap();
    XMLHttpRequest.prototype.open = function(method, url, ...rest) {
        requests.set(this, endpoint(url));
        return openOriginal.call(this, method, url, ...rest);
    };
    XMLHttpRequest.prototype.send = function(...args) {
        const url = requests.get(this);
        if (url) {
            const wanted = relevant(url, args[0]);
            // loadend 在成功、失败、取消时均触发；复用 XHR 不会遗留上次的监听。
            this.addEventListener('loadend', () => {
                try {
                    const status = this.status;
                    const raw = this.responseType === 'json' ? JSON.stringify(this.response) : this.responseText;
                    wanted.then(yes => { if (yes) save(status, raw); }).catch(() => {});
                } catch (_) {}
            }, {once: true});
        }
        return sendOriginal.apply(this, args);
    };
})();
'''.replace('__TARGET__', json.dumps(tweet_id))


class BrowserResolver(QObject):
    """每个任务使用独立固定版本 WebView2，避免登录窗口与解析页互相影响。"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.closed = False
        self.lock = threading.RLock()
        self.processes = set()

    def resolve(self, target, mode, proxy, cookies, stop=None):
        import queue
        from .webview import BrowserProcess
        with self.lock:
            if self.closed or (stop and stop.is_set()):
                raise DownloadError('interrupted', '应用正在关闭')
            process = BrowserProcess(dict(mode='resolve', url=target.url, tweet_id=target.tweet_id,
                                          cookies=dict(cookies), proxy_mode=mode, proxy=proxy))
            self.processes.add(process)
        deadline = time.monotonic() + 90
        last_error = None
        try:
            while time.monotonic() < deadline:
                if self.closed or (stop and stop.is_set()):
                    raise DownloadError('interrupted', '任务已暂停，下次启动可恢复')
                try:
                    event = process.events.get(timeout=0.15)
                except queue.Empty:
                    continue
                kind = event.get('type')
                if kind in ('navigation', 'address'):
                    from urllib.parse import urlsplit
                    path = urlsplit(event.get('url', '')).path
                    if '/login' in path or '/onboarding/' in path:
                        raise DownloadError('login_expired', 'X 要求重新登录或验证，请在登录窗口完成后重试')
                    if kind == 'navigation' and not event.get('ok'):
                        last_error = DownloadError('account_access', 'X 网页加载失败：' + event.get('error', 'unknown'), True)
                elif kind in ('html', 'payload'):
                    status = event.get('status', 200)
                    if status == 429:
                        raise DownloadError('rate_limit', 'X 请求频率限制，请稍后重试', True)
                    if status in (401, 403):
                        raise DownloadError('account_access', 'X 拒绝当前账号请求，请在登录窗口检查账号状态')
                    if status != 200:
                        continue
                    try:
                        if kind == 'html':
                            result = parse_page(event.get('html', ''), target, authenticated=True)
                        else:
                            result = parse_account_payload(event.get('payload'), target)
                    except DownloadError as exc:
                        if exc.code in ('parse', 'sensitive'):
                            continue
                        raise
                    return result
                elif kind == 'error':
                    raise DownloadError(event.get('code', 'browser_process'), '内置网页运行失败：' + event.get('detail', 'unknown'), True)
                elif kind in ('exited', 'closed'):
                    raise DownloadError('browser_process', '内置网页提前退出，请重试', True)
            raise last_error or DownloadError('account_parse', 'X 网页未返回目标内容，请检查登录、内容权限或网络后重试', True)
        finally:
            process.close()
            with self.lock:
                self.processes.discard(process)

    def shutdown(self):
        with self.lock:
            self.closed = True
            active = list(self.processes)
        for process in active:
            process.close()
