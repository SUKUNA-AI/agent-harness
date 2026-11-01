"""Digest-bound LSP text edits and a compensating local-file transaction.

This is cooperative file safety, not an OS sandbox or a globally atomic commit.
Only existing, regular, singly linked workspace files may be replaced.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import uuid

from .context import SECRET, _path, _read, _root
from .core import HarnessError, atomic_json, canonical, digest, now

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_PLAN_BYTES = 16 * 1024 * 1024
MAX_FILES = 256
MAX_EDITS = 10000


def identity(info):
    return {key: int(getattr(info, "st_" + key)) for key in
            ("dev", "ino", "size", "mode", "mtime_ns", "ctime_ns", "nlink", "uid", "gid")}


def capture(root, name):
    path = _path(root, name)
    before = path.lstat()
    data, _ = _read(path, MAX_FILE_BYTES, root=root, name=name)
    after = path.lstat()
    if data is None:
        raise HarnessError("LSP source exceeds the 8 MiB per-file limit")
    if identity(before) != identity(after):
        raise HarnessError("Source changed while capturing LSP input")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise HarnessError("LSP requires UTF-8 source files") from None
    if "\x00" in text or SECRET.search(text):
        raise HarnessError("LSP excludes binary files and possible credentials")
    return {"path": name, "absolute_path": path, "data": data, "text": text,
            "identity": identity(after), "sha256": hashlib.sha256(data).hexdigest()}


def _check_metadata(path):
    if os.name != "posix":
        return
    if not sys.platform.startswith("linux"):
        raise HarnessError("Rename metadata safety supports Linux and Windows; use a native editor on this platform")
    if getattr(path.lstat(), "st_flags", 0):
        raise HarnessError("Rename does not preserve platform file flags; use a native editor for this file")
    if not hasattr(os, "listxattr"):
        raise HarnessError("Cannot inspect extended attributes/ACLs; use a native editor for rename apply")
    if os.listxattr(path, follow_symlinks=False):
        raise HarnessError("Rename refuses extended attributes/ACLs instead of dropping them; use a native editor")


def _windows_security():
    """Use the two Windows inheritance models without converting legacy ACLs."""
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    api.GetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p,
                                     wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    api.GetFileSecurityW.restype = wintypes.BOOL
    api.GetSecurityDescriptorControl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.WORD),
                                                  ctypes.POINTER(wintypes.DWORD)]
    api.GetSecurityDescriptorControl.restype = wintypes.BOOL
    api.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    api.SetFileSecurityW.restype = wintypes.BOOL
    api.GetSecurityDescriptorDacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL),
                                            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)]
    api.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    api.SetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
                                        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    api.SetNamedSecurityInfoW.restype = wintypes.DWORD
    return ctypes, wintypes, api


def _windows_get_dacl(path):
    ctypes, wintypes, api = _windows_security()
    needed = wintypes.DWORD()
    api.GetFileSecurityW(str(path), 4, None, 0, ctypes.byref(needed))
    if not 0 < needed.value <= 65536:
        raise HarnessError("Cannot capture source DACL for rename staging")
    descriptor = ctypes.create_string_buffer(needed.value)
    if not api.GetFileSecurityW(str(path), 4, descriptor, needed.value, ctypes.byref(needed)):
        raise ctypes.WinError(ctypes.get_last_error())
    return descriptor.raw


def _windows_set_dacl(path, value, *, expected):
    ctypes, wintypes, api = _windows_security()
    descriptor = ctypes.create_string_buffer(value, len(value))
    control, revision = wintypes.WORD(), wintypes.DWORD()
    if not api.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)):
        raise ctypes.WinError(ctypes.get_last_error())
    if _windows_get_dacl(path) != expected:
        raise HarnessError("Windows DACL changed before restoration")
    if control.value & 0x0400:  # SE_DACL_AUTO_INHERITED: keep the modern model.
        present, defaulted, dacl = wintypes.BOOL(), wintypes.BOOL(), ctypes.c_void_p()
        if not api.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)) or not present.value:
            raise HarnessError("Cannot extract the original Windows DACL")
        information = 4 | (0x80000000 if control.value & 0x1000 else 0x20000000)
        error = api.SetNamedSecurityInfoW(str(path), 1, information, None, None, dacl, None)
        if error:
            raise ctypes.WinError(error)
    else:
        # SetFileSecurity ignores PROTECTED/UNPROTECTED_SECURITY_INFORMATION.
        # In the legacy model it uses the descriptor's protection control bit,
        # and does not add inherited ACEs or retain AUTO_INHERITED.
        if not api.SetFileSecurityW(str(path), 4, descriptor):
            raise ctypes.WinError(ctypes.get_last_error())
    if _windows_get_dacl(path) != value:
        raise HarnessError("Windows DACL preservation could not be verified")


def _windows_dacl(source, target):
    """Verify the staged file's exact DACL before writing source bytes."""
    original = _windows_get_dacl(source)
    current = _windows_get_dacl(target)
    if current != original:
        _windows_set_dacl(target, original, expected=current)


def _windows_replace(target, staged):
    # ReplaceFileW preserves DACLs, streams and supported original attributes.
    # No IGNORE_ACL_ERRORS / IGNORE_MERGE_ERRORS flag is permitted.
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.ReplaceFileW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                 wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
    api.ReplaceFileW.restype = wintypes.BOOL
    original = _windows_get_dacl(target)
    staged_identity = identity(staged.lstat())
    if not api.ReplaceFileW(str(target), str(staged), None, 0, None, None):
        raise ctypes.WinError(ctypes.get_last_error())
    if not _matches_staged(identity(target.lstat()), staged_identity):
        raise HarnessError("Rename target changed before DACL verification")
    current = _windows_get_dacl(target)
    if current != original:
        # ReplaceFileW can convert a legacy DACL to the modern inheritance model.
        # Restore its exact original ACEs/control, then require a full readback.
        _windows_set_dacl(target, original, expected=current)


class Positions:
    """Translate exact LSP UTF-16 positions; never split a surrogate or CRLF."""
    def __init__(self, text):
        self.text = text
        self.lines = []
        start = byte_start = 0
        for match in re.finditer(r"\r\n|\r|\n", text):
            content = text[start:match.start()]
            self.lines.append((content, byte_start))
            byte_start += len(text[start:match.end()].encode("utf-8"))
            start = match.end()
        self.lines.append((text[start:], byte_start))

    def from_human(self, line, column):
        if (type(line) is not int or type(column) is not int or line < 1 or column < 1
                or line > len(self.lines) or column > len(self.lines[line - 1][0]) + 1):
            raise HarnessError("Position requires an existing 1-based line and Unicode column")
        prefix = self.lines[line - 1][0][:column - 1]
        return {"line": line - 1, "character": len(prefix.encode("utf-16-le")) // 2}

    def offset(self, value):
        if (not isinstance(value, dict) or type(value.get("line")) is not int
                or type(value.get("character")) is not int or value["line"] < 0
                or value["character"] < 0 or value["line"] >= len(self.lines)):
            raise HarnessError("Invalid LSP UTF-16 position")
        content, byte_start = self.lines[value["line"]]
        units = byte_offset = 0
        for char in content:
            if units == value["character"]:
                return byte_start + byte_offset
            units += 2 if ord(char) > 0xFFFF else 1
            byte_offset += len(char.encode("utf-8"))
            if units > value["character"]:
                raise HarnessError("LSP position splits a UTF-16 surrogate pair")
        if units != value["character"]:
            raise HarnessError("LSP position is beyond the source line")
        return byte_start + byte_offset

    def human(self, value):
        offset = self.offset(value)
        content, byte_start = self.lines[value["line"]]
        prefix = content.encode("utf-8")[:offset - byte_start].decode("utf-8")
        return {"line": value["line"] + 1, "column": len(prefix) + 1}


def edited_bytes(data, edits):
    if not isinstance(edits, list) or len(edits) > MAX_EDITS:
        raise HarnessError("Invalid or excessive rename edits")
    ordered = []
    for edit in edits:
        if (not isinstance(edit, dict) or type(edit.get("start")) is not int
                or type(edit.get("end")) is not int or not isinstance(edit.get("text"), str)
                or not 0 <= edit["start"] <= edit["end"] <= len(data)):
            raise HarnessError("Invalid rename byte range")
        for offset in (edit["start"], edit["end"]):
            if offset < len(data) and data[offset] & 0xC0 == 0x80:
                raise HarnessError("Rename range splits a UTF-8 character")
        if "\x00" in edit["text"] or SECRET.search(edit["text"]):
            raise HarnessError("Rename excludes binary replacements and possible credentials")
        replacement = edit["text"].encode("utf-8")
        ordered.append((edit["start"], edit["end"], replacement))
    ordered.sort(key=lambda row: (row[0], row[1]))
    for prior, current in zip(ordered, ordered[1:]):
        if current[0] < prior[1] or current[0] == prior[0]:
            raise HarnessError("Overlapping or ambiguous rename edits are unsupported")
    output_size = len(data) + sum(len(text) - (end - start) for start, end, text in ordered)
    if output_size > MAX_FILE_BYTES:
        raise HarnessError("Renamed source exceeds the per-file limit")
    parts, cursor = [], 0
    for start, end, text in ordered:
        parts.extend((data[cursor:start], text))
        cursor = end
    parts.append(data[cursor:])
    return b"".join(parts)


def file_plan(source, lsp_edits):
    _check_metadata(source["absolute_path"])
    if source["identity"]["nlink"] != 1:
        raise HarnessError("Rename refuses hard-linked files; unlink explicitly before retrying")
    if not isinstance(lsp_edits, list) or len(lsp_edits) > MAX_EDITS:
        raise HarnessError("Invalid LSP edit list")
    positions, edits = Positions(source["text"]), []
    for edit in lsp_edits:
        if (not isinstance(edit, dict) or not isinstance(edit.get("range"), dict)
                or not isinstance(edit.get("newText"), str) or "annotationId" in edit):
            raise HarnessError("Only unannotated LSP text edits are supported")
        selected = edit["range"]
        edits.append({"start": positions.offset(selected.get("start")),
                      "end": positions.offset(selected.get("end")), "text": edit["newText"]})
    edits.sort(key=lambda edit: (edit["start"], edit["end"]))
    after = edited_bytes(source["data"], edits)
    return {"path": source["path"], "before_sha256": source["sha256"],
            "before_identity": source["identity"], "before_size": len(source["data"]),
            "after_sha256": hashlib.sha256(after).hexdigest(), "after_size": len(after),
            "edits": edits}


def make_plan(root, sources, changes, *, request, server):
    files = [file_plan(sources[name], changes[name]) for name in sorted(changes)]
    files = [item for item in files if item["before_sha256"] != item["after_sha256"]]
    if len(files) > MAX_FILES or sum(len(item["edits"]) for item in files) > MAX_EDITS:
        raise HarnessError("Rename exceeds file or edit limits; narrow the operation")
    if sum(item["before_size"] + item["after_size"] for item in files) > MAX_TOTAL_BYTES:
        raise HarnessError("Rename exceeds the 64 MiB combined source/output limit")
    plan = {"schema_version": 1, "purpose": "lsp_rename", "created_at": now(),
            "root_identity": digest(str(root)), "request": request, "server": server,
            "files": files, "file_count": len(files),
            "edit_count": sum(len(item["edits"]) for item in files)}
    plan["digest"] = digest(plan)
    if len(canonical(plan)) > MAX_PLAN_BYTES:
        raise HarnessError("Rename plan exceeds the 16 MiB limit")
    return plan


def read_plan(path):
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise HarnessError("Rename plan must be a regular file")
        data = source.read(MAX_PLAN_BYTES + 1)
    if len(data) > MAX_PLAN_BYTES:
        raise HarnessError("Rename plan exceeds the 16 MiB limit")
    return json.loads(data.decode("utf-8"))


@contextmanager
def workspace_lock(root):
    # A stable, private temporary directory gives cooperating invocations a
    # shared kernel lock without adding state files to the source checkout.
    user = str(os.getuid()) if hasattr(os, "getuid") else str(Path.home())
    directory = Path(tempfile.gettempdir()) / ("agent-harness-rename-" + digest(user)[:16])
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or directory.is_symlink()
            or getattr(info, "st_file_attributes", 0) & 0x400
            or (hasattr(os, "getuid") and (info.st_uid != os.getuid() or info.st_mode & 0o077))):
        raise HarnessError("Rename lock directory is not private and regular")
    anchor = directory / (digest(str(root)) + ".lock")
    if anchor.is_symlink():
        raise HarnessError("Rename lock must not be a symlink")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(anchor, flags, 0o600), "r+b") as guard:
        opened = os.fstat(guard.fileno())
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or anchor.lstat().st_ino != opened.st_ino
                or getattr(anchor.lstat(), "st_file_attributes", 0) & 0x400):
            raise HarnessError("Invalid rename lock anchor")
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(guard.fileno(), msvcrt.LK_NBLCK, 1)
            elif os.name == "posix":
                import fcntl
                fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                raise HarnessError("Rename apply requires OS file locking")
        except OSError:
            raise HarnessError("Another rename apply owns this workspace lock") from None
        try:
            yield
        finally:
            guard.seek(0)
            if os.name == "nt":
                msvcrt.locking(guard.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(guard.fileno(), fcntl.LOCK_UN)


class Parent:
    """Anchor POSIX replacements to the same inspected directory descriptor."""
    def __init__(self, root, name):
        self.root, self.name = root, name
        self.path = _path(root, name)
        self.descriptors = []
        self.fd = None
        self.windows_dacl_uncertain = False
        self.parent_identity = self.path.parent.stat()
        try:
            if os.open in os.supports_dir_fd and os.rename in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"):
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                descriptor = os.open(root, flags)
                self.descriptors.append(descriptor)
                for part in name.split("/")[:-1]:
                    descriptor = os.open(part, flags, dir_fd=descriptor)
                    self.descriptors.append(descriptor)
                self.fd = descriptor
            self.check()
        except BaseException:
            self.close()
            raise

    def close(self):
        for descriptor in reversed(self.descriptors):
            os.close(descriptor)
        self.descriptors = []

    def check(self):
        _path(self.root, self.name)
        current = self.path.parent.stat()
        opened = os.fstat(self.fd) if self.fd is not None else self.parent_identity
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise HarnessError("Rename parent directory changed")

    def stage(self, data, metadata):
        self.check()
        name = ".agent-harness-rename-" + uuid.uuid4().hex + ".tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        args = {"dir_fd": self.fd} if self.fd is not None else {}
        path = name if self.fd is not None else self.path.parent / name
        try:
            with os.fdopen(os.open(path, flags, 0o600, **args), "wb") as output:
                if os.name == "nt":
                    _windows_dacl(self.path, self.path.parent / name)
                elif hasattr(os, "fchown"):
                    staged_owner = os.fstat(output.fileno())
                    if (staged_owner.st_uid, staged_owner.st_gid) != (metadata["uid"], metadata["gid"]):
                        os.fchown(output.fileno(), metadata["uid"], metadata["gid"])
                output.write(data)
                output.flush()
                if hasattr(os, "fchmod"):
                    os.fchmod(output.fileno(), stat.S_IMODE(metadata["mode"]))
                else:
                    os.chmod(self.path.parent / name, stat.S_IMODE(metadata["mode"]))
                os.fsync(output.fileno())
                staged = identity(os.fstat(output.fileno()))
        except BaseException:
            self.unlink(name)
            raise
        return name, staged

    def replace(self, staged, *, expected, staged_source=None):
        self.check()
        actual = capture(self.root, self.name)
        if actual["identity"] != expected["identity"] or actual["sha256"] != expected["sha256"]:
            raise HarnessError("Rename target changed immediately before replacement")
        if staged_source is not None:
            self.verify_backup(staged, staged_source["identity"], staged_source["sha256"],
                               len(staged_source["data"]))
        if self.fd is not None:
            os.replace(staged, self.path.name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        elif os.name == "nt":
            self.windows_dacl_uncertain = True
            _windows_replace(self.path, self.path.parent / staged)
            self.windows_dacl_uncertain = False
        else:
            os.replace(self.path.parent / staged, self.path)

    def verify_backup(self, name, expected, sha256, size):
        self.check()
        relative = self.path.parent.relative_to(self.root) / name
        actual = capture(self.root, relative.as_posix())
        _check_metadata(actual["absolute_path"])
        # Before publication POSIX ctime must still match staging. Windows
        # fstat/lstat expose different ctime APIs; all other fields must match.
        same_identity = (_matches_staged(actual["identity"], expected) if os.name == "nt"
                         else actual["identity"] == expected)
        if (not same_identity
                or actual["sha256"] != sha256 or len(actual["data"]) != size):
            raise HarnessError("Rename backup changed; preserve recovery files for review")
        if os.name == "nt" and _windows_get_dacl(actual["absolute_path"]) != _windows_get_dacl(self.path):
            raise HarnessError("Rename backup DACL changed; preserve recovery files for review")
        return actual

    def unlink(self, name):
        try:
            if self.fd is not None:
                os.unlink(name, dir_fd=self.fd)
            else:
                self.check()
                (self.path.parent / name).unlink()
        except FileNotFoundError:
            pass


def _expected(root, item):
    current = capture(root, item["path"])
    _check_metadata(current["absolute_path"])
    if (current["sha256"] != item["before_sha256"]
            or current["identity"] != item["before_identity"]):
        raise HarnessError("Rename source changed; rebuild the preview: " + item["path"])
    return current


def _matches_staged(actual, staged):
    # Publishing a staged inode can legitimately change ctime. The other
    # captured metadata must not be adopted from an intervening user edit.
    return all(actual[key] == staged[key] for key in
               ("dev", "ino", "size", "mode", "mtime_ns", "nlink", "uid", "gid"))


def bound_apply_result(result, max_bytes):
    """Keep the transaction outcome and recovery handle under every budget.

    The same minimum is checked before staging, when the exact receipt path is
    known. Diagnostic strings and conflict details may consume only spare bytes.
    """
    if type(max_bytes) is not int or not 1024 <= max_bytes <= 262144:
        raise HarnessError("LSP output budget must be 1024..262144 UTF-8 bytes")
    def fits(value):
        try:
            return len(canonical(value)) + 2 <= max_bytes
        except UnicodeEncodeError:
            return False
    conflicts = result.get("conflicts", [])
    count = result.get("conflict_count", len(conflicts))
    full = dict(result, conflict_count=count, total_items=count,
                returned_items=len(conflicts), truncated=False, max_bytes=max_bytes)
    if fits(full):
        return full
    essential = ("status", "operation", "plan_digest", "recovery_receipt", "files", "edits")
    bounded = {key: result[key] for key in essential if key in result}
    bounded.update(conflict_count=count, total_items=count, returned_items=0,
                   conflicts=[], truncated=True, max_bytes=max_bytes)
    if not fits(bounded):
        raise HarnessError("Rename outcome and recovery receipt exceed the output budget; increase --max-bytes before applying")
    for key in ("globally_atomic", "detected_conflicts_preserved", "cleanup_warning_count"):
        if key in result and fits(dict(bounded, **{key: result[key]})):
            bounded[key] = result[key]
    # Keep a useful error prefix, counting UTF-8 and JSON escaping exactly.
    if isinstance(result.get("error"), str):
        low, high = 0, len(result["error"])
        while low < high:
            middle = (low + high + 1) // 2
            if fits(dict(bounded, error=result["error"][:middle])):
                low = middle
            else:
                high = middle - 1
        if fits(dict(bounded, error=result["error"][:low])):
            bounded["error"] = result["error"][:low]
    low, high = 0, len(conflicts)
    while low < high:
        middle = (low + high + 1) // 2
        if fits(dict(bounded, conflicts=conflicts[:middle], returned_items=middle)):
            low = middle
        else:
            high = middle - 1
    bounded.update(conflicts=conflicts[:low], returned_items=low)
    return bounded


def apply_plan(root, plan, *, result_budget=None):
    root = _root(root)
    if (not isinstance(plan, dict) or plan.get("schema_version") != 1
            or plan.get("purpose") != "lsp_rename"
            or plan.get("digest") != digest({key: value for key, value in plan.items() if key != "digest"})
            or plan.get("root_identity") != digest(str(root)) or len(canonical(plan)) > MAX_PLAN_BYTES):
        raise HarnessError("Rename plan integrity or workspace binding failed")
    files = plan.get("files")
    if not isinstance(files, list) or len(files) > MAX_FILES or plan.get("file_count") != len(files):
        raise HarnessError("Invalid rename file list")
    seen, total, edit_count = set(), 0, 0
    for item in files:
        if (not isinstance(item, dict) or not isinstance(item.get("path"), str)
                or item["path"] in seen or not isinstance(item.get("before_identity"), dict)
                or not isinstance(item.get("edits"), list)):
            raise HarnessError("Invalid or duplicate rename file")
        _path(root, item["path"])
        seen.add(item["path"])
        for key in ("before_size", "after_size"):
            if type(item.get(key)) is not int or not 0 <= item[key] <= MAX_FILE_BYTES:
                raise HarnessError("Invalid rename size")
            total += item[key]
        edit_count += len(item["edits"])
        for key in ("before_sha256", "after_sha256"):
            if not isinstance(item.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", item[key]):
                raise HarnessError("Invalid rename hash")
    if total > MAX_TOTAL_BYTES or edit_count > MAX_EDITS or plan.get("edit_count") != edit_count:
        raise HarnessError("Rename plan exceeds total limits or has invalid counts")
    if not files:
        return {"status": "no_changes", "operation": "apply", "plan_digest": plan["digest"],
                "files": 0, "edits": 0, "conflict_count": 0}
    with workspace_lock(root):
        prepared = []
        journal_dir = None
        journal = None
        committed = []
        try:
            # Validate every input before creating any staged workspace files.
            for item in files:
                source = _expected(root, item)
                if source["identity"]["nlink"] != 1 or len(source["data"]) != item["before_size"]:
                    raise HarnessError("Rename refuses hard links or changed source size")
                after = edited_bytes(source["data"], item["edits"])
                if len(after) != item["after_size"] or hashlib.sha256(after).hexdigest() != item["after_sha256"]:
                    raise HarnessError("Rename output does not match the reviewed plan")
                prepared.append({"item": item, "source": source, "after": after})
            journal_dir = Path(tempfile.mkdtemp(prefix="agent-harness-rename-recovery-"))
            if result_budget is not None:
                try:
                    bound_apply_result({"status": "failed_compensated", "operation": "apply",
                                        "plan_digest": plan["digest"], "files": len(files), "edits": edit_count,
                                        "recovery_receipt": str(journal_dir / "receipt.json"),
                                        "conflict_count": MAX_FILES + 1, "conflicts": []}, result_budget)
                except BaseException:
                    shutil.rmtree(journal_dir)
                    raise
            journal = {"schema_version": 1, "purpose": "rename_recovery", "root": str(root),
                       "plan_digest": plan["digest"], "status": "staging", "files": []}
            for row in prepared:
                item = row["item"]
                parent = row["parent"] = Parent(root, item["path"])
                row["backup"], row["backup_identity"] = parent.stage(row["source"]["data"], item["before_identity"])
                row["staged"], row["staged_identity"] = parent.stage(row["after"], item["before_identity"])
                record = row["record"] = {"path": item["path"], "before_sha256": item["before_sha256"],
                                           "after_sha256": item["after_sha256"], "state": "prepared",
                                           "backup": str(parent.path.parent / row["backup"]),
                                           "staged": str(parent.path.parent / row["staged"])}
                journal["files"].append(record)
            journal["status"] = "prepared"
            atomic_json(journal_dir / "receipt.json", journal)
            for row in prepared:
                _expected(root, row["item"])
            for row in prepared:
                _expected(root, row["item"])
                row["record"]["state"] = "replace_intent"
                atomic_json(journal_dir / "receipt.json", journal)
                # Check after journal IO as well: its duration is not a lock on editors.
                _expected(root, row["item"])
                # Record the intent before the syscall: an IO/interrupt error
                # may occur after replacement, with an otherwise unknown result.
                committed.append(row)
                row["parent"].replace(row["staged"], expected=row["source"])
                actual = capture(root, row["item"]["path"])
                _check_metadata(actual["absolute_path"])
                staged = row["staged_identity"]
                if (actual["sha256"] != row["item"]["after_sha256"]
                        or not _matches_staged(actual["identity"], staged)):
                    raise HarnessError("Rename target changed immediately after replacement")
                row["applied_identity"] = actual["identity"]
                row["record"]["state"] = "applied"
                atomic_json(journal_dir / "receipt.json", journal)
            journal["status"] = "applied"
            atomic_json(journal_dir / "receipt.json", journal)
            result = {"status": "applied", "operation": "apply", "plan_digest": plan["digest"],
                      "files": len(files), "edits": edit_count, "conflict_count": 0, "globally_atomic": False}
        except BaseException as error:
            if journal is None:
                raise
            conflicts = []
            for row in reversed(committed):
                try:
                    actual = capture(root, row["item"]["path"])
                    _check_metadata(actual["absolute_path"])
                    if row["parent"].windows_dacl_uncertain:
                        raise HarnessError("Windows DACL after replacement is unverified; preserve backup for recovery")
                    if (actual["sha256"] == row["source"]["sha256"]
                            and actual["identity"] == row["source"]["identity"]):
                        row["record"]["state"] = "unchanged"
                        continue
                    staged = row["staged_identity"]
                    expected = row.get("applied_identity")
                    if (actual["sha256"] != row["item"]["after_sha256"]
                            or not _matches_staged(actual["identity"], staged)
                            or (expected is not None and actual["identity"] != expected)):
                        raise HarnessError("Concurrent change preserved")
                    backup = dict(row["source"], identity=row["backup_identity"])
                    row["parent"].replace(row["backup"], expected=actual, staged_source=backup)
                    restored = capture(root, row["item"]["path"])
                    _check_metadata(restored["absolute_path"])
                    if (restored["sha256"] != row["source"]["sha256"]
                            or not _matches_staged(restored["identity"], row["backup_identity"])):
                        raise HarnessError("Rename restoration could not be verified")
                    row["record"]["state"] = "restored"
                except (HarnessError, OSError) as recovery_error:
                    row["record"]["state"] = "recovery_required"
                    conflicts.append({"path": row["item"]["path"], "error": str(recovery_error)[:300]})
            journal["status"] = "recovery_required" if conflicts else "failed_compensated"
            journal["error"] = type(error).__name__ + ": " + str(error)[:300]
            journal["conflicts"] = conflicts
            try:
                atomic_json(journal_dir / "receipt.json", journal)
            except (OSError, HarnessError):
                # Earlier intent records and backup files remain reviewable.
                conflicts.append({"error": "Could not publish final recovery receipt"})
                journal["status"] = "recovery_required"
            result = {"status": journal["status"], "operation": "apply", "plan_digest": plan["digest"],
                      "files": len(files), "edits": edit_count, "conflict_count": len(conflicts),
                      "error": journal["error"], "recovery_receipt": str(journal_dir / "receipt.json"),
                      "conflicts": conflicts, "detected_conflicts_preserved": True, "globally_atomic": False}
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
        finally:
            keep = journal is not None and journal.get("status") == "recovery_required"
            cleanup_errors = []
            for row in prepared:
                parent = row.get("parent")
                if parent is not None:
                    try:
                        if not keep:
                            for key in ("backup", "staged"):
                                if row.get(key):
                                    try:
                                        parent.unlink(row[key])
                                    except (OSError, HarnessError) as cleanup_error:
                                        cleanup_errors.append({"path": row["item"]["path"],
                                                               "error": type(cleanup_error).__name__})
                    finally:
                        parent.close()
            if cleanup_errors and journal_dir is not None:
                result["cleanup_warnings"] = cleanup_errors
                result["cleanup_warning_count"] = len(cleanup_errors)
                result["recovery_receipt"] = str(journal_dir / "receipt.json")
            elif journal_dir is not None and journal is not None and journal.get("status") == "applied":
                try:
                    shutil.rmtree(journal_dir)
                except OSError:
                    result["recovery_receipt"] = str(journal_dir / "receipt.json")
        return result
