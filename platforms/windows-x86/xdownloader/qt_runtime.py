"""Windows 兼容版固定使用随程序提供的 Qt 插件。"""
import os
from pathlib import Path

_dll_directory = None


def prepare_qt():
    global _dll_directory
    import PySide2

    qt = Path(PySide2.__file__).resolve().parent
    os.environ['QT_PLUGIN_PATH'] = str(qt / 'plugins')
    os.environ['QT_QPA_PLATFORM_PLUGIN_PATH'] = str(qt / 'plugins' / 'platforms')
    os.environ['QT_QPA_PLATFORM'] = 'windows'
    if os.name == 'nt' and _dll_directory is None:
        _dll_directory = os.add_dll_directory(str(qt))
