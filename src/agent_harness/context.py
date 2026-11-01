"""Bounded, explicit source packets. Local files only; never an acceptance gate."""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

from .core import HarnessError, atomic_json, canonical, digest, now

MAX_PACKET_BYTES = 4 * 1024 * 1024
DENIED_PARTS = {".git", ".ssh", ".aws", "node_modules", ".venv", "__pycache__"}
DENIED_NAMES = {"auth.json", "credentials", "credentials.json", "cookies.json", "id_rsa", "id_ed25519"}
SECRET = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,})\b|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|https?://[^\s/@:]+:[^\s/@]+@")


def _root(value):
    path = Path(value).resolve()
    if not path.is_dir():
        raise HarnessError("Source root must be an existing directory")
    return path


def _path(root, value):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise HarnessError("Use explicit relative POSIX file paths")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts) or Path(value).is_absolute():
        raise HarnessError("Packet path must remain inside source root")
    if any(part.casefold() in DENIED_PARTS for part in parts) or parts[-1].casefold() in DENIED_NAMES or parts[-1].casefold().startswith(".env") or Path(value).suffix.casefold() in {".pem", ".key", ".p12", ".pfx"}:
        raise HarnessError("Packet excludes credential and generated paths: " + value)
    path = root
    for part in parts:
        path /= part
        if path.is_symlink():
            raise HarnessError("Packet does not follow symlinks: " + value)
        try:
            observed = path.lstat()
        except FileNotFoundError:
            continue
        if getattr(observed, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise HarnessError("Packet does not follow reparse points: " + value)
    if not path.resolve().is_relative_to(root):
        raise HarnessError("Packet path escaped source root")
    return path


def _stamp(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


@contextmanager
def _open_source(root, name):
    """Anchor POSIX reads to directory descriptors, refusing every symlink hop."""
    path = _path(root, name)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    if os.open in os.supports_dir_fd and hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW"):
        with ExitStack() as stack:
            directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            directory = os.open(root, directory_flags)
            stack.callback(os.close, directory)
            for part in name.split("/")[:-1]:
                directory = os.open(part, directory_flags, dir_fd=directory)
                stack.callback(os.close, directory)
            descriptor = os.open(name.split("/")[-1], flags, dir_fd=directory)
            with os.fdopen(descriptor, "rb") as source:
                _path(root, name)
                yield source
    else:
        # On platforms without openat, validate all components before/after open
        # and compare file identities. These controls are not an OS sandbox.
        with os.fdopen(os.open(path, flags), "rb") as source:
            _path(root, name)
            yield source


def _read(path, limit, *, root, name):
    _path(root, name)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise HarnessError("Packet input must be a regular file")
    if before.st_size > limit:
        return None, before.st_size
    with _open_source(root, name) as source:
        opened = os.fstat(source.fileno())
        if _stamp(before) != _stamp(opened):
            raise HarnessError("Source changed before packet read")
        data = source.read(limit + 1)
        after = os.fstat(source.fileno())
    _path(root, name)
    if len(data) > limit or _stamp(before) != _stamp(opened) or _stamp(opened) != _stamp(after) or _stamp(after) != _stamp(path.stat()):
        raise HarnessError("Source changed while reading packet; retry from current inputs")
    return data, len(data)


def make_packet(root, includes, *, max_files=64, max_file_bytes=65536, max_bytes=262144):
    root = _root(root)
    if not (type(max_files) is int and 1 <= max_files <= 256 and type(max_file_bytes) is int and 1 <= max_file_bytes <= 262144 and type(max_bytes) is int and 1 <= max_bytes <= 1048576):
        raise HarnessError("Packet limits exceed supported bounds")
    if not includes or any(not isinstance(name, str) for name in includes):
        raise HarnessError("Select at least one explicit source file")
    names = list(dict.fromkeys(includes))
    if len(names) > max_files:
        raise HarnessError("Too many selected files; narrow the packet")
    files, omitted, used = [], [], 0
    for name in names:
        path = _path(root, name)
        data, size = _read(path, min(max_file_bytes, max_bytes - used), root=root, name=name)
        if data is None:
            omitted.append({"path": name, "size": size, "reason": "byte_limit"})
            continue
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            omitted.append({"path": name, "size": size, "reason": "not_utf8"})
            continue
        if "\x00" in content:
            omitted.append({"path": name, "size": size, "reason": "binary"})
            continue
        if SECRET.search(content):
            raise HarnessError("Possible credential in selected file; inspect locally: " + name)
        used += size
        files.append({"path": name, "size": size, "sha256": hashlib.sha256(data).hexdigest(), "content": content})
    result = {"schema_version": 1, "purpose": "source_context_only", "created_at": now(), "root_identity": digest(str(root)), "files": files, "omitted": omitted, "complete": not omitted, "source_bytes": used, "limits": {"max_files": max_files, "max_file_bytes": max_file_bytes, "max_bytes": max_bytes}}
    result["digest"] = digest(result)
    if len(canonical(result)) + 1 > MAX_PACKET_BYTES:
        raise HarnessError("Serialized packet exceeds input size limit; narrow the selected files")
    return result


def read_packet(path):
    with Path(path).open("rb") as source:
        data = source.read(MAX_PACKET_BYTES + 1)
    if len(data) > MAX_PACKET_BYTES:
        raise HarnessError("Packet exceeds input size limit")
    return json.loads(data.decode("utf-8"))


def verify_packet(root, packet):
    root = _root(root)
    if not isinstance(packet, dict) or packet.get("schema_version") != 1 or packet.get("purpose") != "source_context_only":
        raise HarnessError("Unsupported source packet")
    if packet.get("digest") != digest({key: value for key, value in packet.items() if key != "digest"}):
        raise HarnessError("Packet content integrity failed")
    if packet.get("root_identity") != digest(str(root)):
        raise HarnessError("Packet belongs to another source root; rebuild explicitly")
    files = packet.get("files")
    if not isinstance(files, list) or len(files) > 256:
        raise HarnessError("Invalid packet file list")
    errors, seen = [], set()
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("content"), str) or not isinstance(item.get("path"), str):
            raise HarnessError("Invalid packet file record")
        name = item["path"]
        if name in seen:
            raise HarnessError("Duplicate packet file")
        seen.add(name)
        content = item["content"].encode("utf-8")
        if len(content) > 262144 or item.get("size") != len(content) or item.get("sha256") != hashlib.sha256(content).hexdigest():
            raise HarnessError("Packet source content integrity failed")
        path = _path(root, name)
        try:
            actual, _ = _read(path, len(content), root=root, name=name)
            if actual is None or hashlib.sha256(actual).hexdigest() != item["sha256"]:
                errors.append({"path": name, "reason": "source_changed"})
        except OSError:
            errors.append({"path": name, "reason": "source_unavailable"})
    return {"status": "stale" if errors else "current", "files_checked": len(files), "complete": packet.get("complete") is True and not packet.get("omitted"), "errors": errors, "limitation": "Source context only; omitted files and task acceptance are not verified"}


def dispatch(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    pack = commands.add_parser("pack")
    pack.add_argument("--root", required=True)
    pack.add_argument("--include", required=True, action="append")
    pack.add_argument("--output", required=True)
    pack.add_argument("--max-files", type=int, default=64)
    pack.add_argument("--max-file-bytes", type=int, default=65536)
    pack.add_argument("--max-bytes", type=int, default=262144)
    verify = commands.add_parser("verify")
    verify.add_argument("--root", required=True)
    verify.add_argument("--packet", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "pack":
            output = Path(args.output).absolute()
            if output.resolve().is_relative_to(_root(args.root)):
                raise HarnessError("Write private packets outside the source root")
            packet = make_packet(args.root, args.include, max_files=args.max_files, max_file_bytes=args.max_file_bytes, max_bytes=args.max_bytes)
            # Exclusive creation preserves existing handoffs and evidence.
            atomic_json(output, packet, exclusive=True)
            result = {"path": str(output), "digest": packet["digest"], "files": len(packet["files"]), "source_bytes": packet["source_bytes"], "complete": packet["complete"], "omitted": packet["omitted"]}
        else:
            result = verify_packet(args.root, read_packet(args.packet))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get("status") == "stale" else 0
    except (HarnessError, ValueError, OSError, TypeError) as error:
        print(json.dumps({"error": str(error), "type": type(error).__name__}, ensure_ascii=False), file=sys.stderr)
        return 2
