import ctypes
import errno
import os
import select
import struct
from collections import namedtuple


Event = namedtuple('Event', 'path mask cookie is_directory')

IN_MODIFY = 0x00000002
IN_ATTRIB = 0x00000004
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_Q_OVERFLOW = 0x00004000
IN_IGNORED = 0x00008000
IN_ISDIR = 0x40000000

WATCH_MASK = (
    IN_MODIFY | IN_ATTRIB | IN_CLOSE_WRITE | IN_MOVED_FROM | IN_MOVED_TO
    | IN_CREATE | IN_DELETE | IN_DELETE_SELF | IN_MOVE_SELF)
EVENT_HEADER = struct.Struct('iIII')


class InotifyWatcher:

    def __init__(self, path):
        self.path = os.path.realpath(path)
        self._libc = ctypes.CDLL(None, use_errno=True)
        self._libc.inotify_init1.argtypes = [ctypes.c_int]
        self._libc.inotify_init1.restype = ctypes.c_int
        self._libc.inotify_add_watch.argtypes = [
            ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self._libc.inotify_add_watch.restype = ctypes.c_int
        self._libc.inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
        self._libc.inotify_rm_watch.restype = ctypes.c_int
        self.fd = self._libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            self._raise_os_error()
        self._paths = {}
        self._watches = {}
        self._add_tree(self.path)

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
        self._paths.clear()
        self._watches.clear()

    def read(self, timeout=None):
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return []
        events = []
        while True:
            try:
                payload = os.read(self.fd, 1024 * 1024)
            except BlockingIOError:
                break
            if not payload:
                break
            offset = 0
            while offset + EVENT_HEADER.size <= len(payload):
                watch, mask, cookie, length = EVENT_HEADER.unpack_from(
                    payload, offset)
                offset += EVENT_HEADER.size
                raw_name = payload[offset:offset + length]
                offset += length
                name = raw_name.rstrip(b'\0').decode(
                    'utf-8', errors='surrogateescape')
                if mask & IN_Q_OVERFLOW:
                    events.append(Event(self.path, mask, cookie, False))
                    continue
                directory = self._paths.get(watch)
                if not directory:
                    continue
                path = os.path.join(directory, name) if name else directory
                is_directory = bool(mask & IN_ISDIR)
                events.append(Event(path, mask, cookie, is_directory))
                if (is_directory and mask & (IN_CREATE | IN_MOVED_TO)
                        and os.path.isdir(path)):
                    self._add_tree(path)
                if mask & (IN_DELETE_SELF | IN_MOVE_SELF | IN_IGNORED):
                    self._forget_watch(watch)
        return events

    def _add_tree(self, path):
        if os.path.islink(path) or not os.path.isdir(path):
            return
        self._add_watch(path)
        for directory, names, _ in os.walk(path, followlinks=False):
            names[:] = [name for name in names
                if not os.path.islink(os.path.join(directory, name))]
            self._add_watch(directory)

    def _add_watch(self, path):
        path = os.path.realpath(path)
        if path in self._watches:
            return
        watch = self._libc.inotify_add_watch(
            self.fd, os.fsencode(path), WATCH_MASK)
        if watch < 0:
            error = ctypes.get_errno()
            if error in {errno.ENOENT, errno.ENOTDIR}:
                return
            raise OSError(error, os.strerror(error), path)
        old_path = self._paths.get(watch)
        if old_path:
            self._watches.pop(old_path, None)
        self._paths[watch] = path
        self._watches[path] = watch

    def _forget_watch(self, watch):
        path = self._paths.pop(watch, None)
        if path:
            self._watches.pop(path, None)

    @staticmethod
    def _raise_os_error():
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
