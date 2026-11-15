"""Wake the resting service for an arrival, with the ship tick as the fallback.

Linux directory notifications are scheduling hints only. Missing watches, an
unsupported kernel and a replaced directory all retain the bounded tick retry.
"""

import ctypes
import os
import select
import time

from . import post_dir
from .ship import ShipError


class Arrivals:
    def __init__(self, runtime):
        self.runtime = runtime
        self.fd = None
        self.watched = set()
        try:
            self.library = ctypes.CDLL(None, use_errno=True)
            initialize = self.library.inotify_init1
            initialize.argtypes = (ctypes.c_int,)
            initialize.restype = ctypes.c_int
            self.library.inotify_add_watch.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32)
            self.library.inotify_add_watch.restype = ctypes.c_int
            descriptor = initialize(os.O_NONBLOCK | os.O_CLOEXEC)
            if descriptor >= 0:
                self.fd = descriptor
        except (AttributeError, OSError):
            pass

    def refresh(self, names):
        if self.fd is None:
            return
        paths = [self.runtime / 'requests', self.runtime / 'post/store', self.runtime / 'post/drop']
        paths += [self.runtime / 'post/drop' / name for name in names]
        for path in paths:
            try:
                # Use the existing directory walk for anything under post.
                directory = post_dir.open_directory(path, create=False)
                try:
                    info = os.fstat(directory)
                    identity = (info.st_dev, info.st_ino)
                    if identity not in self.watched:
                        # Child creates, closes and renames; replacement/deletion also wakes.
                        watch = self.library.inotify_add_watch(
                            self.fd, os.fsencode(f'/proc/self/fd/{directory}'), 0x00000FCC)
                        if watch >= 0:
                            self.watched.add(identity)
                finally:
                    os.close(directory)
            except (OSError, ValueError, ShipError):
                continue  # the ordinary duty owns any error; the tick still runs

    def wait(self, seconds):
        if self.fd is None or not self.watched:
            time.sleep(seconds)
            return False
        try:
            ready, _, _ = select.select([self.fd], [], [], seconds)
            if not ready:
                return False
            while True:
                try:
                    if not os.read(self.fd, 65536):
                        break
                except BlockingIOError:
                    break
            return True
        except OSError:
            self.close()
            time.sleep(seconds)
            return False

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        self.watched.clear()
