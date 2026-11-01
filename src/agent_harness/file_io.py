"""Bounded local reads which refuse links, special files and changing inputs.

These cooperative identity checks are not a filesystem sandbox or a lock against
an external writer. Callers needing containment must also validate parent paths.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat

DEFAULT_MAX_BYTES = 16 * 1024 * 1024
# POSIX filesystems may coalesce rapid same-size writes into identical timestamp
# observations too. Metadata alone is never proof of an unchanged bounded read.
_VERIFY_CONTENT = True


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


def _read_bounded(fd, limit):
    chunks, total = [], 0
    while total <= limit:
        chunk = os.read(fd, min(65536, limit + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def read_regular_file(path, max_bytes=DEFAULT_MAX_BYTES, missing_ok=False) -> bytes | None:
    """Read a file of at most max_bytes; only a missing path may produce None.

    O_NONBLOCK prevents a raced FIFO from blocking before fstat rejects it.
    Both the opened descriptor and the final directory entry must retain the
    original identity and remain ordinary files throughout the bounded read.
    Timestamps may be deferred or coalesced, so compare a second bounded pass
    from the same descriptor before final checks on every platform.
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
        data = _read_bounded(fd, max_bytes)
        after = os.fstat(fd)
        if len(data) > max_bytes:
            raise FileReadError(f"File input changed while reading or exceeds its byte limit: {path}")
        verified = after
        if _VERIFY_CONTENT:
            os.lseek(fd, 0, os.SEEK_SET)
            if _read_bounded(fd, max_bytes) != data:
                raise FileReadError(f"File input changed during content verification: {path}")
            verified = os.fstat(fd)
        last = path.lstat()
        _regular(last, path)
        if (_stamp(opened) != _stamp(after) or _stamp(after) != _stamp(verified)
                or _stamp(before) != _stamp(last)
                or _stamp(verified, cross_api=True) != _stamp(last, cross_api=True)):
            raise FileReadError(f"File input changed while reading or exceeds its byte limit: {path}")
        return data
    finally:
        os.close(fd)
