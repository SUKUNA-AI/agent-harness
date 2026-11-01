"""Conservative, reversible local Codex installation. No agent execution API."""
from __future__ import annotations

import argparse
import contextlib
import contextvars
import datetime as dt
import difflib
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import uuid

from .file_io import FileReadError, read_regular_file, _stamp

VERSION = "1.4.1"
START = "<!-- agent-harness:begin -->"
END = "<!-- agent-harness:end -->"
AUTO_COMPACT_TOKEN_LIMIT = 500000
CAP_KEY = "max_concurrent_threads_per_session"
MANAGED = {"enabled": True, "max_concurrent_threads_per_session": 4, "interrupt_message": True}
SENSITIVE = re.compile(r"(?i)(token|secret|password|credential|authorization|api[_-]?key|headers)")
_RESTORE_METADATA = contextvars.ContextVar("installation_restore_metadata", default=None)
_PUBLICATION = contextvars.ContextVar("installation_publication", default=None)


class InstallError(ValueError):
    pass


class RollbackError(InstallError):
    def __init__(self, report: dict):
        super().__init__(report["error"])
        self.report = report


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read(path: Path) -> bytes | None:
    """Installation inputs are ordinary files, limited to 16 MiB each."""
    try:
        return read_regular_file(path, missing_ok=True)
    except FileReadError as error:
        raise InstallError(str(error)) from error


def required_read(path: Path) -> bytes:
    value = read(path)
    if value is None:
        raise InstallError(f"Required installation file is missing: {path}")
    return value


def decode(data: bytes | None) -> str:
    return (data or b"").decode("utf-8-sig")


def encode_like(text: str, original: bytes | None) -> bytes:
    return text.encode("utf-8-sig" if original and original.startswith(b"\xef\xbb\xbf") else "utf-8")


def regular_target(path: Path) -> None:
    """Never replace a link/special file with a regular file byte backup.

    lstat also detects dangling links. Windows reparse attributes cover junctions
    and other link-like entries on Python versions without Path.is_junction.
    """
    try:
        observed = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(observed.st_mode) or getattr(observed, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        raise InstallError(f"Unsupported symlink/reparse installation target; no link replaced: {path}")
    if not stat.S_ISREG(observed.st_mode):
        raise InstallError(f"Installation target is not a regular file: {path}")


def windows_helper(name: str, *args, **kwargs):
    from . import rename
    from .core import HarnessError
    try:
        return getattr(rename, name)(*args, **kwargs)
    except HarnessError as error:
        raise InstallError(str(error)) from error


def windows_owner_group(path: Path) -> dict:
    """Owner/group changes need privileges; refuse them rather than changing ownership."""
    ctypes, wintypes, api = windows_helper("_windows_security")
    needed = wintypes.DWORD()
    api.GetFileSecurityW(str(path), 3, None, 0, ctypes.byref(needed))
    if not 0 < needed.value <= 65536:
        raise InstallError(f"Cannot inspect Windows installation ownership: {path}")
    descriptor = ctypes.create_string_buffer(needed.value)
    if not api.GetFileSecurityW(str(path), 3, descriptor, needed.value, ctypes.byref(needed)):
        raise ctypes.WinError(ctypes.get_last_error())
    api.GetLengthSid.argtypes = [ctypes.c_void_p]
    api.GetLengthSid.restype = wintypes.DWORD
    result = {}
    for name in ("Owner", "Group"):
        getter = getattr(api, "GetSecurityDescriptor" + name)
        getter.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)]
        getter.restype = wintypes.BOOL
        sid, defaulted = ctypes.c_void_p(), wintypes.BOOL()
        if not getter(descriptor, ctypes.byref(sid), ctypes.byref(defaulted)) or not sid.value:
            raise InstallError(f"Cannot inspect Windows installation ownership: {path}")
        length = api.GetLengthSid(sid)
        if not 0 < length <= 65536:
            raise InstallError(f"Invalid Windows installation ownership: {path}")
        result[name.lower()] = ctypes.string_at(sid, length).hex()
    return result


def metadata(path: Path) -> dict | None:
    """Capture replaceable security metadata, refusing unsupported preservation.

    Content timestamps intentionally change on an edit. Identity timestamps are
    used only to detect concurrent changes, not restored on the replacement.
    """
    regular_target(path)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    if before.st_nlink != 1:
        raise InstallError(f"Cannot preserve installation target hard links: {path}")
    if getattr(before, "st_flags", 0):
        raise InstallError(f"Cannot preserve platform file flags: {path}")
    if os.name == "nt":
        # Ordinary archive/normal attributes are compatible with ReplaceFileW.
        # Hidden, readonly, encrypted, sparse, compressed or other special
        # attributes require a native tool rather than uncertain restoration.
        if before.st_file_attributes & ~(0x20 | 0x80):
            raise InstallError(f"Unsupported Windows installation file attributes: {path}")
        security = {"platform": "windows", "dacl": windows_helper("_windows_get_dacl", path).hex(),
                    "owner_group": windows_owner_group(path),
                    "mode": stat.S_IMODE(before.st_mode),
                    "attributes": before.st_file_attributes}
    elif os.name == "posix" and sys.platform.startswith("linux"):
        if not all(hasattr(os, name) for name in ("listxattr", "getxattr", "setxattr", "removexattr")):
            raise InstallError("Cannot inspect/preserve installation extended attributes and ACLs")
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        try:
            opened = os.fstat(fd)
            if _stamp(before, cross_api=True) != _stamp(opened, cross_api=True):
                raise InstallError(f"Concurrent metadata change: {path}")
            # Linux inode flags beyond EXTENTS describe behavior we cannot
            # faithfully transfer to a replacement inode. Refuse them upfront.
            import array
            import errno
            import fcntl
            flags = array.array("l", [0])
            try:
                request = 0x80006601 | (flags.itemsize << 16)  # FS_IOC_GETFLAGS
                fcntl.ioctl(fd, request, flags, True)
            except OSError as error:
                if error.errno not in (errno.ENOTTY, errno.EOPNOTSUPP, errno.EINVAL):
                    raise
            if flags[0] & ~0x80000:  # FS_EXTENT_FL is a storage-layout detail.
                raise InstallError(f"Cannot preserve Linux inode flags: {path}")
            attrs = {name: os.getxattr(fd, name).hex() for name in sorted(os.listxattr(fd))}
            security = {"platform": "linux", "mode": stat.S_IMODE(opened.st_mode),
                        "uid": opened.st_uid, "gid": opened.st_gid, "xattrs": attrs}
            if _stamp(opened) != _stamp(os.fstat(fd)):
                raise InstallError(f"Concurrent metadata change: {path}")
        finally:
            os.close(fd)
    else:
        raise InstallError("Installation metadata preservation supports Linux and Windows only")
    regular_target(path)
    last = path.lstat()
    if _stamp(before) != _stamp(last):
        raise InstallError(f"Concurrent metadata change: {path}")
    if os.name == "nt" and (
            last.st_file_attributes != before.st_file_attributes
            or windows_helper("_windows_get_dacl", path).hex() != security["dacl"]
            or windows_owner_group(path) != security["owner_group"]):
        raise InstallError(f"Concurrent Windows security metadata change: {path}")
    return {"identity": _stamp(last), "security": security}


def copy_metadata(path: Path, original: dict | None) -> None:
    if original is None:
        return
    value = original["security"]
    if value["platform"] == "windows":
        if windows_owner_group(path) != value["owner_group"]:
            raise InstallError(f"Cannot preserve Windows installation ownership: {path}")
        expected = windows_helper("_windows_get_dacl", path)
        desired = bytes.fromhex(value["dacl"])
        if expected != desired:
            windows_helper("_windows_set_dacl", path, desired, expected=expected)
        os.chmod(path, value["mode"])
    else:
        observed = path.stat()
        if (observed.st_uid, observed.st_gid) != (value["uid"], value["gid"]):
            os.chown(path, value["uid"], value["gid"], follow_symlinks=False)
        os.chmod(path, value["mode"], follow_symlinks=False)
        # A staging file may inherit its own parent ACL; copy the exact source
        # set, including deleting attributes not present on the original.
        for name in set(os.listxattr(path, follow_symlinks=False)) - value["xattrs"].keys():
            os.removexattr(path, name, follow_symlinks=False)
        for name, data in value["xattrs"].items():
            os.setxattr(path, name, bytes.fromhex(data), follow_symlinks=False)
    actual = metadata(path)["security"]
    # Windows ReplaceFileW carries original attributes onto the final file;
    # temporary-file attributes need not match before that replacement.
    if {k: v for k, v in actual.items() if k != "attributes"} != {k: v for k, v in value.items() if k != "attributes"}:
        raise InstallError(f"Installation metadata preservation could not be verified: {path}")


def preflight_metadata(plans: list[dict]) -> None:
    """Prove every metadata set can be staged before changing any target."""
    for p in plans:
        observed = metadata(p["path"])
        if "_metadata" in p and observed != p["_metadata"]:
            raise InstallError(f"Concurrent metadata edit before transaction: {p['path']}")
        p["_metadata"] = observed
        if read(p["path"]) != p["before"]:
            raise InstallError(f"Concurrent edit before transaction: {p['path']}")
    for p in plans:
        if p["_metadata"] is None or p["after"] is None:
            continue
        fd, name = tempfile.mkstemp(prefix=".harness-metadata-", dir=p["path"].parent)
        os.close(fd)
        try:
            copy_metadata(Path(name), p["_metadata"])
        finally:
            os.unlink(name)


def check_planned_metadata(p: dict) -> None:
    if metadata(p["path"]) != p["_metadata"]:
        raise InstallError(f"Concurrent metadata edit; user edit preserved: {p['path']}")


def atomic(path: Path, data: bytes) -> dict:
    original = metadata(path)
    restore = _RESTORE_METADATA.get()
    desired = restore[1] if restore is not None and restore[0] == path else original
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".harness-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        copy_metadata(Path(name), desired)
        staged = metadata(Path(name))
        receipt = {"identity": staged["identity"][:-1],
                   "security": desired["security"] if desired is not None else staged["security"]}
        if metadata(path) != original:
            raise InstallError(f"Concurrent metadata edit before replacement: {path}")
        # Retain the exact staged receipt even if a replacement commits and its
        # wrapper subsequently raises. Post-write observations never establish
        # ownership of security metadata introduced by another writer.
        publication = _PUBLICATION.get()
        if publication is not None and publication["path"] == path:
            publication["_applied_metadata"] = receipt
        if os.name == "nt" and original is not None:
            windows_helper("_windows_replace", path, Path(name))
        else:
            os.replace(name, path)
        if not matches_receipt(path, receipt):
            raise InstallError(f"Published installation metadata changed; user edit preserved: {path}")
        return receipt
    finally:
        if os.path.exists(name):
            os.unlink(name)


def restore_target(path: Path, data: bytes | None, original: dict | None) -> None:
    """Restore deleted files with their captured ACLs before publication."""
    if data is None:
        path.unlink(missing_ok=True)
        return
    token = _RESTORE_METADATA.set((path, original))
    try:
        atomic(path, data)
    finally:
        _RESTORE_METADATA.reset(token)


def matches_receipt(path: Path, receipt: dict | None) -> bool:
    current = metadata(path)
    if current is None or receipt is None:
        return current is None and receipt is None
    # Publication changes inode ctime. All other identity fields and the exact
    # security set must match the verified staging inode, never a fresh target.
    return (current["identity"][:-1] == receipt["identity"]
            and current["security"] == receipt["security"])


def owns_applied_metadata(p: dict) -> bool:
    return "_applied_metadata" in p and matches_receipt(p["path"], p["_applied_metadata"])


def publish_target(p: dict) -> None:
    p.pop("_applied_metadata", None)
    token = _PUBLICATION.set(p)
    try:
        if p["after"] is None:
            p["_applied_metadata"] = None
            p["path"].unlink(missing_ok=True)
        else:
            atomic(p["path"], p["after"])
        if not owns_applied_metadata(p):
            raise InstallError(f"Published installation metadata is unverified; user edit preserved: {p['path']}")
    finally:
        _PUBLICATION.reset(token)


def ordinary_legacy_metadata(security: dict) -> bool:
    """The only defensible baseline for old installer-created POSIX files."""
    return (security["platform"] == "linux" and security["mode"] == 0o600
            and security["uid"] == os.geteuid() and security["gid"] == os.getegid()
            and not security["xattrs"])


def canonical(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


@contextlib.contextmanager
def lock(state: Path):
    state.mkdir(parents=True, exist_ok=True)
    path = state / "install.lock"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise InstallError(f"Installation writer lock exists: {path}. Inspect its owner; no automatic removal.")
    try:
        os.write(fd, canonical({"pid": os.getpid(), "created": dt.datetime.now(dt.timezone.utc).isoformat()}))
        os.close(fd)
        yield
    finally:
        path.unlink(missing_ok=True)


def selected_instructions(home: Path) -> Path:
    override = home / "AGENTS.override.md"
    return override if decode(read(override)).strip() else home / "AGENTS.md"


def managed_block(text: str) -> str | None:
    if text.count(START) != text.count(END) or text.count(START) > 1:
        raise InstallError("Malformed or duplicated harness instruction markers")
    if START not in text:
        return None
    a, b = text.index(START), text.index(END) + len(END)
    if b < a:
        raise InstallError("Invalid harness marker order")
    return text[a:b]


def replace_block(text: str, block: str) -> str:
    previous = managed_block(text)
    if previous is not None:
        return text.replace(previous, block, 1)
    return text + ("\n" if text.endswith("\n") else "\n\n" if text else "") + block + "\n"


def assignment(text: str, key: str, root: bool = False) -> tuple[int, str] | None:
    """Находит управляемую строку в корне TOML или agents, включая dotted-форму."""
    table = ""
    matches = []
    for i, line in enumerate(text.splitlines(keepends=True)):
        stripped = line.strip()
        if stripped.startswith("["):
            m = re.fullmatch(r"\[\s*agents\s*\]\s*(?:#.*)?", stripped)
            table = "agents" if m else "other"
            continue
        pattern = rf"\s*{re.escape(key)}\s*=" if table == "agents" else rf"\s*agents\.{re.escape(key)}\s*=" if table == "" else None
        if root:
            pattern = rf"\s*{re.escape(key)}\s*=" if table == "" else None
        if pattern and re.match(pattern, line):
            matches.append((i, line))
    if len(matches) > 1:
        raise InstallError(f"Duplicate managed key {key if root else 'agents.' + key}")
    return matches[0] if matches else None


def render_value(value: object) -> str:
    return str(value).lower() if isinstance(value, bool) else str(value)


def positive_cap(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise InstallError("Native child cap must be a positive integer")
    return value


def line_value(line: str, key: str) -> object:
    """Read a scalar assignment from our metadata, including dotted agents keys."""
    parsed = tomllib.loads(line)
    return parsed.get("agents", parsed)[key]


def normalize_cap_alias(text: str, agents: dict) -> tuple[str, list[dict]]:
    """Keep one native cap key; record enough text to undo the migration exactly."""
    if "max_threads" not in agents:
        return text, []
    found = assignment(text, "max_threads")
    if found is None:
        raise InstallError("Unsupported quoted/inline agents.max_threads; no rewrite performed")
    index, old = found
    if CAP_KEY in agents:
        # Leave a uniquely identified comment where the removed alias was. This
        # retains the location for a later rollback without moving user fields.
        eol = "\r\n" if old.endswith("\r\n") else "\n" if old.endswith("\n") else ""
        new = "# agent-harness:legacy-cap " + digest(old.encode("utf-8"))[:16] + eol
        kind = "remove"
    else:
        new = re.sub(r"^(\s*(?:agents\.)?)max_threads(?=\s*=)", rf"\g<1>{CAP_KEY}", old)
        kind = "rename"
    lines = text.splitlines(keepends=True)
    lines[index] = new
    result = "".join(lines)
    expected = tomllib.loads(text)
    legacy = expected["agents"].pop("max_threads")
    if kind == "rename":
        expected["agents"][CAP_KEY] = legacy
    if tomllib.loads(result) != expected:
        raise InstallError("Cap alias migration would change unrelated TOML values")
    return result, [{"kind": kind, "before": old, "after": new}]


def patch_config(text: str, preferred_cap: int = 4, explicit_cap: bool = False) -> tuple[str, dict]:
    preferred_cap = positive_cap(preferred_cap)
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise InstallError(f"Invalid existing TOML; no write: {e}")
    agents = data.get("agents", {})
    if not isinstance(agents, dict):
        raise InstallError("agents must be a table")
    if agents.get("enabled") is False:
        raise InstallError("Explicit agents.enabled=false conflicts with installation; user decision required")
    for boolean in ("enabled", "interrupt_message"):
        if boolean in agents and not isinstance(agents[boolean], bool):
            raise InstallError(f"agents.{boolean} must be boolean")
    desired = dict(MANAGED)
    desired["max_concurrent_threads_per_session"] = preferred_cap
    caps = [agents[k] for k in ("max_concurrent_threads_per_session", "max_threads") if k in agents]
    if any(not isinstance(n, int) or isinstance(n, bool) or n < 1 for n in caps):
        raise InstallError("Existing agents cap must be a positive integer")
    if caps and not explicit_cap:
        desired["max_concurrent_threads_per_session"] = min([preferred_cap, *caps])
    # A user disabling interrupt messages is preserved; changing it is unnecessary for delegation.
    if "interrupt_message" in agents:
        desired["interrupt_message"] = agents["interrupt_message"]
    text, alias_edits = normalize_cap_alias(text, agents)
    # Existing min/explicit-cap selection used both original aliases. All edits
    # from here operate on the unambiguous normalized configuration.
    data = tomllib.loads(text)
    agents = data.get("agents", {})
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    edits = {}
    missing = []
    for key, value in desired.items():
        found = assignment(text, key)
        if key in agents and found is None:
            raise InstallError(f"Unsupported quoted/inline agents.{key}; preserve file and use an explicit compatible table")
        if found:
            i, old = found
            if agents[key] == value:
                continue
            # Explicit --max-child-threads authorizes a cap increase; default never raises a stricter cap.
            new = re.sub(r"(=\s*)([^#\r\n]*?)(\s*(?:#.*)?)(\r?\n)?$", lambda m: m[1] + render_value(value) + m[3] + (m[4] or ""), old)
            lines[i] = new
            edits[key] = {"before": old, "after": new}
        else:
            missing.append(key)
    result = "".join(lines)
    header = re.search(r"(?m)^\[\s*agents\s*\]\s*(?:#[^\r\n]*)?\r?$", result)
    table_created = False
    if missing:
        # Existing dotted agents fields create the table semantically; add missing dotted fields at root.
        dotted = any(assignment(text, k) and assignment(text, k)[1].lstrip().startswith("agents.") for k in (*desired, "max_threads"))
        if not header and "agents" in data and not dotted:
            raise InstallError("Unsupported inline/quoted agents table; no rewrite performed")
        additions = []
        for key in missing:
            new = ("agents." if dotted else "") + key + " = " + render_value(desired[key]) + eol
            additions.append(new)
            edits[key] = {"before": None, "after": new}
        if dotted:
            first_table = re.search(r"(?m)^\s*\[", result)
            at = first_table.start() if first_table else len(result)
            prefix = "" if at == 0 or result[:at].endswith(eol) else eol
            if prefix:
                # Appending to a dotted config can terminate its original last
                # line. That separator belongs to this patch, so the recorded
                # installed lines must include it (including an alias which is
                # restored after a separate cap-value edit during rollback).
                for edit in (*edits.values(), *alias_edits):
                    if edit["after"] and not edit["after"].endswith("\n"):
                        edit["after"] += prefix
                        edit["separator_added"] = prefix
            result = result[:at] + prefix + "".join(additions) + result[at:]
        elif header:
            at = result.find("\n", header.end())
            if at < 0:
                result += eol + "".join(additions)
            else:
                result = result[:at + 1] + "".join(additions) + result[at + 1:]
        else:
            table_created = True
            result += (eol if result.endswith(eol) else eol + eol if result else "") + "[agents]" + eol + "".join(additions)
    root_edits = {}
    compact_key = "model_auto_compact_token_limit"
    if compact_key not in data:
        # Вставка после ведущих комментариев не путает таблицы с multiline-строками.
        new = f"{compact_key} = {AUTO_COMPACT_TOKEN_LIMIT}{eol}"
        at = re.match(r"(?:[ \t]*(?:#[^\r\n]*)?\r?\n)*", result).end()
        result = result[:at] + new + result[at:]
        root_edits[compact_key] = {"before": None, "after": new}
    parsed = tomllib.loads(result)
    if parsed.get("agents", {}) != {**agents, **desired}:
        raise InstallError("Config patch changed unexpected agent settings")
    original_other = {k: v for k, v in data.items() if k != "agents"}
    if root_edits:
        original_other[compact_key] = AUTO_COMPACT_TOKEN_LIMIT
    if {k: v for k, v in parsed.items() if k != "agents"} != original_other:
        raise InstallError("Config patch changed unrelated settings")
    return result, {"keys": edits, "root_keys": root_edits, "alias_edits": alias_edits, "table_created": table_created}


def replace_rollback_line(lines: list[str], index: int, replacement: str) -> None:
    """Keep a separator until later rollback edits remove the following lines."""
    if replacement and not replacement.endswith("\n") and index + 1 < len(lines):
        replacement += "\r\n" if lines[index].endswith("\r\n") else "\n"
    lines[index] = replacement


def revert_config(before: str, installed: str, current: str, meta: dict) -> str:
    if current == installed:
        return before
    # Older manifests could omit a separator added after a final dotted
    # assignment. Recover that exact byte from the verified installed copy,
    # never from the user's current file, and keep the stored manifest intact.
    meta = {**meta, "keys": {key: dict(edit) for key, edit in meta["keys"].items()}}
    for key, edit in meta["keys"].items():
        if edit["after"] and not edit["after"].endswith("\n"):
            observed = assignment(installed, key)
            for separator in ("\n", "\r\n"):
                if observed and observed[1] == edit["after"] + separator:
                    edit["after"] = observed[1]
                    edit["separator_added"] = separator
                    break
    result = current
    installed_data = tomllib.loads(installed)
    # Старые manifests содержат только keys; root_keys появился вместе с порогом.
    for root, edits in ((False, meta["keys"]), (True, meta.get("root_keys", {}))):
        for key, edit in edits.items():
            expected = tomllib.loads(result)
            section = expected if root else expected.get("agents", {})
            installed_section = installed_data if root else installed_data.get("agents", {})
            label = key if root else "agents." + key
            if key not in section or section[key] != installed_section.get(key):
                raise InstallError(f"Rollback conflict at {label}; user edit preserved")
            if edit["before"] is not None:
                section[key] = line_value(edit["before"], key)
            else:
                section.pop(key)
            found = assignment(result, key, root=root)
            if found is None or found[1] != edit["after"]:
                raise InstallError(f"Rollback conflict at {key if root else 'agents.' + key}; user edit preserved")
            lines = result.splitlines(keepends=True)
            replace_rollback_line(lines, found[0], edit["before"] or "")
            result = "".join(lines)
            # The textual locator can see a TOML example inside a multiline
            # string. Check the full semantic delta for agents AND root keys.
            if tomllib.loads(result) != expected:
                raise InstallError(f"Rollback conflict at {label}; unrelated settings preserved")
    for edit in reversed(meta.get("alias_edits", [])):
        expected = tomllib.loads(result)
        agents = expected.get("agents", {})
        if "max_threads" in agents:
            raise InstallError("Rollback conflict at legacy agents.max_threads; user edit preserved")
        lines = result.splitlines(keepends=True)
        if edit["kind"] == "rename":
            found = assignment(result, CAP_KEY)
            if found is None or found[1] != edit["after"] or agents.get(CAP_KEY) != line_value(edit["before"], "max_threads"):
                raise InstallError("Rollback conflict at cap alias migration; user edit preserved")
            index = found[0]
            agents.pop(CAP_KEY)
        elif edit["kind"] == "remove":
            matches = [i for i, line in enumerate(lines) if line == edit["after"]]
            if len(matches) != 1:
                raise InstallError("Rollback conflict at removed cap alias; user edit preserved")
            index = matches[0]
        else:
            raise InstallError("Unsupported cap alias migration metadata")
        agents["max_threads"] = line_value(edit["before"], "max_threads")
        replace_rollback_line(lines, index, edit["before"])
        result = "".join(lines)
        if tomllib.loads(result) != expected:
            raise InstallError("Cap alias rollback would change unrelated TOML values")
    if meta["table_created"]:
        # Remove our table only if no user content was added to it.
        cleaned = re.sub(r"(?m)^\[agents\]\r?\n(?=\s*(?:\[|\Z))", "", result)
        if cleaned != result:
            expected = tomllib.loads(result)
            if expected.get("agents") == {}:
                expected.pop("agents")
            if tomllib.loads(cleaned) != expected:
                raise InstallError("Rollback table cleanup would change unrelated TOML values")
            result = cleaned
    for edit in (*meta["keys"].values(), *meta.get("alias_edits", [])):
        separator = edit.get("separator_added")
        if separator and edit["before"]:
            lines = result.splitlines(keepends=True)
            if lines and lines[-1] == edit["before"] + separator:
                # The original EOF is restored only when nothing now follows
                # it. A user-added section still needs the line separator.
                result = result[:-len(separator)]
    tomllib.loads(result)
    return result


def redacted_diff(path: Path, old: bytes | None, new: bytes) -> str:
    old_text, new_text = decode(old), decode(new)
    # Config context is zero: unrelated values are never copied into the report.
    lines = difflib.unified_diff(old_text.splitlines(), new_text.splitlines(), fromfile=str(path), tofile=str(path), n=0)
    return "\n".join("<redacted sensitive line>" if SENSITIVE.search(line) and not line.startswith(("---", "+++")) else line for line in lines)


def active_manifest(state: Path) -> dict | None:
    p = state / "active.json"
    if not p.exists():
        return None
    ref = json.loads(decode(required_read(p)))
    if not isinstance(ref, dict) or not isinstance(ref.get("manifest"), str) or not isinstance(ref.get("sha256"), str):
        raise InstallError("Invalid active installation reference")
    target = Path(ref["manifest"])
    if not target.is_file() or digest(required_read(target)) != ref["sha256"]:
        raise InstallError("Active installation manifest is missing or modified")
    manifest = json.loads(decode(required_read(target)))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or manifest.get("status") != "written_pending_native_verification":
        raise InstallError("Active installation manifest is not a committed installation")
    return manifest


def manifest_chain(manifest: dict | None) -> list[dict]:
    chain = []
    seen = set()
    while manifest:
        identity = manifest["install_id"]
        if identity in seen:
            raise InstallError("Installation history contains a cycle")
        seen.add(identity)
        chain.append(manifest)
        ref = manifest.get("previous_manifest")
        if not ref:
            break
        p = Path(ref["manifest"])
        if digest(required_read(p)) != ref["sha256"]:
            raise InstallError("Previous installation manifest integrity error")
        manifest = json.loads(decode(required_read(p)))
    return chain


def plan(package: Path, home: Path, skills: Path, state: Path, overlay: Path, max_child_threads: int | None = None) -> list[dict]:
    if not (package / "policies/global.md").is_file() or not (package / "tools/harness.py").is_file():
        raise InstallError("Package policy or executable entrypoint missing")
    for target in (home / "config.toml", selected_instructions(home), overlay, state / "active.json"):
        regular_target(target)
    old_active = active_manifest(state)
    if old_active is None and ((home / "harness-machine.json").exists() or managed_block(decode(read(selected_instructions(home))))):
        raise InstallError(
            "Installed harness targets have no active manifest; inspect the installation "
            f"transactions in {state / 'installs'} and recover with rollback --manifest before reinstalling"
        )
    owned = {x["path"]: x for m in reversed(manifest_chain(old_active)) for x in m["files"]}
    existing_machine = json.loads(decode(required_read(overlay))) if overlay.exists() else None
    if existing_machine is not None and existing_machine.get("schema_version") != 1:
        raise InstallError("Unsupported machine overlay schema")
    preferred_cap = positive_cap(max_child_threads if max_child_threads is not None else (existing_machine or {}).get("preferred_native_child_cap", 4))
    plans = []
    desired = set()
    def add(path: Path, new: bytes, kind: str, meta: dict | None = None):
        regular_target(path)
        desired.add(str(path))
        original_metadata = metadata(path)
        old = read(path)
        if metadata(path) != original_metadata:
            raise InstallError(f"Concurrent metadata edit while planning: {path}")
        if kind == "owned" and old is not None:
            prior = owned.get(str(path))
            if not prior or digest(old) != prior["after_sha256"]:
                raise InstallError(f"Existing or manually edited file conflicts: {path}")
        if old == new:
            return
        plans.append({"path": path, "before": old, "after": new, "kind": kind,
                      "meta": meta or {}, "_metadata": original_metadata})
    instruction = selected_instructions(home)
    text = decode(read(instruction))
    template = decode(required_read(package / "adapters/codex/bootstrap.md"))
    eol = "\r\n" if "\r\n" in text else "\n"
    body = template.replace("{{VERSION}}", VERSION).replace("{{MACHINE_POINTER}}", str(home / "harness-machine.json")).strip()
    block = START + eol + body.replace("\n", eol) + eol + END
    previous = managed_block(text)
    if previous and previous != block:
        prior = owned.get(str(instruction))
        if not prior:
            raise InstallError("Existing unmanaged harness block; inspect before adopting")
        prior_installed = decode(required_read(Path(prior["installed_copy"])))
        if managed_block(prior_installed) != previous:
            raise InstallError("Managed instruction block was edited; preserve it and resolve conflict")
    new_text = replace_block(text, block)
    config_path = home / "config.toml"
    old_config = decode(read(config_path))
    config, config_meta = patch_config(old_config, preferred_cap, max_child_threads is not None)
    parsed = tomllib.loads(config)
    limit = parsed.get("project_doc_max_bytes", 32768)
    if not isinstance(limit, int) or len(new_text.encode("utf-8")) > limit:
        raise InstallError("Global instructions exceed configured project_doc_max_bytes")
    add(instruction, encode_like(new_text, read(instruction)), "instructions", {"block_before": previous, "block_after": block})
    add(config_path, encode_like(config, read(config_path)), "config", config_meta)
    pointer = {"schema_version": 1, "policy_version": VERSION, "package_root": str(package), "machine_overlay": str(overlay), "installation_state": str(state)}
    add(home / "harness-machine.json", canonical(pointer), "owned")
    for role in ("delivery_owner", "delivery_helper", "delivery_reviewer"):
        source = decode(required_read(package / f"adapters/codex/agents/{role}.toml"))
        source = source.replace("{{POLICY_PATH}}", (package / "policies/global.md").as_posix()).replace("{{MACHINE_POINTER}}", (home / "harness-machine.json").as_posix())
        role_data = tomllib.loads(source)
        if not all(isinstance(role_data.get(k), str) and role_data[k] for k in ("name", "description", "developer_instructions")):
            raise InstallError(f"Invalid native role {role}")
        add(home / f"agents/{role}.toml", source.encode("utf-8"), "owned")
    skill = decode(required_read(package / "adapters/codex/SKILL.md")).replace("{{PACKAGE_ROOT}}", package.as_posix()).replace("{{MACHINE_POINTER}}", (home / "harness-machine.json").as_posix()).replace("{{VERSION}}", VERSION)
    add(skills / "agent-harness/SKILL.md", skill.encode("utf-8"), "owned")
    # Bundle only first-party, namespaced skills. Every installed resource joins
    # the existing conflict checks, backups, integrity checks and rollback.
    for folder in sorted((package / "skills").iterdir()):
        if folder.is_symlink() or not folder.is_dir() or not re.fullmatch(r"harness-[a-z0-9]+(?:-[a-z0-9]+)*", folder.name):
            raise InstallError(f"Invalid bundled skill directory: {folder.name}")
        if not (folder / "SKILL.md").is_file():
            raise InstallError(f"Bundled skill lacks SKILL.md: {folder.name}")
        for source in sorted(folder.rglob("*")):
            if source.is_symlink():
                raise InstallError(f"Symlink in bundled skill: {source}")
            if not source.is_file() or "__pycache__" in source.parts or source.suffix in {".pyc", ".pyo"}:
                continue
            data = required_read(source)
            if source.suffix == ".md":
                data = data.replace(b"{{PACKAGE_ROOT}}", package.as_posix().encode("utf-8")).replace(b"{{MACHINE_POINTER}}", (home / "harness-machine.json").as_posix().encode("utf-8")).replace(b"{{VERSION}}", VERSION.encode("utf-8"))
            add(skills / folder.name / source.relative_to(folder), data, "owned")
    # Do not silently leave obsolete auto-discovered skills active after an
    # upgrade, and do not broaden this installer into an automatic deleter.
    for path, prior in owned.items():
        old_path = Path(path)
        if prior["kind"] == "owned" and old_path.is_relative_to(skills) and path not in desired and old_path.exists():
            raise InstallError(f"Previously installed skill resource is absent from this package; inspect and uninstall the old version before migration: {old_path}")
    if existing_machine is None:
        machine = {"schema_version": 1, "package_root": str(package), "codex_home": str(home), "run_store": str(state / "runs"), "installation_state": str(state), "preferred_native_child_cap": preferred_cap, "platform": platform.system(), "shell": "PowerShell" if os.name == "nt" else "user shell", "capabilities": {}, "native_slot_semantics": "unknown_until_observed", "arch_linux": "unverified"}
        add(overlay, canonical(machine), "owned")
    else:
        # This is a machine-local document, so preserve all user capability/resource
        # additions and update only our explicitly managed fields.
        machine = dict(existing_machine)
        fields = {"installation_state": str(state), "package_root": str(package), "codex_home": str(home)}
        if max_child_threads is not None or "preferred_native_child_cap" not in machine:
            fields["preferred_native_child_cap"] = preferred_cap
        edits = {}
        for key, value in fields.items():
            if key not in machine or machine[key] != value:
                edits[key] = {"before_present": key in machine, "before": machine.get(key), "after": value}
                machine[key] = value
        if edits:
            add(overlay, canonical(machine), "json_fields", {"keys": edits})
    return plans


def apply_plan(plans: list[dict], state: Path, package: Path) -> dict:
    regular_target(state / "active.json")
    previous = active_manifest(state)
    previous_ref = read(state / "active.json")
    pointer_plan = {"path": state / "active.json", "before": previous_ref, "after": b""}
    if not plans:
        if previous is None:
            raise InstallError("Cannot report unchanged without an active installation manifest")
        return {"status": "unchanged", "manifest": (state / "active.json").as_posix(), "policy_version": VERSION}
    preflight_metadata([*plans, pointer_plan])
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    transaction = state / "installs" / stamp
    transaction.mkdir(parents=True)
    files = []
    for i, p in enumerate(plans):
        regular_target(p["path"])
        if read(p["path"]) != p["before"]:
            raise InstallError(f"Concurrent edit before installation: {p['path']}")
        backup = transaction / f"{i:02d}.before"
        installed = transaction / f"{i:02d}.installed"
        if p["before"] is not None:
            atomic(backup, p["before"])
        atomic(installed, p["after"])
        files.append({"path": str(p["path"]), "before_sha256": digest(p["before"]) if p["before"] is not None else None, "after_sha256": digest(p["after"]), "backup": str(backup) if p["before"] is not None else None, "installed_copy": str(installed), "kind": p["kind"], "meta": p["meta"], "metadata_before": p["_metadata"]["security"] if p["_metadata"] else None})
    manifest = {"schema_version": 1, "policy_version": VERSION, "package_root": str(package), "install_id": stamp, "created": dt.datetime.now(dt.timezone.utc).isoformat(), "files": files, "previous_manifest": json.loads(previous_ref) if previous_ref else None, "status": "prepared"}
    manifest_path = transaction / "manifest.json"
    atomic(manifest_path, canonical(manifest))
    written = []
    new_ref = None
    try:
        for i, p in enumerate(plans):
            regular_target(p["path"])
            check_planned_metadata(p)
            if read(p["path"]) != p["before"]:
                raise InstallError(f"Concurrent edit while installing: {p['path']}")
            # Track even an atomic implementation which commits then raises.
            written.append(p)
            publish_target(p)
            files[i]["metadata_after"] = p["_applied_metadata"]["security"]
        manifest["status"] = "written_pending_native_verification"
        atomic(manifest_path, canonical(manifest))
        if read(state / "active.json") != previous_ref:
            raise InstallError("Concurrent edit to the active installation manifest")
        check_planned_metadata(pointer_plan)
        new_ref = canonical({"manifest": str(manifest_path), "sha256": digest(required_read(manifest_path))})
        pointer_plan["after"] = new_ref
        publish_target(pointer_plan)
    except BaseException as original_error:
        recovery_errors = []
        # The pointer is part of the transaction, too. Never overwrite a ref
        # which an external writer changed to something other than our value.
        try:
            regular_target(state / "active.json")
            if new_ref is not None and read(state / "active.json") == new_ref:
                if not owns_applied_metadata(pointer_plan):
                    recovery_errors.append("active manifest pointer (concurrent metadata edit preserved)")
                else:
                    restore_target(state / "active.json", previous_ref, pointer_plan["_metadata"])
        except (OSError, InstallError):
            recovery_errors.append("active manifest pointer")
        for p in reversed(written):
            try:
                regular_target(p["path"])
                current = read(p["path"])
                if current == p["after"]:
                    if not owns_applied_metadata(p):
                        recovery_errors.append(str(p["path"]) + " (concurrent metadata edit preserved)")
                    else:
                        restore_target(p["path"], p["before"], p["_metadata"])
                elif current != p["before"]:
                    recovery_errors.append(str(p["path"]) + " (concurrent edit preserved)")
            except (OSError, InstallError):
                recovery_errors.append(str(p["path"]))
        manifest["status"] = "failed_recovery_required" if recovery_errors else "failed_restored_unchanged_targets"
        try:
            atomic(manifest_path, canonical(manifest))
        except (OSError, InstallError):
            recovery_errors.append("transaction status")
        if recovery_errors:
            raise InstallError(
                f"Installation failed; inspect {manifest_path} before recovery. "
                "Unrestored or unrecorded targets: " + ", ".join(recovery_errors)
            ) from original_error
        raise
    return {"status": manifest["status"], "manifest": str(manifest_path), "files_changed": len(files), "policy_version": VERSION}


def rollback_plan(manifest: dict, current_files: dict | None = None) -> list[dict]:
    plans = []
    for p in reversed(manifest["files"]):
        path = Path(p["path"])
        regular_target(path)
        original_metadata = metadata(path)
        current = current_files[str(path)] if current_files is not None and str(path) in current_files else read(path)
        if metadata(path) != original_metadata:
            raise InstallError(f"Concurrent metadata edit while planning rollback: {path}")
        installed = required_read(Path(p["installed_copy"]))
        before = required_read(Path(p["backup"])) if p["backup"] else None
        if digest(installed) != p["after_sha256"] or (before is not None and digest(before) != p["before_sha256"]):
            raise InstallError(f"Backup integrity error: {path}")
        if current == before:
            continue
        if current == installed:
            new = before
        elif p["kind"] == "instructions" and current is not None:
            text = decode(current)
            if managed_block(text) != p["meta"]["block_after"]:
                raise InstallError(f"Rollback conflict in managed instructions: {path}")
            new_text = text.replace(p["meta"]["block_after"], p["meta"]["block_before"] or "", 1)
            new = encode_like(new_text, current)
        elif p["kind"] == "config" and current is not None:
            new = encode_like(revert_config(decode(before), decode(installed), decode(current), p["meta"]), current)
        elif p["kind"] == "json_fields" and current is not None:
            value = json.loads(decode(current))
            for key, edit in p["meta"]["keys"].items():
                if key not in value or value[key] != edit["after"]:
                    raise InstallError(f"Rollback conflict in machine overlay field {key}; user edit preserved")
                if edit["before_present"]:
                    value[key] = edit["before"]
                else:
                    value.pop(key)
            new = canonical(value)
        else:
            raise InstallError(f"Rollback conflict: manually modified {path}; no file overwritten")
        if new is None and original_metadata is not None:
            security = original_metadata["security"]
            known = p.get("metadata_after")
            if ((known is not None and security != known)
                    or (known is None and not ordinary_legacy_metadata(security))):
                raise InstallError(f"Rollback would remove user metadata/ACLs; file preserved: {path}")
        plans.append({"path": path, "before": current, "after": new, "kind": p["kind"], "_metadata": original_metadata})
    return plans


def apply_rollback(plans: list[dict], state: Path, manifest: dict, source_ref: dict, command: str, active_before: bytes | None) -> dict:
    """Journal rollback writes and compensate only bytes still owned by this attempt.

    The installer lock coordinates harness writers. Per-target guards detect
    intervening external edits; they are not filesystem CAS against an editor.
    """
    pointer = state / "active.json"
    regular_target(pointer)
    if read(pointer) != active_before:
        raise InstallError("Concurrent edit to the active installation reference before rollback")
    previous = None if command == "uninstall" else manifest.get("previous_manifest")
    next_ref = canonical(previous) if previous else None
    changes = [dict(p) for p in plans]
    changes.append({"path": pointer, "before": active_before, "after": next_ref, "kind": "active_pointer"})
    preflight_metadata(changes)
    # Reject unsupported object types and stale plans before changing any target.
    for p in changes:
        regular_target(p["path"])
        if read(p["path"]) != p["before"]:
            raise InstallError(f"Concurrent edit before rollback: {p['path']}")
    if not plans and active_before == next_ref:
        return {"status": "rolled_back", "files_changed": 0, "preserved_user_edits": True}

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    transaction = state / "rollbacks" / stamp
    transaction.mkdir(parents=True)
    rows = []
    for i, p in enumerate(changes):
        row = {"path": str(p["path"]), "kind": p.get("kind", "target"), "progress": "pending",
               "before_metadata": p["_metadata"]["security"] if p["_metadata"] else None}
        for label in ("before", "after"):
            data = p[label]
            copy = transaction / f"{i:02d}.{label}"
            if data is not None:
                atomic(copy, data)
            row[label + "_copy"] = str(copy) if data is not None else None
            row[label + "_sha256"] = digest(data) if data is not None else None
        rows.append(row)
    journal = {"schema_version": 1, "operation": command, "status": "prepared", "source_manifest": source_ref, "files": rows}
    journal_path = transaction / "manifest.json"
    atomic(journal_path, canonical(journal))

    attempted = []
    try:
        journal["status"] = "applying"
        for i, p in enumerate(changes):
            rows[i]["progress"] = "started"
            atomic(journal_path, canonical(journal))
            regular_target(p["path"])
            try:
                check_planned_metadata(p)
            except InstallError:
                rows[i]["progress"] = "concurrent_edit_preserved"
                raise
            if read(p["path"]) != p["before"]:
                rows[i]["progress"] = "concurrent_edit_preserved"
                raise InstallError(f"Concurrent edit while rolling back; user edit preserved: {p['path']}")
            # A write/unlink may commit and then raise, so include it in recovery.
            attempted.append(i)
            if p["before"] != p["after"]:
                publish_target(p)
            rows[i]["progress"] = "applied"
            atomic(journal_path, canonical(journal))
        journal["status"] = "rolled_back"
        atomic(journal_path, canonical(journal))
    except BaseException as original_error:
        recovery_errors = []
        for i in reversed(attempted):
            p = changes[i]
            try:
                regular_target(p["path"])
                current = read(p["path"])
                if current == p["before"]:
                    rows[i]["progress"] = "restored"
                elif current == p["after"]:
                    if owns_applied_metadata(p):
                        restore_target(p["path"], p["before"], p["_metadata"])
                        rows[i]["progress"] = "restored"
                    else:
                        rows[i]["progress"] = "concurrent_edit_preserved"
                        recovery_errors.append({"path": str(p["path"]), "reason": "concurrent_metadata_edit_preserved"})
                else:
                    rows[i]["progress"] = "concurrent_edit_preserved"
                    recovery_errors.append({"path": str(p["path"]), "reason": "concurrent_edit_preserved"})
            except (OSError, InstallError) as error:
                rows[i]["progress"] = "recovery_required"
                recovery_errors.append({"path": str(p["path"]), "reason": type(error).__name__})
        journal["status"] = "rollback_failed_recovery_required" if recovery_errors else "rollback_failed_compensated"
        journal["error_type"] = type(original_error).__name__
        journal["recovery_errors"] = recovery_errors
        try:
            atomic(journal_path, canonical(journal))
        except (OSError, InstallError) as error:
            recovery_errors.append({"path": str(journal_path), "reason": "recovery_progress_unrecorded", "error_type": type(error).__name__})
        report = {
            "status": "rollback_failed_recovery_required" if recovery_errors else "rollback_failed_compensated",
            "error": str(original_error), "journal": str(journal_path), "source_manifest": source_ref,
            "files_attempted": sum(changes[i].get("kind") != "active_pointer" for i in attempted),
            "recovery_required": bool(recovery_errors), "recovery_errors": recovery_errors,
            "recovery": "Inspect the journal and reconcile listed conflicts before retrying the original command with --manifest and --state-dir. Backups and metadata describe this attempt's pre-rollback files.",
        }
        if not isinstance(original_error, Exception):
            raise
        raise RollbackError(report) from original_error
    return {"status": "rolled_back", "files_changed": len(plans), "preserved_user_edits": True, "journal": str(journal_path)}


def verify(package: Path, home: Path, skills: Path, state: Path, native: bool = False, cwd: Path | None = None) -> dict:
    errors = []
    warnings = []
    pointer = home / "harness-machine.json"
    try:
        active = active_manifest(state)
        if active is None:
            errors.append("active installation manifest missing; installed files alone are not a verified installation")
        else:
            for manifest in manifest_chain(active):
                for entry in manifest["files"]:
                    installed = required_read(Path(entry["installed_copy"]))
                    if digest(installed) != entry["after_sha256"]:
                        raise InstallError("Installed backup integrity error")
                    if entry["backup"] and digest(required_read(Path(entry["backup"]))) != entry["before_sha256"]:
                        raise InstallError("Original backup integrity error")
    except (OSError, KeyError, ValueError) as ex:
        errors.append("Installation state invalid: " + str(ex))
    try:
        data = json.loads(decode(required_read(pointer)))
        if Path(data["package_root"]).resolve() != package or data["policy_version"] != VERSION:
            errors.append("machine pointer references another package/version")
        if not Path(data["machine_overlay"]).is_file():
            errors.append("machine overlay missing")
    except (OSError, KeyError, ValueError) as ex:
        errors.append(f"machine pointer invalid: {type(ex).__name__}")
    try:
        text = decode(read(selected_instructions(home)))
        block = managed_block(text)
        if not block or f"{VERSION}" not in block:
            errors.append("active global policy block missing/version mismatch")
        parsed = tomllib.loads(decode(read(home / "config.toml")))
        agents = parsed.get("agents", {})
        if agents.get("enabled") is not True:
            errors.append("agents.enabled is not true")
        cap = agents.get("max_concurrent_threads_per_session")
        try:
            expected_cap = json.loads(decode(required_read(Path(data["machine_overlay"])))).get("preferred_native_child_cap", 4)
            positive_cap(expected_cap)
        except (OSError, KeyError, ValueError, UnboundLocalError):
            expected_cap = 4
        if not isinstance(cap, int) or isinstance(cap, bool) or not 1 <= cap <= expected_cap:
            errors.append("agent cap missing or invalid")
        limit = parsed.get("project_doc_max_bytes", 32768)
        if len(text.encode("utf-8")) > limit:
            errors.append("active instructions exceed byte limit")
    except (ValueError, TypeError) as ex:
        agents = {}
        errors.append(str(ex))
    for name in ("delivery_owner", "delivery_helper", "delivery_reviewer"):
        try:
            role = tomllib.loads(decode(required_read(home / f"agents/{name}.toml")))
            if role["name"] != name or not role["developer_instructions"] or not role["description"]:
                errors.append(f"invalid role {name}")
            if not str(package / "policies/global.md").replace("\\", "/") in role["developer_instructions"]:
                errors.append(f"role {name} lacks accessible policy reference")
        except (OSError, KeyError, ValueError):
            errors.append(f"role {name} missing or invalid")
    if not (skills / "agent-harness/SKILL.md").is_file():
        errors.append("conditional skill missing")
    try:
        overlay = Path(json.loads(decode(required_read(pointer)))["machine_overlay"])
        pending = plan(package, home, skills, state, overlay)
        if pending:
            errors.append("Installed targets differ from canonical package: " + ", ".join(str(p["path"]) for p in pending))
    except (OSError, KeyError, ValueError) as ex:
        errors.append("Installation integrity check failed: " + str(ex))
    native_result = {"status": "not_run", "reason": "Use --native in a new process; files alone do not prove client loading"}
    if native:
        executable = shutil.which("codex")
        if not executable:
            native_result = {"status": "unavailable", "reason": "codex executable missing"}
            errors.append("native validation unavailable")
        else:
            env = {**os.environ, "CODEX_HOME": str(home)}
            results = []
            for argv in ([executable, "app-server", "--strict-config", "--print-startup-config"], [executable, "-C", str(cwd or package), "debug", "prompt-input", "Ответь кратко: сколько будет 2 + 2?"]):
                try:
                    completed = subprocess.run(argv, capture_output=True, timeout=60, env=env)
                    output = completed.stdout.decode("utf-8", errors="replace")
                    row = {"argv": argv, "exit_code": completed.returncode, "stdout_sha256": digest(completed.stdout), "stderr_sha256": digest(completed.stderr)}
                    if "prompt-input" in argv:
                        row["policy_in_model_input"] = START in output and VERSION in output
                        if not row["policy_in_model_input"]:
                            errors.append("native model input lacks global policy marker")
                    if completed.returncode:
                        errors.append("native command failed: " + " ".join(argv[1:]))
                    results.append(row)
                except subprocess.TimeoutExpired:
                    errors.append("native validation timeout")
                    results.append({"argv": argv, "status": "timeout"})
            native_result = {"status": "observed" if not errors else "failed", "checks": results, "scope": "new native CLI process; desktop/subagent behavior requires separate probe"}
    if not native:
        warnings.append("Native loading and actual agent behavior not verified by this invocation")
    return {"status": "failed" if errors else "structural_verified" if not native else "native_input_verified", "policy_version": VERSION, "errors": errors, "warnings": warnings, "active_instruction_file": str(selected_instructions(home)), "file_config_agents": agents, "effective_runtime_agents": "unknown_until_client_probe", "native": native_result, "hosted_work": "not_configured_by_local_files"}


def doctor(package: Path, home: Path, skills: Path, state: Path) -> dict:
    from .diagnostics import model_configuration

    parsed = tomllib.loads(decode(read(home / "config.toml")))
    names = ["codex", "git", "python", "python3", "node", "npm", "bash", "go"]
    agent_data = parsed.get("agents", {})
    agent_values = {
        k: agent_data[k] for k in ("enabled", CAP_KEY, "max_threads", "interrupt_message")
        if isinstance(agent_data, dict) and k in agent_data and isinstance(agent_data[k], (bool, int))
    }
    windows = parsed.get("windows", {})
    windows_sandbox = windows.get("sandbox", "not_explicit") if isinstance(windows, dict) else "invalid_type"
    return {
        "schema_version": 1, "policy_version": VERSION,
        "platform": platform.platform(), "python": sys.version.split()[0],
        "codex_home": str(home),
        "codex_home_source": "explicit/CLI argument" if home != Path.home() / ".codex" else "default ~/.codex",
        "active_instruction_file": str(selected_instructions(home)),
        "override_nonempty": selected_instructions(home).name == "AGENTS.override.md",
        "config_syntax": "valid", "agents_file_config": agent_values,
        "security_config": {k: parsed[k] for k in ("approval_policy", "sandbox_mode") if k in parsed and isinstance(parsed[k], str)},
        "windows_sandbox": windows_sandbox if isinstance(windows_sandbox, str) else "invalid_type",
        "effective_runtime_security": "must_be_observed_in_client",
        "native_roles": sorted(p.name for p in (home / "agents").glob("*.toml")),
        "skills_count": len(list(skills.glob("*/SKILL.md"))),
        "executables": {name: shutil.which(name) for name in names},
        "package_root": str(package), "state_dir": str(state),
        "hosted_work": "local_codex_config_not_applicable", "arch_linux": "unverified",
        "model_configuration": model_configuration(home, parsed),
    }


def installed_locations(package: Path, home: Path) -> tuple[Path, Path]:
    """Discover installed private state without assuming it lives beside the package."""
    pointer_path = home / "harness-machine.json"
    if pointer_path.exists():
        pointer = json.loads(decode(required_read(pointer_path)))
        if pointer.get("schema_version") != 1:
            raise InstallError("Unsupported machine pointer schema")
        overlay = Path(pointer["machine_overlay"]).resolve()
        machine = json.loads(decode(required_read(overlay))) if overlay.exists() else {}
        state_value = pointer.get("installation_state") or machine.get("installation_state")
        if state_value:
            return Path(state_value).resolve(), overlay
        # Compatible discovery for the first 1.0.0 install, before the state field
        # was added. Accept the overlay parent only when its manifest is present.
        if (overlay.parent / "active.json").is_file():
            return overlay.parent, overlay
    state = (package.parent / "agent-harness-local").resolve()
    return state, state / "machine.json"


def dispatch(command: str, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"harness {command}")
    parser.add_argument("--package-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")))
    parser.add_argument("--skills-home", type=Path, default=Path.home() / ".agents/skills")
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--machine-overlay", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--native", action="store_true")
    parser.add_argument("--cwd", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--max-child-threads", type=int, help="Explicitly authorize a positive native child cap, persisted in machine overlay; unrelated to four logical lanes")
    args = parser.parse_args(argv)
    package, home, skills = args.package_root.resolve(), args.codex_home.resolve(), args.skills_home.resolve()
    try:
        if args.max_child_threads is not None:
            positive_cap(args.max_child_threads)
            if command != "install":
                raise InstallError("--max-child-threads is valid only for install")
        discovered_state, discovered_overlay = installed_locations(package, home)
        state = (args.state_dir or discovered_state).resolve()
        overlay = (args.machine_overlay or (state / "machine.json" if args.state_dir else discovered_overlay)).resolve()
        if command == "doctor":
            result = doctor(package, home, skills, state)
        elif command == "verify":
            result = verify(package, home, skills, state, args.native, args.cwd)
        elif command == "install":
            with lock(state):
                planned = plan(package, home, skills, state, overlay, args.max_child_threads)
                preview = {"status": "dry_run", "policy_version": VERSION, "changes": [{"path": str(p["path"]), "before_sha256": digest(p["before"]) if p["before"] is not None else None, "after_sha256": digest(p["after"]), "diff": redacted_diff(p["path"], p["before"], p["after"])} for p in planned]}
                result = apply_plan(planned, state, package) if args.apply else preview
        elif command in ("rollback", "uninstall"):
            with lock(state):
                regular_target(state / "active.json")
                active_before = read(state / "active.json")
                if args.manifest:
                    source_path = args.manifest.resolve()
                    source_data = required_read(source_path)
                    manifest = json.loads(source_data)
                    source_ref = {"manifest": str(source_path), "sha256": digest(source_data)}
                else:
                    manifest = active_manifest(state)
                    source_ref = json.loads(active_before) if manifest and active_before is not None else None
                if not manifest:
                    raise InstallError("No active installation manifest; nothing removed")
                if read(state / "active.json") != active_before:
                    raise InstallError("Concurrent edit to the active installation reference while planning rollback")
                planned = rollback_plan(manifest)
                if command == "uninstall":
                    virtual = {}
                    initial = {}
                    initial_metadata = {}
                    for previous in manifest_chain(manifest):
                        for p in rollback_plan(previous, virtual):
                            key = str(p["path"])
                            # Keep the exact bytes used to derive this rollback;
                            # a fresh read could silently adopt an intervening edit.
                            initial.setdefault(key, p["before"])
                            initial_metadata.setdefault(key, p["_metadata"])
                            virtual[key] = p["after"]
                    planned = [{"path": Path(p), "before": initial[p], "after": value, "_metadata": initial_metadata[p]} for p, value in virtual.items() if initial[p] != value]
                result = {"status": "rollback_dry_run", "changes": [{"path": str(p["path"]), "remove": p["after"] is None, "diff": redacted_diff(p["path"], p["before"], p["after"] or b"")} for p in planned]}
                if args.apply:
                    result = apply_rollback(planned, state, manifest, source_ref, command, active_before)
        else:
            raise InstallError(f"Unknown install command {command}")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get("status") == "failed" else 0
    except (InstallError, OSError, ValueError, KeyError) as ex:
        print(json.dumps(ex.report if isinstance(ex, RollbackError) else {"status": "blocked", "error": str(ex)}, ensure_ascii=False))
        return 2
