"""Bounded local reads which refuse links, special files and changing inputs.

Every ancestor is anchored without following links. These cooperative identity
checks are not a filesystem sandbox or a lock against an external file writer.
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



def _directory(info, path):
    if (stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & 0x400):
        raise FileReadError(f"Unsupported symlink/reparse or non-directory ancestor: {path}")
    if not stat.S_ISDIR(info.st_mode):
        raise NotADirectoryError(str(path))


def directory_identity(info):
    return {"device": info.st_dev, "inode": info.st_ino}


def windows_file_id_identity(volume_serial, identifier):
    """One explicit WinAPI representation, independent of Python stat versions.

    Preserve all volume/file-ID bits. FILE_ID_128 is an opaque byte sequence,
    so encode its bytes directly rather than packing/truncating it as an inode.
    """
    if (type(volume_serial) is not int or not 0 <= volume_serial < 2**64
            or type(identifier) is not bytes or len(identifier) != 16):
        raise FileReadError("Invalid full Windows file identity")
    return {"identity_kind": "windows-file-id-info-v1", "device": f"{volume_serial:016x}",
            "inode": identifier.hex()}


def _windows_directory_identity(kernel, handle):
    import ctypes
    from ctypes import wintypes

    class FileId128(ctypes.Structure):
        _fields_ = [("Identifier", ctypes.c_ubyte * 16)]

    class FileIdInfo(ctypes.Structure):
        _fields_ = [("VolumeSerialNumber", ctypes.c_uint64), ("FileId", FileId128)]

    kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                   ctypes.c_void_p, wintypes.DWORD]
    kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
    value = FileIdInfo()
    if not kernel.GetFileInformationByHandleEx(handle, 18, ctypes.byref(value), ctypes.sizeof(value)):
        raise FileReadError("Full Windows FileIdInfo root identity is unavailable")
    return windows_file_id_identity(value.VolumeSerialNumber, bytes(value.FileId.Identifier))


class ParentDirectory:
    """Pin every ancestor; POSIX operations use dirfd, Windows denies retarget.

    Windows directory handles deliberately omit FILE_SHARE_DELETE. This pins
    the inspected chain while path-based Windows metadata/replace APIs run.
    No resolution of an untrusted path is used to expand ownership.
    """
    def __init__(self, path, *, create=False):
        self.path = Path(os.path.abspath(path))
        self.fd = None
        self._fds, self._handles, self._chain = [], [], []
        try:
            current = Path(self.path.anchor)
            self._pin(current, None)
            for part in self.path.parts[1:-1]:
                current = current / part
                if create:
                    try:
                        if self.fd is not None:
                            os.mkdir(part, dir_fd=self.fd)
                        else:
                            current.mkdir()
                    except FileExistsError:
                        pass
                self._pin(current, part)
            self.check()
        except BaseException:
            self.close()
            raise

    def _pin(self, path, part):
        before = path.lstat()
        _directory(before, path)
        if os.name == "posix" and hasattr(os, "O_NOFOLLOW"):
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
            descriptor = os.open(part if part is not None else path, flags,
                                 **({"dir_fd": self.fd} if part is not None else {}))
            self._fds.append(descriptor)
            opened = os.fstat(descriptor)
            self.fd = descriptor
        elif os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                          wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
            kernel.CreateFileW.restype = wintypes.HANDLE
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.CreateFileW(str(path), 0, 3, None, 3, 0x02200000, None)
            if handle == wintypes.HANDLE(-1).value:
                raise FileReadError("Cannot pin Windows ancestor: " + str(path))
            self._handles.append((kernel, handle))
            opened = path.lstat()
        else:
            raise FileReadError("Physical directory anchoring is unavailable on this platform")
        _directory(opened, path)
        if directory_identity(before) != directory_identity(opened):
            raise FileReadError("Ancestor changed while opening: " + str(path))
        self._chain.append((path, directory_identity(opened)))

    def physical_identity(self):
        """Identity of the pinned directory, with complete Windows FileIdInfo."""
        self.check()
        if os.name == "nt":
            kernel, handle = self._handles[-1]
            value = _windows_directory_identity(kernel, handle)
        else:
            value = directory_identity(os.fstat(self.fd))
        self.check()
        return value

    @property
    def binding(self):
        return [{"path": str(path), **identity} for path, identity in self._chain]

    def check(self):
        for path, identity in self._chain:
            try:
                info = path.lstat()
                _directory(info, path)
            except OSError as error:
                raise FileReadError("Ancestor changed: " + str(path)) from error
            if directory_identity(info) != identity:
                raise FileReadError("Ancestor physical identity changed: " + str(path))

    def open(self, flags, mode=0o600, *, name=None):
        self.check()
        name = self.path.name if name is None else name
        if Path(name).name != name:
            raise FileReadError("Anchored operation requires a leaf name")
        return os.open(name if self.fd is not None else self.path.parent / name,
                       flags | getattr(os, "O_NOFOLLOW", 0), mode,
                       **({"dir_fd": self.fd} if self.fd is not None else {}))

    def replace(self, staged):
        self.check()
        if self.fd is not None:
            os.replace(staged, self.path.name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        else:
            os.replace(self.path.parent / staged, self.path)
        self.sync()
        self.check()

    def unlink(self, name=None, *, missing_ok=False):
        name = self.path.name if name is None else name
        if Path(name).name != name:
            raise FileReadError("Anchored operation requires a leaf name")
        self.check()
        try:
            os.unlink(name if self.fd is not None else self.path.parent / name,
                      **({"dir_fd": self.fd} if self.fd is not None else {}))
            self.sync()
        except FileNotFoundError:
            if not missing_ok:
                raise
        self.check()

    def sync(self):
        if self.fd is not None:
            os.fsync(self.fd)
        # Windows directory FlushFileBuffers is not supported by this API.
        # No durable=true claim is made by this primitive on Windows.

    def close(self):
        for fd in reversed(self._fds):
            os.close(fd)
        for kernel, handle in reversed(self._handles):
            kernel.CloseHandle(handle)
        self._fds, self._handles = [], []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def parent_binding(path):
    """Inspect existing ancestors without creating any directory."""
    path = Path(os.path.abspath(path))
    while not path.parent.exists() and not path.parent.is_symlink():
        path = path.parent
    with ParentDirectory(path) as parent:
        return parent.binding


def verify_parent_binding(path, binding):
    current = parent_binding(path)
    if current[:len(binding)] != binding:
        raise FileReadError("Installation parent physical identity changed: " + str(path))


def _regular(info, path):
    if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
        raise FileReadError(f"Unsupported symlink/reparse file input: {path}")
    if not stat.S_ISREG(info.st_mode):
        raise FileReadError(f"Input is not a regular file: {path}")


def _stamp(info, *, cross_api=False):
    mode = info.st_mode
    # CPython Windows path stat synthesizes execute bits from executable
    # suffixes; fstat has no filename and reports only the attribute-derived
    # mode. These bits are not ACL permissions. Keep all other fields, and
    # compare the complete mode between observations from the same API.
    if cross_api and os.name == "nt":
        mode &= ~0o111
    value = (info.st_dev, info.st_ino, mode, info.st_size,
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


def _read_regular_file(path, max_bytes=DEFAULT_MAX_BYTES, missing_ok=False, *, parent) -> bytes | None:
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
    fd = parent.open(flags)
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
        parent.check()
        return data
    finally:
        os.close(fd)


def read_regular_file(path, max_bytes=DEFAULT_MAX_BYTES, missing_ok=False) -> bytes | None:
    try:
        with ParentDirectory(path) as parent:
            return _read_regular_file(Path(path), max_bytes, missing_ok, parent=parent)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise
