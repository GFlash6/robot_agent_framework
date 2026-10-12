"""Advisory whole-file locks: fcntl on POSIX, msvcrt byte-range locks on Windows."""

import os
import time

if os.name == "nt":
    import msvcrt

    def lock(handle, blocking=True):
        # msvcrt locks a byte range; lock the first byte of a file opened "a+".
        handle.seek(0)
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                if not blocking:
                    raise BlockingIOError("file lock held by another owner") from exc
                time.sleep(0.01)

    def unlock(handle):
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def lock(handle, blocking=True):
        fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))

    def unlock(handle):
        fcntl.flock(handle, fcntl.LOCK_UN)
