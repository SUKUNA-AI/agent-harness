"""Bounded local reads which refuse links, special files and changing inputs.

These cooperative identity checks are not a filesystem sandbox or a lock against
an external writer. Callers needing containment must also validate parent paths.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat

DEFAULT_MAX_BYTES = 16 * 1024 * 1024


class FileReadError(ValueError):
    """An input is unsupported, too large, or changed while being read."""


def _regular(info, path):
    if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
        raise FileReadError(f"Unsupported symlink/reparse file input: {path}")
    if not stat.S_ISREG(info.st_mode):
        raise FileReadError(f"Input is not a regular file: {path}")


def _stamp(info, *, cross_api=False):
    value = (info.st_dev, info.st_ino, info.st_mode, info.st_size,
             info.st_mtime_ns, info.st_nlink, info.st_uid, info.st_gid)
    # Windows stat() can expose birth time as ctime, while fstat() exposes
    # change time. Compare ctime only between observations using the same API.
    return value if cross_api else (*value, info.st_ctime_ns)


def read_regular_file(path, max_bytes=DEFAULT_MAX_BYTES, missing_ok=False) -> bytes | None:
    """Read at most max_bytes; only a missing path may produce None.

    O_NONBLOCK prevents a raced FIFO from blocking before fstat rejects it.
    Both the opened descriptor and the final directory entry must retain the
    original identity and remain ordinary files throughout the bounded read.
    """
    if type(max_bytes) is not int or max_bytes < 0:
        raise ValueError("max_bytes must be a nonnegative integer")
    path = Path(path)
    try:
        before = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise
    _regular(before, path)
    if before.st_size > max_bytes:
        raise FileReadError(f"File input exceeds the {max_bytes} byte limit: {path}")
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0)
             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        _regular(opened, path)
        if _stamp(before, cross_api=True) != _stamp(opened, cross_api=True):
            raise FileReadError(f"File input changed before reading: {path}")
        chunks, total = [], 0
        while total <= max_bytes:
            chunk = os.read(fd, min(65536, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        after = os.fstat(fd)
        last = path.lstat()
        _regular(last, path)
        if (total > max_bytes or _stamp(opened) != _stamp(after)
                or _stamp(before) != _stamp(last)
                or _stamp(after, cross_api=True) != _stamp(last, cross_api=True)):
            raise FileReadError(f"File input changed while reading or exceeds its byte limit: {path}")
        return b"".join(chunks)
    finally:
        os.close(fd)
