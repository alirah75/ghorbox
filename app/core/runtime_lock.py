"""OS-released exclusive ownership of a data directory, including after process crashes."""

import os
from pathlib import Path

from app.core.security import private_directory


class RuntimeBusy(Exception):
    """Safe exception without process IDs or paths."""


class RuntimeLock:
    def __init__(self, directory: Path):
        self.directory = directory
        self.handle = None

    def __enter__(self):
        private_directory(self.directory)
        path = self.directory / ".runtime.lock"
        if path.is_symlink():
            raise RuntimeBusy("مسیر قفل نامعتبر است.")
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        self.handle = os.fdopen(descriptor, "r+b", buffering=0)
        try:
            if path.stat().st_size == 0:
                self.handle.write(b"0")
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            self.handle = None
            raise RuntimeBusy(
                "این پوشه در حال استفاده است؛ ابتدا اجرای دیگر ربات را متوقف کنید."
            ) from None
        return self

    def __exit__(self, *_):
        if self.handle is not None:
            try:
                if os.name == "nt":
                    import msvcrt

                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            finally:
                self.handle.close()
                self.handle = None
        # Never unlink this file: replacing a live lock's inode would bypass ownership.
