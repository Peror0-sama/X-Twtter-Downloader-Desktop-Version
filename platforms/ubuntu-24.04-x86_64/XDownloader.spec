# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import PySide6
import os
import sys

qt_dir = Path(PySide6.__file__).parent
# 隔离构建机 PATH：否则 PyInstaller 会误收集 Poppler 的同名 ICU DLL，
# 它与 Windows/Qt 使用的 ICU 导出不兼容，导致干净环境启动失败。
os.environ['PATH'] = os.pathsep.join([
    str(qt_dir), str(Path(sys.executable).parent), str(Path(sys.base_prefix)),
    str(Path(os.environ['SystemRoot']) / 'System32'), os.environ['SystemRoot']])
# Qt 6.12 需要新版 VC runtime；放在进程 DLL 搜索根目录，避免加载旧的系统副本。
vc_runtime = [(str(qt_dir / name), '.') for name in (
    'msvcp140.dll', 'msvcp140_1.dll', 'msvcp140_2.dll',
    'msvcp140_codecvt_ids.dll', 'vcruntime140.dll', 'vcruntime140_1.dll', 'concrt140.dll')]

a = Analysis(
    ['main.py'], pathex=[], binaries=vc_runtime + [('third_party/ffmpeg/ffmpeg.exe', 'ffmpeg')],
    datas=[('assets/peror0_sama.ico', 'assets'),
           ('third_party/webview_host', 'third_party/webview_host'),
           ('third_party/webview2-runtime', 'third_party/webview2-runtime')],
    hiddenimports=['socks'],
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['gallery_dl', 'PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.QtWebEngineQuick',
              'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets', 'tkinter'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name='XDownloader', icon='assets/peror0_sama.ico', debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=False,
    disable_windowed_traceback=False,
)
