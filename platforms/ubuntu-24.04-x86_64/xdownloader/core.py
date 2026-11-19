"""匿名媒体解析。仅解析页面数据，不执行页面脚本，不读取登录凭据。"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
URL_PATTERN = re.compile(r"^/([A-Za-z0-9_]{1,50})/status/(\d{5,25})(?:/(?:mediaViewer|(?:photo|video)/\d+))?/?$")
JSON_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')


class DownloadError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False, warning: str = ""):
        super().__init__(message)
        self.code, self.retryable = code, retryable
        self.warning = warning


@dataclass(frozen=True)
class TweetURL:
    url: str
    tweet_id: str
    username: str
    kind: str = 'tweet'


@dataclass(frozen=True)
class Media:
    media_id: str
    index: int
    kind: str
    url: str
    extension: str
    error: str = ""


@dataclass(frozen=True)
class Tweet:
    tweet_id: str
    username: str
    display_name: str
    media: list[Media]
    warning: str = ""


def normalize_url(value: str) -> TweetURL:
    value = value.strip().strip('\ufeff')
    try:
        parts = urlsplit(value)
        valid_host = parts.hostname in {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com", "mobile.x.com"}
        if parts.scheme != "https" or not valid_host or parts.username or parts.password or parts.port not in (None, 443):
            raise ValueError()
        typo = re.fullmatch(r'/i/btoadcasts/([A-Za-z0-9]{10,32})/?', parts.path)
        if typo:
            raise DownloadError('invalid_url', '可能为连接错误，请检查拼写。可能的正确链接：https://x.com/i/broadcasts/' + typo.group(1))
        broadcast = re.fullmatch(r'/i/broadcasts/([A-Za-z0-9]{10,32})/?', parts.path)
        if broadcast:
            identity = broadcast.group(1)
            return TweetURL(f'https://x.com/i/broadcasts/{identity}', identity, '未知账号', 'broadcast')
        match = URL_PATTERN.fullmatch(parts.path)
        if not match:
            raise ValueError()
    except (ValueError, AttributeError):
        raise DownloadError("invalid_url", "链接格式错误：请输入完整的 X 推文或 /i/broadcasts/ 广播链接") from None
    user, tweet_id = match.groups()
    return TweetURL(f"https://x.com/{user}/status/{tweet_id}", tweet_id, user)


def split_concatenated_urls(text: str) -> str:
    return re.sub(r'(?<=[^\r\n])(?=https://)', '\n', text)


def parse_input(text: str, source: str = "输入框"):
    urls, errors = [], []
    for line_no, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        # 一行可有多个 URL；其他非空内容同样列出错误，不静默忽略。
        for token in split_concatenated_urls(line).split():
            try:
                urls.append(normalize_url(token))
            except DownloadError as exc:
                errors.append(f"{source} 第 {line_no} 行：{token[:100]} — {exc}")
    return urls, errors


def read_txt(path: Path) -> str:
    if path.suffix.lower() != ".txt":
        raise DownloadError("file_type", "只接受 .txt 文件")
    if path.stat().st_size > 20 * 1024 * 1024:
        raise DownloadError("file_size", "TXT 文件超过 20 MB，请拆分后导入")
    data = path.read_bytes()
    codecs = ["utf-16"] if data.startswith((b"\xff\xfe", b"\xfe\xff")) else ["utf-8-sig", "gb18030"]
    for codec in codecs:
        try:
            text = data.decode(codec)
            if "\x00" in text:
                raise UnicodeError()
            return text
        except UnicodeError:
            continue
    raise DownloadError("encoding", "无法识别文本编码，请另存为 UTF-8 TXT")


def safe_component(value: str, limit: int = 45) -> str:
    value = unicodedata.normalize("NFC", value)
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", value)
    value = "".join(c for c in value if unicodedata.category(c) not in {"Cf", "Cs"})
    value = value.strip(" .")[:limit].rstrip(" .") or "_"
    if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", value):
        value = "_" + value
    return value


def filename(user: str, display: str, index: int, unique: str, extension: str) -> str:
    return f"{safe_component(user, 30)}_{safe_component(display, 40)}_{index}_{unique}.{extension}"


def validate_media_url(url: str) -> str:
    p = urlsplit(url)
    host = p.hostname or ''
    official = host in {"pbs.twimg.com", "video.twimg.com"} or host.endswith('.video.pscp.tv')
    if p.scheme != "https" or not official or p.username or p.password or p.port not in (None, 443):
        raise DownloadError("media_url", "媒体地址不是受支持的 X 官方媒体地址")
    return url


def make_session(mode: str = "system", proxy: str = "") -> requests.Session:
    session = requests.Session()
    # trust_env=False 同时禁用 .netrc 自动认证；代理显式设置。
    session.trust_env = False
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9", "Referer": "https://x.com/"})
    if mode == "manual":
        p = urlsplit(proxy)
        if p.scheme not in {"http", "https", "socks5", "socks5h"} or not p.hostname:
            raise DownloadError("proxy", "代理格式应为 http://127.0.0.1:端口 或 socks5h://127.0.0.1:端口")
        try:
            if not p.port:
                raise ValueError()
        except ValueError:
            raise DownloadError("proxy", "请填写有效的代理端口") from None
        session.proxies = {"http": proxy, "https": proxy}
    elif mode == "system":
        proxies = urllib.request.getproxies()
        session.proxies = {k: v for k, v in proxies.items() if k in {"http", "https", "all", "no"}}
    elif mode != "direct":
        raise DownloadError("proxy", "未知网络连接方式")
    return session


def http_error(status: int) -> DownloadError:
    if status == 429:
        return DownloadError("rate_limit", "X 请求频率限制（HTTP 429），请稍后重试", True)
    if status in (401, 403):
        return DownloadError("access_denied", f"访问被拒绝（HTTP {status}）：服务器未说明是账号权限还是访问验证，请登录并检查原帖")
    if status in (404, 410):
        return DownloadError("not_found", f"无法访问（HTTP {status}）：该地址未返回内容，无法仅凭此响应确认推文已删除")
    return DownloadError("http", f"服务器返回 HTTP {status}", status >= 500)


def network_error(exc: Exception, stage: str = "") -> DownloadError:
    prefix = f"{stage}：" if stage else ""
    if isinstance(exc, requests.exceptions.ProxyError):
        return DownloadError("proxy", prefix + "代理连接失败，请检查代理地址及代理程序是否运行", True)
    if isinstance(exc, requests.exceptions.SSLError):
        return DownloadError("tls", prefix + "HTTPS 证书校验失败，请检查系统时间及网络证书")
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return DownloadError("timeout", prefix + "连接服务器超时，请检查网络及代理", True)
    # requests 在流式读取时可能将 urllib3 的 ReadTimeoutError 包成 ConnectionError。
    pending, seen = [exc], set()
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        if isinstance(item, requests.exceptions.ReadTimeout) or type(item).__name__ == 'ReadTimeoutError':
            return DownloadError("timeout", prefix + "读取服务器数据超时，请检查网络及代理", True)
        pending.extend(v for v in getattr(item, 'args', ()) if isinstance(v, BaseException))
        pending.extend(v for v in (item.__cause__, item.__context__) if isinstance(v, BaseException))
    if isinstance(exc, requests.exceptions.Timeout):
        return DownloadError("timeout", prefix + "网络请求超时，请检查到 X 和媒体服务器的连接", True)
    return DownloadError("network", prefix + "网络连接中断或无法连接服务器，请检查网络及代理", True)


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _json_documents(soup):
    for script in soup.find_all("script"):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            yield json.loads(raw)
            continue
        except (ValueError, RecursionError):
            pass
        # X SSR 将 GraphQL 响应作为 JSON 字符串嵌在脚本中；仅解码字符串。
        for match in JSON_STRING.finditer(raw):
            try:
                decoded = json.loads(match.group())
                if isinstance(decoded, str) and decoded.startswith(("{", "[")):
                    yield json.loads(decoded)
            except (ValueError, RecursionError):
                continue


def media_from_entity(entity: dict, index: int) -> Media:
    kind = entity.get("type", "unknown")
    media_id = str(entity.get("id_str") or entity.get("media_key") or entity.get("id") or "")
    url, ext, error = "", "mp4", ""
    if kind == "photo":
        url = entity.get("media_url_https") or entity.get("media_url") or ""
        p = urlsplit(url)
        query = parse_qs(p.query)
        ext = (query.get("format") or [Path(p.path).suffix.lstrip(".") or "jpg"])[0]
        query = {k: v[-1] for k, v in query.items()}
        query.update({"format": ext, "name": "orig"})
        url = urlunsplit((p.scheme, p.netloc, p.path, urlencode(query), ""))
    elif kind in ("video", "animated_gif"):
        variants = entity.get("video_info", {}).get("variants", [])
        gifs = [v for v in variants if v.get("content_type") == "image/gif"]
        mp4s = [v for v in variants if v.get("content_type") == "video/mp4" or urlsplit(v.get("url", "")).path.endswith(".mp4")]
        options = gifs if kind == "animated_gif" and gifs else mp4s
        if options:
            variant = max(options, key=lambda v: v.get("bitrate") or v.get("bit_rate") or 0)
            url = variant.get("url", "")
            ext = "gif" if options is gifs else "mp4"
        else:
            error = "没有可直接下载的 GIF/MP4 文件（本版本不转换或合并流媒体）"
    else:
        error = f"暂不支持的媒体类型：{kind}"
    if url:
        try:
            validate_media_url(url)
        except DownloadError as exc:
            url, error = "", str(exc)
    if ext.lower() not in {"jpg", "jpeg", "png", "webp", "gif", "mp4", "avif"}:
        ext, error = "bin", "无法识别媒体格式"
    if not media_id:
        media_id = hashlib.sha256((url or str(index)).encode()).hexdigest()[:24]
    return Media(media_id, index, kind, url, ext.lower(), error)


def page_status_error(text):
    """仅用于没有推文正文的官方错误页，不从用户发表的文字推断状态。"""
    value = str(text or '').lower()
    if any(t in value for t in ('violated the x rules', 'violated the twitter rules', '违反了 x 规则', '違反了 x 規則')):
        return DownloadError('rules', 'X 规则限制：页面明确标记此推文违反规则，当前未提供媒体')
    if any(t in value for t in ('something went wrong', '出了点问题', '發生錯誤', '发生错误')):
        return DownloadError('page_load', 'X 页面加载出错：网站显示错误提示，尚未取得推文数据；请检查网络后手动重试', True)
    if any(t in value for t in ('this post was deleted', 'this tweet was deleted', '此帖子已删除', '此推文已删除', '此貼文已刪除')):
        return DownloadError('deleted', '已删除：X 页面明确标记此推文已被删除，无法下载')
    if any(t in value for t in ("this post doesn’t exist", "this post doesn't exist", "this page doesn’t exist", "this page doesn't exist", '此帖子不存在', '此貼文不存在', '此頁面不存在', '此页面不存在')):
        return DownloadError('not_found', '页面不存在：X 未提供此推文，未说明是地址错误、删除还是不可见')
    if any(t in value for t in ('account suspended', '账号已被冻结', '帳戶已停權', '帳戶已遭停權')):
        return DownloadError('suspended', '作者账号已被停用或封禁，当前无法访问该推文')
    if any(t in value for t in ('these posts are protected', 'only approved followers', '只有获准', '只有經核准', '這些貼文已受保護', '帖子受保护')):
        return DownloadError('private', '无访问权限：此账号的推文受保护，需作者批准关注后查看')
    if any(t in value for t in ('sign in to view', 'log in to view', '登录以查看', '登入以查看')):
        return DownloadError('login_required', 'X 要求登录：请在登录窗口完成登录或验证')
    return None


def unavailable_error(reason):
    if isinstance(reason, (dict, list)):
        reason = json.dumps(reason, ensure_ascii=False)
    reason = str(reason or '').lower()
    if any(word in reason for word in ('violated the x rules', 'violated the twitter rules', '违反了 x 规则', '違反了 x 規則')):
        return DownloadError('rules', 'X 规则限制：接口明确标记此推文违反规则，当前未提供媒体')
    if any(word in reason for word in ('nsfw', 'sensitive', 'age_restricted', 'agerestricted', 'age-restricted', '敏感内容', '年龄限制')):
        return DownloadError('sensitive', '敏感内容或年龄限制：当前访问方式无权查看，请登录有权限的 X 账号后重试')
    if 'suspend' in reason or '账号已被冻结' in reason:
        return DownloadError('suspended', '作者账号已被停用或封禁，当前无法访问该推文')
    if any(word in reason for word in ('protect', 'private', 'authoronly', 'onlyme', 'circle', 'subscriber', 'not authorized', '没有权限', '无权查看', '受保护', '仅自己可见')):
        return DownloadError('private', '无访问权限：X 明确限制当前账号查看此推文；需使用获准账号或满足订阅等条件')
    # BounceDeleted 也用于规则限制，不能按包含 deleted 就认定作者删除。
    if re.search(r'\bdeleted\b', reason) or any(word in reason for word in ('已删除', '已刪除')) or reason.strip() in {'delete', 'tweetdeleted', 'postdeleted'}:
        return DownloadError('deleted', '已删除：X 明确标记此推文已被删除，无法下载')
    if any(word in reason for word in ('notfound', 'not_found', "doesn't exist", 'doesn’t exist', '不存在')):
        return DownloadError('not_found', '无法找到推文：X 未提供此内容；无法进一步确认是删除、地址错误还是不可见')
    if any(word in reason for word in ('withheld', 'country', '地区限制', '法律')):
        return DownloadError('withheld', '地区或法律限制：X 在当前访问地区不提供此内容')
    return DownloadError('unavailable', '无法访问 · 原因未确认：X 未提供此推文，未明确说明是删除、权限还是其他限制')


def author_identity(obj, documents):
    """只读取目标推文绑定的作者；兼容 core/legacy 分离及用户结果包装。"""
    legacy = obj.get('legacy') or {}
    author = (obj.get('core') or {}).get('user_results', {}).get('result') or obj.get('author') or obj.get('user') or legacy.get('user') or {}
    scopes = []

    def collect(value):
        if not isinstance(value, dict):
            return
        scopes.append(value)
        for key in ('core', 'legacy', 'result', 'user'):
            collect(value.get(key))

    collect(author)
    user_id = str(legacy.get('user_id_str') or legacy.get('user_id') or obj.get('user_id_str') or obj.get('user_id') or author.get('rest_id') or '') if isinstance(author,dict) else ''
    if user_id:
        for document in documents:
            for user in _walk(document):
                if str(user.get('rest_id') or user.get('id_str') or '') == user_id and user.get('__typename') == 'User':
                    collect(user)

    def field(name):
        return next((v[name].strip() for v in scopes if isinstance(v.get(name),str) and v[name].strip()), '')

    username, display = field('screen_name'), field('name')
    if not re.fullmatch(r'[A-Za-z0-9_]{1,50}', username):
        username = ''
    return username, display


def external_card_error(candidates):
    links = []
    for obj in candidates:
        card = obj.get('card') or {}
        if not card:
            continue
        urls = (obj.get('legacy') or {}).get('entities', {}).get('urls', [])
        values = [u.get('unwound_url') or u.get('expanded_url') or u.get('url') for u in urls if isinstance(u, dict)]
        bindings = (card.get('legacy') or card).get('binding_values') or []
        if isinstance(bindings, dict):
            bindings = [{'key': k, 'value': v} for k, v in bindings.items()]
        values += [(v.get('value') or {}).get('string_value') for v in bindings
                   if isinstance(v, dict) and v.get('key') == 'card_url']
        for value in values:
            if not isinstance(value, str):
                continue
            value = value.strip()
            try:
                url = urlsplit(value)
                if url.scheme not in ('https', 'http') or not url.hostname or url.username or url.password:
                    continue
                if url.hostname.lower() in ('x.com', 'twitter.com', 'www.x.com', 'www.twitter.com'):
                    continue
                if any(ord(c) < 32 for c in value):
                    continue
            except ValueError:
                continue
            if value not in links:
                links.append(value)
    warning = '外链卡片，未下载。\n' + ('\n'.join('外链：' + url for url in links) if links else 'X 未返回可用的外链地址。')
    return DownloadError('external_card', '外链卡片：没有 X 原生媒体，未下载外站内容', warning=warning)


def parse_page(html: str, target: TweetURL, authenticated=False) -> Tweet:
    soup = BeautifulSoup(html, "html.parser")
    visible = soup.get_text(" ", strip=True)
    # 优先识别 X 返回的限制原因，避免下载限制页面的推荐图或头像。
    sensitive_gate = bool(re.search(r'reason\s*:\s*["\']sensitiveMedia', html) or "This post is only available in the X app" in visible or "此帖子仅在 X 应用中可见" in visible)
    if sensitive_gate and not authenticated:
        raise DownloadError("sensitive", "敏感内容受限：X 不允许当前匿名访问，需要登录或使用 X 应用查看")
    candidates, restrictions = [], []
    documents = list(_json_documents(soup))
    if target.kind == 'broadcast':
        from .broadcast import parse_broadcast
        return parse_broadcast(soup, documents, target)
    for document in documents:
        for obj in _walk(document):
            identity = str(obj.get("rest_id") or obj.get("id_str") or (obj.get('legacy') or {}).get('id_str') or "")
            legacy = obj.get('legacy') or {}
            complete_legacy = all(key in legacy for key in ('full_text', 'entities', 'user_id_str'))
            if identity == target.tweet_id and (obj.get("__typename") == "Tweet" or "extended_entities" in obj or 'media_entities2' in obj or 'extended_entities' in legacy or complete_legacy):
                candidates.append(obj)
            if identity == target.tweet_id:
                result = obj.get('result') or obj
                if isinstance(result,dict) and result.get('__typename') in ('TweetUnavailable','TweetTombstone'):
                    restrictions.append(result.get('unavailable_reason') or result.get('reason') or result.get('tombstone') or result)
            # TweetDetail 的 tombstone 常没有 rest_id，以精确匹配的时间线条目绑定目标。
            if obj.get('entryId') == 'tweet-' + target.tweet_id:
                content = obj.get('content') or {}
                item = content.get('itemContent') or (content.get('item') or {}).get('itemContent') or {}
                result = (item.get('tweet_results') or {}).get('result') or {}
                if result.get('__typename') in ('TweetUnavailable', 'TweetTombstone'):
                    restrictions.append(result.get('unavailable_reason') or result.get('reason') or result.get('tombstone') or result)
    identities = [author_identity(obj, documents) for obj in candidates]
    user, display = max(identities, key=lambda pair: bool(pair[0])+bool(pair[1]), default=('', ''))
    warnings = []
    if not user:
        user = target.username
        warnings.append('未能确认作者账号，暂用链接中的 @'+user+' 命名')
    if not display:
        display = '名称未知'
        warnings.append('未能取得作者显示名称，文件名暂用“名称未知”')
    for obj in candidates:
        legacy = obj.get("legacy", obj)
        entities = obj.get("media_entities2")
        if entities is None:
            entities = legacy.get("extended_entities", {}).get("media", [])
        if not entities:
            continue
        seen, media = set(), []
        for entity in entities:
            item = media_from_entity(entity, len(media) + 1)
            if item.media_id in seen:
                continue
            seen.add(item.media_id)
            media.append(item)
        return Tweet(target.tweet_id, user, display, media, '；'.join(warnings))
    if restrictions:
        errors = [unavailable_error(reason) for reason in restrictions]
        raise next((e for e in errors if e.code != 'unavailable'), errors[0])
    if any('media_entities2' in obj or 'extended_entities' in obj or
           any(key in (obj.get('legacy') or {}) for key in ('entities', 'full_text', 'extended_entities'))
           for obj in candidates):
        if any(obj.get('card') for obj in candidates):
            raise external_card_error(candidates)
        raise DownloadError("no_media", "该推文没有可下载的图片、视频或 GIF")
    if sensitive_gate:
        raise DownloadError('sensitive', 'X 未向当前账号返回媒体，内容或年龄权限仍受限制')
    lower = visible.lower()
    if any(x in lower for x in ('this post was deleted', 'this tweet was deleted', '此帖子已删除', '此推文已删除')):
        raise DownloadError('deleted', '已删除：X 页面明确标记此推文已被删除，无法下载')
    if any(x in lower for x in ("this post doesn’t exist", "this post doesn't exist", "此帖子不存在")):
        raise DownloadError('not_found', '无法找到推文：页面提示不存在，但未明确说明删除或不可见的具体原因')
    if any(x in lower for x in ("age-restricted", "sensitive content", "敏感内容")):
        raise DownloadError("sensitive", "敏感内容受限，当前访问方式无法取得媒体")
    if any(x in lower for x in ("protected account", "these posts are protected", "只有获准", "帖子受保护")):
        raise DownloadError("private", "受保护的账号，当前访问方式无法访问")
    if any(x in lower for x in ("sign in to view", "log in to view", "登录以查看")):
        raise DownloadError("login_required", "X 要求有效登录才能查看此内容")
    raise DownloadError("parse", "未能取得目标推文媒体信息：页面结构变化或匿名访问受限；请检查原链接并稍后重试")


def resolve(target: TweetURL, mode="system", proxy="") -> Tweet:
    try:
        with make_session(mode, proxy) as session:
            # 无认证信息、无浏览器 Cookies；仅请求用户指定的推文。
            with session.get(target.url, timeout=(10, 30), stream=True) as response:
                if response.status_code != 200:
                    raise http_error(response.status_code)
                chunks, count = [], 0
                for chunk in response.iter_content(65536):
                    count += len(chunk)
                    if count > 8 * 1024 * 1024:
                        raise DownloadError("page_size", "推文页面超过解析大小限制")
                    chunks.append(chunk)
        return parse_page(b"".join(chunks).decode("utf-8", "replace"), target)
    except requests.RequestException as exc:
        raise network_error(exc) from None
