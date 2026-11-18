"""Ubuntu 桌面的账号加密、单实例和文件操作。"""
from pathlib import Path
import ctypes
import hashlib
import fcntl
import os

def crypt_account(data, protect):
    import secretstorage
    from cryptography.fernet import Fernet
    from .core import DownloadError
    try:
        bus = secretstorage.dbus_init()
        collection = secretstorage.get_default_collection(bus)
        if collection.is_locked() and collection.unlock():
            raise DownloadError('account_storage', '请解锁系统登录密钥环后重试')
        attributes = {'application': 'Peror0_sama.XDownloader', 'purpose': 'account-encryption-v1'}
        items = list(collection.search_items(attributes))
        if items:
            if items[0].is_locked() and items[0].unlock():
                raise DownloadError('account_storage', '请解锁系统登录密钥环后重试')
            key = items[0].get_secret()
        elif protect:
            key = Fernet.generate_key()
            collection.create_item('XDownloader 登录加密密钥', attributes, key, replace=True)
        else:
            raise DownloadError('account_storage', '系统密钥环中没有登录密钥，请重新登录')
        cipher = Fernet(key)
        return cipher.encrypt(data) if protect else cipher.decrypt(data)
    except DownloadError:
        raise
    except Exception as exc:
        raise DownloadError('account_storage', '无法访问系统登录密钥环，请解锁密钥环后重试') from exc

class SingleInstance:
    def __init__(self, name='Peror0_sama.XDownloader'):
        self.fd = None
        self.directory = Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}'))
        self.path = self.directory / ('XDownloader-' + hashlib.sha256(name.encode()).hexdigest()[:16] + '.lock')

    def acquire(self):
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(self.path, flags, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        except Exception:
            os.close(fd)
            raise
        self.fd = fd
        return True

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

def rename_without_overwrite(source, destination):
    # Linux rename() 会覆盖已有文件，使用原子 RENAME_NOREPLACE 保留 Windows 行为。
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(destination))
    return Path(destination)

def reveal_file(path):
    from PySide6.QtCore import QUrl
    from PySide6.QtDBus import QDBusConnection, QDBusMessage
    from PySide6.QtGui import QDesktopServices
    uri = QUrl.fromLocalFile(str(path)).toString()
    request = QDBusMessage.createMethodCall('org.freedesktop.FileManager1', '/org/freedesktop/FileManager1',
                                          'org.freedesktop.FileManager1', 'ShowItems')
    request.setArguments([[uri], ''])
    reply = QDBusConnection.sessionBus().call(request, timeout=2000)
    if reply.type() == QDBusMessage.MessageType.ErrorMessage:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))
