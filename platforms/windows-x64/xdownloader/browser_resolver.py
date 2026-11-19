"""用 X 官方页面自身返回的数据解析目标推文，不依赖第三方提取器。"""
from __future__ import annotations

import json
import threading
import time

from PySide6.QtCore import QObject

from .account import parse_account_payload
from .core import DownloadError, parse_page, page_status_error, normalize_url


def page_status_script(tweet_id):
    # 只读取官方错误组件；有推文正文时不按正文中的关键词判定状态。
    return r'''
(() => {
    const id = __TARGET__;
    const main = document.querySelector('main');
    const articles = Array.from(document.querySelectorAll('main article'));
    const target = articles.find(a => Array.from(a.querySelectorAll('time')).some(t => {
        const link = t.closest('a');
        return link && new URL(link.href, location.href).pathname.split('/status/')[1]?.split('/')[0] === id;
    }));
    const errors = Array.from(document.querySelectorAll('[data-testid="error-detail"], [data-testid="emptyState"], [data-testid="tombstone"]'))
        .filter(e => !e.closest('[data-testid="tweetText"]') &&
            (target ? e.closest('article') === target : articles.length === 0));
    return JSON.stringify({url:location.href, ready_state:document.readyState,
        has_main:!!main, has_article:articles.length > 0, has_target:!!target,
        error_text:errors.map(e => e.innerText || '').join('\n').slice(0,4096),
        text:articles.length ? '' : ((main || document.body)?.innerText || '').slice(0,4096)});
})();
'''.replace('__TARGET__', json.dumps(tweet_id))


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
                                          cookies=dict(cookies), proxy_mode=mode, proxy=proxy,
                                          page_status_script=page_status_script(target.tweet_id)))
            self.processes.add(process)
        deadline = time.monotonic() + 90
        last_error = None
        error_priority = -1
        status_deadline = None
        next_status = 0.0
        page_seen = False

        def remember(error, priority, grace=False):
            nonlocal last_error, error_priority, status_deadline
            if priority >= error_priority:
                last_error, error_priority = error, priority
            if grace and status_deadline is None:
                status_deadline = time.monotonic() + 10

        try:
            while time.monotonic() < deadline + getattr(stop, 'paused_seconds', 0):
                if self.closed or (stop and stop.is_set()):
                    raise DownloadError('interrupted', '任务已暂停，下次启动可恢复')
                now = time.monotonic()
                if status_deadline is not None and now >= status_deadline + getattr(stop, 'paused_seconds', 0):
                    raise last_error
                if now >= next_status:
                    process.send({'op': 'page_status'})
                    next_status = now + 2
                try:
                    event = process.events.get(timeout=0.15)
                except queue.Empty:
                    continue
                kind = event.get('type')
                if kind == 'page_status':
                    try:
                        status = json.loads(event.get('result') or '{}')
                        same_target = isinstance(status, dict) and normalize_url(status.get('url', '')).tweet_id == target.tweet_id
                    except (ValueError, TypeError, DownloadError):
                        same_target = False
                    if same_target:
                        page_seen = page_seen or bool(status.get('has_main') or status.get('has_article'))
                        text = status.get('error_text') or (status.get('text') if not status.get('has_article', True) else '')
                        reason = page_status_error(text)
                        if reason:
                            if reason.code == 'page_load':
                                # 首屏可能短暂出现错误占位，允许已发出的接口继续完成。
                                remember(reason, 6, grace=True)
                            else:
                                raise reason
                elif kind in ('navigation', 'address'):
                    from urllib.parse import urlsplit
                    path = urlsplit(event.get('url', '')).path
                    if '/login' in path or '/onboarding/' in path:
                        raise DownloadError('login_expired', 'X 要求重新登录或验证，请在登录窗口完成后重试')
                    if kind == 'navigation' and not event.get('ok'):
                        remember(DownloadError('page_load', 'X 网页加载失败：' + event.get('error', 'unknown'), True), 6)
                elif kind in ('html', 'payload'):
                    status = event.get('status', 200)
                    if status == 429:
                        raise DownloadError('rate_limit', 'X 请求频率限制，请稍后重试', True)
                    if status == 401:
                        raise DownloadError('login_expired', '登录失效（HTTP 401）：请重新登录或完成账号验证')
                    if status == 403:
                        remember(DownloadError('account_access', '接口访问被拒绝（HTTP 403）：X 未说明具体原因，请在登录窗口检查账号及原帖'), 7, grace=True)
                        continue
                    if status != 200:
                        remember(DownloadError('api_http', f'目标接口请求失败（HTTP {status}），不能据此判断推文状态', status >= 500), 6, grace=True)
                        continue
                    try:
                        if kind == 'html':
                            result = parse_page(event.get('html', ''), target, authenticated=True)
                        else:
                            result = parse_account_payload(event.get('payload'), target)
                    except DownloadError as exc:
                        if exc.code in ('unavailable', 'not_found', 'no_media', 'external_card', 'sensitive', 'private', 'rules', 'deleted', 'suspended', 'withheld'):
                            remember(exc, 8 if exc.code != 'unavailable' else 4, grace=True)
                            # 空接口响应先等待页面错误提示，区分“不存在”和未知原因。
                            continue
                        if exc.code in ('parse', 'payload_parse'):
                            if kind == 'payload':
                                remember(DownloadError('payload_parse', '接口解析失败：已收到目标接口响应，但未能识别完整的目标推文数据'), 3)
                            else:
                                remember(DownloadError('account_parse', '页面解析未完成：初始 HTML 不含目标媒体，等待期间未取得可解析的目标接口响应'), 1)
                            continue
                        raise
                    except (TypeError, ValueError, AttributeError, KeyError, RecursionError):
                        remember(DownloadError('payload_parse', '接口解析失败：X 返回的数据结构异常'), 3)
                        continue
                    return result
                elif kind == 'capture_error':
                    if event.get('target', True):
                        detail = str(event.get('detail', 'unknown'))
                        # 仅接收异常类型，不把可能带凭据的异常消息放进界面。
                        import re
                        detail = detail if re.fullmatch(r'[A-Za-z0-9_]{1,64}', detail) else 'unknown'
                        remember(DownloadError('capture', '目标接口读取失败：浏览器未能提取响应（' + detail + '）', True), 5)
                elif kind == 'error':
                    raise DownloadError(event.get('code', 'browser_process'), '内置网页运行失败：' + event.get('detail', 'unknown'), True)
                elif kind in ('exited', 'closed'):
                    raise DownloadError('browser_process', '内置网页提前退出，请重试', True)
            if not page_seen and error_priority <= 1:
                raise DownloadError('page_load', '页面加载超时：未形成推文页面，也未取得目标数据；请检查网络或在登录窗口查看加载状态', True)
            raise last_error or DownloadError('account_parse', '接口等待超时：页面已打开，但未取得目标推文数据；无法据此确认推文状态', True)
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
