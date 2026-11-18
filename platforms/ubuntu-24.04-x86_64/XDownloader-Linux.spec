from pathlib import Path

a = Analysis(
    ['main.py'], pathex=[],
    binaries=[('third_party/ffmpeg/ffmpeg', 'ffmpeg')],
    datas=[('assets/peror0_sama.ico', 'assets'),
           ('assets/peror0_sama.png', 'assets'),
           ('assets/NotoSansCJK-Regular.ttc', 'assets'),
           ('third_party/electron', 'third_party/electron'),
           ('browser_host', 'browser_host')],
    hiddenimports=['socks', 'secretstorage', 'cryptography.fernet', 'PySide6.QtDBus'],
    excludes=['gallery_dl','PySide6.QtQml','PySide6.QtQuick','PySide6.QtWebEngineQuick',
              'PySide6.QtWebEngineCore','PySide6.QtWebEngineWidgets','tkinter'],
    hookspath=[], hooksconfig={}, runtime_hooks=[], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='XDownloader', debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='XDownloader')
