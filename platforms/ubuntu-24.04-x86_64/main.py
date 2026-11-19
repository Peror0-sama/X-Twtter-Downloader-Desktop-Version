from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--ozone-platform=x11")
import argparse
import json
import sys
import time
from pathlib import Path


def base_directory():
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "XDownloader"


def cli(args):
    from PySide6.QtCore import QLockFile
    from xdownloader.storage import Store
    from xdownloader.engine import Engine
    from xdownloader.core import parse_input, read_txt
    base = Path(args.data_dir).resolve() if args.data_dir else base_directory()
    (base / "data").mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(base / "data" / "application.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        if sys.stderr:
            print("测试目录正在使用或不可写，请指定独立的 --data-dir", file=sys.stderr)
        return 2
    store = Store(base)
    if args.proxy_mode:
        store.set_setting("proxy_mode", args.proxy_mode)
    if args.proxy:
        store.set_setting("proxy_url", args.proxy)
    urls, errors = parse_input("\n".join(args.urls))
    for file in args.txt:
        found, invalid = parse_input(read_txt(Path(file)), Path(file).name)
        urls.extend(found)
        errors.extend(invalid)
    added, duplicates = store.add_urls(urls, gif_mode=args.gif_format)
    engine = Engine(store)
    targets = {u.tweet_id for u in urls}
    for tweet_id in targets:
        row = store.tweet(tweet_id)
        if row["status"] not in ("done", "failed", "waiting_choice"):
            engine.enqueue(tweet_id)
    while engine.busy():
        time.sleep(0.2)
    engine.shutdown()
    report = {"added": len(added), "duplicates": duplicates, "invalid": errors,
              "tweets": [t for t in store.snapshot() if t["id"] in targets]}
    # 不在报告中输出临时媒体地址或任何登录信息。
    for tweet in report["tweets"]:
        for media in tweet["media"]:
            media.pop("url", None)
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        Path(args.report).write_text(serialized, encoding="utf-8")
    if sys.stdout:
        print(serialized)
    lock.unlock()
    return 1 if errors or any(t["status"] != "done" for t in report["tweets"]) else 0


def main():
    # Windowed PyInstaller builds have no console streams. Python 3.10
    # argparse still writes to them when --help or invalid arguments are used.
    import os
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    parser = argparse.ArgumentParser(description="XDownloader")
    parser.add_argument("--cli", action="store_true")
    parser.add_argument("--data-dir")
    parser.add_argument("--report")
    parser.add_argument("--proxy-mode", choices=["system", "direct", "manual"])
    parser.add_argument("--proxy")
    parser.add_argument("--txt", action="append", default=[])
    parser.add_argument("--gif-format", choices=["mp4", "gif"], default="mp4", help="CLI 批次动图格式，默认 mp4；GUI 遇到动图时询问")
    parser.add_argument("urls", nargs="*")
    args = parser.parse_args()
    if args.cli:
        return cli(args)
    from PySide6.QtCore import QLockFile, Qt
    from PySide6.QtWidgets import QApplication, QMessageBox
    from xdownloader.storage import Store
    from xdownloader.ui import MainWindow
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Peror0_sama.XDownloader")
    app = QApplication(sys.argv[:1])
    from PySide6.QtGui import QFontDatabase, QFont
    font = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "assets" / "NotoSansCJK-Regular.ttc"
    if font.is_file():
        QFontDatabase.addApplicationFont(str(font))
        app.setFont(QFont("Noto Sans CJK SC", 10))
    app.setApplicationName("XDownloader")
    app.setOrganizationName("XDownloader")
    from xdownloader.instance import SingleInstance
    instance = SingleInstance()
    if not instance.acquire():
        QMessageBox.information(None, "XDownloader 已在运行", "请使用已打开的下载器或系统托盘中的图标。")
        return 0
    base = base_directory()
    try:
        (base / "data").mkdir(parents=True, exist_ok=True)
        lock = QLockFile(str(base / "data" / "application.lock"))
        lock.setStaleLockTime(0)
        if not lock.tryLock(100):
            if lock.error() == QLockFile.LockError.LockFailedError:
                QMessageBox.information(None, "XDownloader 已在运行", "请使用已经打开的下载器窗口。")
                instance.close()
                return 0
            raise OSError("Cannot create application lock")
        store = Store(base)
        window = MainWindow(store)
    except Exception as exc:
        instance.close()
        QMessageBox.critical(None, "无法启动", f"无法初始化程序目录（{type(exc).__name__}）。\n请检查用户数据目录权限和磁盘空间。\n{base}")
        return 1
    window.show()
    try:
        result = app.exec()
    finally:
        lock.unlock()
        instance.close()
    return result


if __name__ == "__main__":
    sys.exit(main())
