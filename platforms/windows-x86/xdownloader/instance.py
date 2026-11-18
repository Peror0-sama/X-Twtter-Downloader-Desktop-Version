"""按 Windows 会话限制一个下载器实例，不受 EXE 所在目录影响。"""
import ctypes
from ctypes import wintypes


class SingleInstance:
    def __init__(self, name=r'Local\Peror0_sama.XDownloader'):
        self.name, self.handle = name, None
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.api.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        self.api.CreateMutexW.restype = wintypes.HANDLE
        self.api.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.api.CloseHandle.restype = wintypes.BOOL

    def acquire(self):
        ctypes.set_last_error(0)
        handle = self.api.CreateMutexW(None, False, self.name)
        error = ctypes.get_last_error()
        if not handle:
            raise OSError(error, 'Cannot create application mutex')
        if error == 183:
            self.api.CloseHandle(handle)
            return False
        self.handle = handle
        return True

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None
