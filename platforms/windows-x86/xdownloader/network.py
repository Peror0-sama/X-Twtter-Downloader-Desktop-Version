from pathlib import Path
import uuid

import requests

from .core import DownloadError, http_error, make_session, network_error, normalize_url, resolve, validate_media_url

DEFAULT_TEST_URL = "https://x.com/pellas00/status/2121256800184148093"


def probe_network(url: str, mode: str, proxy: str, cookies: dict, directory: Path):
    steps = []
    probe = directory / (".network-check-" + uuid.uuid4().hex + ".tmp")
    try:
        try:
            with probe.open("xb") as stream:
                stream.write(b"XDownloader write test")
            probe.unlink()
            steps.append("✓ 下载目录可写")
        except OSError:
            raise DownloadError("directory", "下载目录不可写，请检查权限和磁盘空间") from None
        target = normalize_url(url)
        if cookies:
            from .account import resolve_authenticated
            tweet = resolve_authenticated(target, mode, proxy, cookies)
        else:
            tweet = resolve(target, mode, proxy)
        steps.append(f"✓ X 推文访问及解析成功，发现 {len(tweet.media)} 个媒体")
        available = [m for m in tweet.media if m.url and not m.error]
        if not available:
            raise DownloadError("no_direct_media", "测试推文没有当前可直接下载的媒体")
        media = available[0]
        validate_media_url(media.url)
        with make_session(mode, proxy) as session:
            with session.get(media.url, headers={"Range": "bytes=0-65535"}, stream=True, timeout=(10, 20), allow_redirects=False) as response:
                if response.status_code not in (200, 206):
                    raise http_error(response.status_code)
                if "text/html" in response.headers.get("Content-Type", ""):
                    raise DownloadError("content_type", "媒体服务器返回错误页面")
                data = next(response.iter_content(65536), b"")
                valid = data.startswith((b"\xff\xd8\xff", b"\x89PNG", b"GIF87a", b"GIF89a")) or data[4:8] == b"ftyp" or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")
                if not valid:
                    raise DownloadError("content_type", "媒体响应不符合预期的图片/GIF/MP4 格式")
        steps.append(f"✓ 媒体服务器已成功传输 {len(data):,} 字节，格式检查通过")
        steps.append("测试通过：当前环境可下载此测试推文的媒体。测试未保存媒体文件，也未加入队列。")
        return {"ok": True, "message": "\n".join(steps), "url": target.url}
    except requests.RequestException as exc:
        error = network_error(exc)
    except DownloadError as exc:
        error = exc
    except Exception as exc:
        error = DownloadError("internal", f"网络测试未完成（{type(exc).__name__}）")
    if error.code in {"sensitive", "private", "login_required", "login_expired", "account_access", "no_media", "not_found"}:
        steps.append("X 已响应，但测试内容/账号受限；尚未证明媒体下载链路可用。")
    steps.append("✗ " + str(error))
    return {"ok": False, "message": "\n".join(steps), "url": url}
