"""使用随应用内嵌的 FFmpeg 转换，不查找或依赖电脑上的 FFmpeg。"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

from PIL import Image
from .core import DownloadError

_slot = threading.Semaphore(1)


def ffmpeg_path():
    base = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1] / "third_party"
    path = base / "ffmpeg" / "ffmpeg.exe"
    if not path.is_file():
        raise DownloadError("converter_missing", "内嵌 GIF 转换组件缺失，请重新解压完整的 1.2 程序")
    return path


def _run(arguments, stop):
    process = subprocess.Popen([str(ffmpeg_path()), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *arguments],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    deadline = time.monotonic() + 600
    try:
        while process.poll() is None:
            if stop.wait(0.15):
                raise DownloadError("interrupted", "GIF 转换已暂停，下次启动可恢复")
            if time.monotonic() > deadline:
                raise DownloadError("conversion_timeout", "GIF 转换超过 10 分钟，源文件已保留，可重试")
        if process.returncode:
            raise DownloadError("conversion_failed", "GIF 转换失败，请检查磁盘空间或重试；已保留下载源文件")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def convert_mp4_to_gif(source: Path, target: Path, stop):
    while not _slot.acquire(timeout=0.15):
        if stop.is_set():
            raise DownloadError("interrupted", "GIF 转换已暂停，下次启动可恢复")
    palette = target.with_name(target.name + ".palette.png")
    try:
        if stop.is_set():
            raise DownloadError("interrupted", "GIF 转换已暂停，下次启动可恢复")
        # 两遍处理避免把全部视频帧同时缓存；保持原尺寸与时间戳，GIF 无限循环。
        source_args = ["-protocol_whitelist", "file,pipe", "-threads", "2", "-i", str(source)]
        _run([*source_args, "-filter_threads", "1", "-vf", "palettegen", "-frames:v", "1", str(palette)], stop)
        _run([*source_args, "-i", str(palette), "-filter_complex_threads", "1", "-lavfi", "paletteuse",
              "-an", "-fps_mode", "passthrough", "-loop", "0", "-f", "gif", str(target)], stop)
        with Image.open(target) as gif:
            if gif.format != "GIF":
                raise DownloadError("conversion_invalid", "转换输出不是有效 GIF")
            for frame in range(gif.n_frames):
                if stop.is_set():
                    raise DownloadError("interrupted", "GIF 转换已暂停，下次启动可恢复")
                gif.seek(frame)
                gif.load()
    finally:
        try:
            palette.unlink(missing_ok=True)
        except OSError:
            pass
        finally:
            _slot.release()
