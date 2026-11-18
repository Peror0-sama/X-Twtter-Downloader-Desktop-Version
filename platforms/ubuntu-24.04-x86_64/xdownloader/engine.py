from __future__ import annotations
from .linux_support import rename_without_overwrite as __LINUX_RENAME__

import hashlib
import os
import queue
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from PIL import Image

from .core import DownloadError, http_error, make_session, network_error, normalize_url, resolve, validate_media_url
from .storage import Store
from .conversion import convert_mp4_to_gif


def verify_file(path: Path) -> tuple[str, int, str]:
    size = path.stat().st_size
    if not size:
        raise DownloadError("empty", "下载文件为空")
    with path.open("rb") as stream:
        head = stream.read(32)
        stream.seek(0)
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if head[4:8] == b"ftyp" and head[8:12] not in (b"avif", b"avis"):
            # 核对 MP4 顶层 box 长度及必要结构，拒绝截断/伪装为 MP4 的错误页。
            stream.seek(0)
            boxes, offset = set(), 0
            while offset < size:
                stream.seek(offset)
                raw = stream.read(8)
                if len(raw) != 8:
                    raise DownloadError("corrupt", "MP4 文件结构不完整")
                length, kind = struct.unpack(">I4s", raw)
                header = 8
                if length == 1:
                    extended = stream.read(8)
                    if len(extended) != 8:
                        raise DownloadError("corrupt", "MP4 文件结构不完整")
                    length, header = struct.unpack(">Q", extended)[0], 16
                elif length == 0:
                    length = size - offset
                if length < header or offset + length > size:
                    raise DownloadError("corrupt", "MP4 文件被截断，需重新下载")
                boxes.add(kind)
                offset += length
            if not {b"ftyp", b"moov", b"mdat"} <= boxes:
                raise DownloadError("corrupt", "MP4 缺少必要的媒体数据")
            return "mp4", size, digest
    try:
        with Image.open(path) as img:
            kind = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif", "AVIF": "avif"}.get(img.format)
            if not kind:
                raise ValueError("unsupported")
            img.verify()
            return kind, size, digest
    except Exception:
        raise DownloadError("corrupt", "下载内容不是有效的图片/GIF/MP4 文件，可能是访问错误页") from None


class _StopSignal:
    def __init__(self, global_stop, local_stop=None):
        self.global_stop, self.local_stop = global_stop, local_stop

    def is_set(self):
        return self.global_stop.is_set() or bool(self.local_stop and self.local_stop.is_set())

    def wait(self, timeout):
        end = time.monotonic() + timeout
        while not self.is_set():
            remaining = end - time.monotonic()
            if remaining <= 0:
                return False
            self.global_stop.wait(min(remaining, 0.1))
        return True


class Engine:
    def __init__(self, store: Store, credential_provider=lambda: {}):
        self.store = store
        self.credential_provider = credential_provider
        self.events = queue.Queue()
        self.tweet_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tweet")
        self.media_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="media")
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.pending = {}
        self.cancel_events = {}
        self.deleting = set()
        self.closed = False
        self.transfer_progress = {}

    def set_transfer_progress(self, tweet_id, media_id, **values):
        with self.lock:
            self.transfer_progress.setdefault((tweet_id, media_id), {}).update(values)

    def progress_snapshot(self, keys=None):
        with self.lock:
            if keys is not None:
                self.transfer_progress = {k:v for k,v in self.transfer_progress.items() if k in keys}
            return {k:dict(v) for k,v in self.transfer_progress.items()}

    def enqueue(self, tweet_id: str):
        with self.lock:
            row = self.store.tweet(tweet_id)
            if self.stop.is_set() or tweet_id in self.pending or not row or row['status'] in ('done','deleting'):
                return
            self.cancel_events[tweet_id] = threading.Event()
            future = self.tweet_pool.submit(self.run_tweet, tweet_id)
            self.pending[tweet_id] = future
        future.add_done_callback(lambda _: self._finished(tweet_id))

    def _finished(self, tweet_id):
        with self.lock:
            if tweet_id in self.deleting:
                self.store.delete_records([tweet_id])
                self.deleting.discard(tweet_id)
            self.pending.pop(tweet_id, None)
            self.cancel_events.pop(tweet_id, None)
        self.events.put(("refresh", tweet_id))

    def busy(self):
        with self.lock:
            return bool(self.pending)

    def retry(self, tweet_id, auth_mode):
        with self.lock:
            row = self.store.tweet(tweet_id)
            if tweet_id in self.pending or not row or row['status'] in ('done','deleting'):
                return False
            self.store.update_tweet(tweet_id, status="queued", error="", error_code="", auth_mode=auth_mode)
        self.enqueue(tweet_id)
        return True

    def delete_plans(self, tweet_ids):
        count = 0
        with self.lock:
            for tweet_id in set(tweet_ids):
                row = self.store.tweet(tweet_id)
                if not row or row['status'] in ('done','deleting'):
                    continue
                if tweet_id in self.pending:
                    self.deleting.add(tweet_id)
                    self.cancel_events[tweet_id].set()
                    self.store.update_tweet(tweet_id,status='deleting',error='正在停止任务并移除计划，已有文件保留')
                    count += 1
                else:
                    count += self.store.delete_records([tweet_id])
        return count

    def delete_completed(self, tweet_ids):
        with self.lock:
            return self.store.delete_records(set(tweet_ids)-set(self.pending), completed=True)

    def stop_signal(self, tweet_id=None):
        return _StopSignal(self.stop, self.cancel_events.get(tweet_id))

    def request_stop(self):
        self.stop.set()

    def shutdown(self):
        if not self.closed:
            self.stop.set()
            self.tweet_pool.shutdown(wait=True)
            self.media_pool.shutdown(wait=True)
            self.closed = True

    def cleanup_interrupted(self, tweet_ids):
        """仅在线程结束后清除这些队列项拥有的临时文件，保留成品。"""
        if not self.closed:
            raise RuntimeError("Stop workers before cleaning partial downloads")
        errors = []
        for tweet_id in tweet_ids:
            tweet = self.store.tweet(tweet_id)
            if not tweet or tweet["status"] in ("done", "deleting"):
                continue
            for media in self.store.media(tweet_id):
                if media["status"] == "done":
                    continue
                path = self.store.media_path(media)
                for suffix in (".source.part", ".gif.part", ".source.part.remux.part"):
                    partial = path.with_name(path.stem + suffix)
                    try:
                        partial.unlink(missing_ok=True)
                    except OSError:
                        errors.append(str(partial))
                # 最终文件与其校验值保留：重命名后中断也能在重试时恢复。
                self.store.update_media(tweet_id, media["media_id"], status="queued", downloaded=0, total=0, speed=0, source_sha256="", error="")
            self.store.update_tweet(tweet_id, status="queued", error="", error_code="")
        return errors

    def check_stop(self, tweet_id=None):
        if self.stop_signal(tweet_id).is_set():
            raise DownloadError("interrupted", "任务已暂停，下次启动可恢复")

    def run_tweet(self, tweet_id):
        row = self.store.tweet(tweet_id)
        if not row or row["status"] in ("done", "deleting"):
            return
        try:
            self.check_stop(tweet_id)
            self.store.update_tweet(tweet_id, status="resolving", error="", error_code="")
            with self.lock:
                self.transfer_progress = {k:v for k,v in self.transfer_progress.items() if k[0] != tweet_id}
            mode = self.store.setting("proxy_mode", "system")
            proxy = self.store.setting("proxy_url", "")
            cookies = self.credential_provider() if row["auth_mode"] == "account" else {}
            if row["auth_mode"] == "account" and not cookies.get("auth_token"):
                raise DownloadError("login_required", "此任务选择了登录模式，请先登录 X 或改用匿名模式重试")
            target = normalize_url(row["url"])

            def fetch():
                if cookies:
                    from .account import resolve_authenticated
                    return resolve_authenticated(target, mode, proxy, cookies, self.stop_signal(tweet_id))
                return resolve(target, mode, proxy)

            tweet = fetch()
            self.check_stop(tweet_id)
            self.store.save_tweet(tweet)
            if any(m.kind == "animated_gif" and m.extension == "mp4" and not m.error for m in tweet.media):
                if self.store.wait_for_format(tweet_id):
                    self.events.put(("gif_choice", row["batch_id"]))
                    return
            gif_mode = self.store.batch_mode(row["batch_id"]) or "mp4"
            self.store.update_tweet(tweet_id, status="downloading", error="")
            current = {m.media_id: m for m in tweet.media}
            futures = []
            for media in self.store.media(tweet_id):
                if media["media_id"] not in current and media["status"] != "done":
                    self.store.update_media(tweet_id, media["media_id"], status="failed", error="该媒体已不在推文返回的列表中")
                    continue
                item = current.get(media["media_id"])
                if item and item.error:
                    self.store.update_media(tweet_id, media["media_id"], status="failed", error=item.error)
                    continue
                file_mode = (media.get('gif_mode') or 'mp4') if gif_mode == 'individual' else gif_mode
                futures.append(self.media_pool.submit(self.run_media, tweet_id, media, mode, proxy, file_mode))
            for future in as_completed(futures):
                future.result()
            self.check_stop(tweet_id)
            rows = self.store.media(tweet_id)
            failed = [r for r in rows if r["status"] != "done"]
            if failed:
                self.store.update_tweet(tweet_id, status="failed", error_code="partial", error=f"{len(failed)}/{len(rows)} 个文件未完成：" + failed[0]["error"])
            else:
                self.store.update_tweet(tweet_id, status="done", error="", error_code="")
        except DownloadError as exc:
            self.store.update_tweet(tweet_id, status="queued" if exc.code == "interrupted" else "failed", error=str(exc), error_code=exc.code)
        except Exception as exc:
            self.store.update_tweet(tweet_id, status="failed", error=f"处理失败（{type(exc).__name__}），请重试", error_code="internal")
        self.events.put(("refresh", tweet_id))

    def clear_history(self, expired_only=False):
        with self.lock:
            return self.store.clear_history(expired_only=expired_only, exclude=set(self.pending))

    def run_media(self, tweet_id, media, mode, proxy, gif_mode="mp4"):
        mid = media["media_id"]
        try:
            self.check_stop(tweet_id)
            # Future 可能已排队很久，必须在开始时原子地获取最新目录，不能沿用旧快照。
            media = self.store.begin_media(tweet_id, mid)
            if media is None:
                return
            path = self.store.media_path(media)
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                _, size, digest = verify_file(path)
                if media["sha256"] and digest == media["sha256"]:
                    self.store.update_media(tweet_id, mid, status="done", downloaded=size, total=size, speed=0, error="")
                    return
                raise DownloadError("conflict", "目标文件已存在且校验不符；为防止覆盖，请先移走该文件再重试")
            self.check_stop(tweet_id)
            self._download(tweet_id, media, mode, proxy, gif_mode)
        except DownloadError as exc:
            self.store.update_media(tweet_id, mid, status="queued" if exc.code == "interrupted" else "failed", error=str(exc), speed=0)
        except OSError as exc:
            message = "磁盘空间不足" if exc.errno == 28 or getattr(exc, "winerror", None) == 112 else "无法写入下载目录，请检查目录权限、磁盘空间或文件占用"
            self.store.update_media(tweet_id, mid, status="failed", error=message, speed=0)
        except Exception as exc:
            self.store.update_media(tweet_id, mid, status="failed", error=f"文件处理失败（{type(exc).__name__}）", speed=0)

    def _download(self, tweet_id, media, mode, proxy, gif_mode="mp4"):
        mid = media["media_id"]
        validate_media_url(media["url"])
        self.store.update_media(tweet_id, mid, status="downloading", error="", downloaded=0, speed=0)
        path = self.store.media_path(media)
        part = path.with_name(path.stem + ".source.part")
        converted = path.with_name(path.stem + ".gif.part")
        try:
            # 转换失败/中断时复用已校验的源文件，不重复下载。
            stored = next(m for m in self.store.media(tweet_id) if m["media_id"] == mid)
            reuse = False
            if part.exists() and stored["source_sha256"]:
                try:
                    reuse = verify_file(part)[2] == stored["source_sha256"]
                except DownloadError:
                    pass
            if not reuse:
                self._fetch_part(tweet_id, mid, media["url"], part, mode, proxy)
            self.set_transfer_progress(tweet_id, mid, _phase='verifying')
            extension, size, digest = verify_file(part)
            self.store.update_media(tweet_id, mid, source_sha256=digest)
            output = part
            if media["kind"] == "animated_gif" and extension == "mp4" and gif_mode == "gif":
                self.store.update_media(tweet_id, mid, status="converting", speed=0, error="正在转换 GIF")
                convert_mp4_to_gif(part, converted, self.stop_signal(tweet_id))
                extension, size, digest = verify_file(converted)
                output = converted
            if extension != media["ext"]:
                path = path.with_suffix("." + extension)
            self.check_stop(tweet_id)
            if path.exists():
                raise DownloadError("conflict", "目标文件已存在，为避免覆盖已停止保存")
            # 先记录校验值，崩溃发生在重命名后也可确认文件归属并恢复。
            self.store.update_media(tweet_id, mid, sha256=digest, total=size, downloaded=size, filename=path.name, ext=extension)
            # Windows rename 不覆盖已有目标。
            __LINUX_RENAME__(output, path)
            self.store.update_media(tweet_id, mid, status="done", error="", speed=0)
            if output != part:
                try:
                    part.unlink(missing_ok=True)
                except OSError:
                    pass  # 成品已保存；残留临时源文件不应让成功任务变为失败。
        except requests.RequestException as exc:
            raise network_error(exc) from None
        finally:
            try:
                converted.unlink(missing_ok=True)
            except OSError:
                pass

    def _fetch_part(self, tweet_id, mid, url, part, mode, proxy):
        from urllib.parse import urlsplit
        if urlsplit(url).path.endswith('.m3u8'):
            from .hls import download_hls
            download_hls(url, part, mode, proxy, self.stop_signal(tweet_id),
                         lambda count, speed: self.store.update_media(tweet_id, mid, downloaded=count, speed=speed),
                         segment_progress=lambda done, total, phase: self.set_transfer_progress(
                             tweet_id, mid, _segments_done=done, _segments_total=total, _phase=phase))
            return
        with make_session(mode, proxy) as session:
            # 媒体 CDN 无需账号 Cookie；禁止将 X 登录凭据发给媒体或其他站点。
            with session.get(url, stream=True, timeout=(10, 30), allow_redirects=False) as response:
                if response.status_code != 200:
                    raise http_error(response.status_code)
                content_type = response.headers.get("Content-Type", "").lower()
                if "text/html" in content_type or "application/json" in content_type:
                    raise DownloadError("content_type", "媒体服务器返回了错误页面，而非媒体文件")
                total = int(response.headers.get("Content-Length") or 0)
                self.store.update_media(tweet_id, mid, total=total)
                downloaded, last = 0, time.monotonic()
                started = last
                with part.open("wb") as stream:
                    for chunk in response.iter_content(128 * 1024):
                        self.check_stop(tweet_id)
                        stream.write(chunk)
                        downloaded += len(chunk)
                        now = time.monotonic()
                        if now - last >= 0.3:
                            self.store.update_media(tweet_id, mid, downloaded=downloaded, speed=downloaded / max(now - started, 0.001))
                            last = now
                    stream.flush()
                    os.fsync(stream.fileno())
                if total and downloaded != total:
                    raise DownloadError("truncated", "文件长度与服务器声明不一致，请重试", True)
