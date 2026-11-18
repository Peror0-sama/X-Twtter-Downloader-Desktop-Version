"""有界并行下载官方回放分段，再由内嵌 FFmpeg 无转码合并为 MP4。"""
from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
import os
import re
import subprocess
import threading
import time
from urllib.parse import urljoin

from .conversion import ffmpeg_path
from .core import DownloadError, http_error, make_session, validate_media_url


def get_bytes(url, mode, proxy, stop, limit, session=None):
    if stop.is_set():
        raise DownloadError('interrupted', '下载已暂停，下次启动可恢复')
    with (make_session(mode, proxy) if session is None else nullcontext(session)) as session:
        current = validate_media_url(url)
        for _ in range(4):
            with session.get(current, stream=True, timeout=(10, 30), allow_redirects=False) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    current = validate_media_url(urljoin(current, response.headers.get('Location', '')))
                    continue
                if response.status_code != 200:
                    raise http_error(response.status_code)
                chunks, size = [], 0
                for chunk in response.iter_content(128 * 1024):
                    if stop.is_set():
                        raise DownloadError('interrupted', '下载已暂停，下次启动可恢复')
                    size += len(chunk)
                    if size > limit:
                        raise DownloadError('hls_size', '回放分段超过支持的大小')
                    chunks.append(chunk)
                expected = int(response.headers.get('Content-Length') or 0)
                if expected and not response.headers.get('Content-Encoding') and size != expected:
                    raise DownloadError('truncated', '回放分段不完整', True)
                return b''.join(chunks), current
        raise DownloadError('hls_redirect', '回放地址重定向次数过多')


def media_playlist(url, mode, proxy, stop):
    for _ in range(4):
        data, url = get_bytes(url, mode, proxy, stop, 8 * 1024 * 1024)
        text = data.decode('utf-8-sig')
        if not text.startswith('#EXTM3U'):
            raise DownloadError('hls_invalid', '回放服务器未返回有效的播放列表')
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        variants = []
        for i, line in enumerate(lines):
            if line.startswith('#EXT-X-STREAM-INF:') and i + 1 < len(lines):
                bandwidth = re.search(r'(?:^|,)BANDWIDTH=(\d+)', line.split(':', 1)[1])
                variants.append((int(bandwidth.group(1)) if bandwidth else 0, urljoin(url, lines[i + 1])))
        if variants:
            url = max(variants, key=lambda v: v[0])[1]
            continue
        if '#EXT-X-ENDLIST' not in lines:
            raise DownloadError('broadcast_live', '该广播仍在直播或回放尚未生成完整，请结束后重试')
        segments, duration = [], 0.0
        for line in lines:
            if line.startswith('#EXT-X-KEY:') and 'METHOD=NONE' not in line:
                raise DownloadError('hls_encrypted', '此回放使用了暂不支持的加密格式')
            if line.startswith('#EXT-X-BYTERANGE'):
                raise DownloadError('hls_range', '此回放使用了暂不支持的分段范围格式')
            if line.startswith('#EXT-X-MAP:'):
                match = re.search(r'URI="([^"]+)"', line)
                if not match or 'BYTERANGE=' in line:
                    raise DownloadError('hls_map', '无法解析回放初始化分段')
                segments.append(validate_media_url(urljoin(url, match.group(1))))
            elif line.startswith('#EXTINF:'):
                duration += float(line.split(':', 1)[1].split(',', 1)[0])
            elif not line.startswith('#'):
                segments.append(validate_media_url(urljoin(url, line)))
        if not segments or duration <= 0:
            raise DownloadError('no_media', '该回放没有可下载的视频分段')
        return segments, duration
    raise DownloadError('hls_depth', '回放播放列表嵌套过多')


def download_hls(url, part: Path, mode, proxy, stop, progress, segment_progress=None):
    segments, duration = media_playlist(url, mode, proxy, stop)
    completed = 0
    if segment_progress:
        segment_progress(0, len(segments), 'downloading')
    output = part.with_name(part.name + '.remux.part')
    started = time.monotonic()
    downloaded = 0
    pending = deque()
    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='hls')
    local = threading.local()
    sessions = []
    def fetch_segment(item):
        # 每个工作线程复用自己的连接，避免数千段回放反复建立代理/TLS 连接。
        # 失败立即返回，不重新请求该分段。
        if not hasattr(local, 'session'):
            local.session = make_session(mode, proxy)
            sessions.append(local.session)
        return get_bytes(item, mode, proxy, stop, 32 * 1024 * 1024, session=local.session)
    try:
        iterator = iter(segments)
        def submit():
            item = next(iterator, None)
            if item:
                pending.append(pool.submit(fetch_segment, item))
        for _ in range(4):
            submit()
        with part.open('wb') as stream:
            while pending:
                if stop.is_set():
                    raise DownloadError('interrupted', '下载已暂停，下次启动可恢复')
                data, _ = pending.popleft().result()
                stream.write(data)
                downloaded += len(data)
                completed += 1
                if segment_progress:
                    segment_progress(completed, len(segments), 'downloading')
                progress(downloaded, downloaded / max(time.monotonic() - started, 0.01))
                submit()
            stream.flush()
            os.fsync(stream.fileno())
        if segment_progress:
            segment_progress(completed, len(segments), 'merging')
        process = subprocess.Popen([str(ffmpeg_path()), '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
            '-protocol_whitelist', 'file,pipe', '-i', str(part), '-map', '0:v:0', '-map', '0:a?',
            '-c', 'copy', '-movflags', '+faststart', '-f', 'mp4', str(output)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            while process.poll() is None:
                if stop.wait(0.15):
                    raise DownloadError('interrupted', '回放合并已暂停，下次启动可恢复')
            if process.returncode:
                raise DownloadError('hls_mux', '回放合并失败，请检查磁盘空间后重试')
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()
        output.replace(part)
        return duration
    finally:
        for future in pending:
            future.cancel()
        pool.shutdown(wait=True, cancel_futures=True)
        for session in sessions:
            session.close()
        output.unlink(missing_ok=True)
