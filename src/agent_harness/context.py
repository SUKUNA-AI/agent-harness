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
from .file_io import FileReadError, read_regular_file

MAX_PACKET_BYTES = 4 * 1024 * 1024
MAX_EXCERPT_FILE_BYTES = 16 * 1024 * 1024
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


def _stamp(value, *, cross_api=False):
    fields = (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
    # Windows CPython can expose birth time through stat().st_ctime and change
    # time through fstat().st_ctime. Keep ctime checks within the same API.
    # https://github.com/python/cpython/issues/157671
    return fields if cross_api else (*fields, value.st_ctime_ns)


@contextmanager
def _open_source(root, name):
    """Anchor POSIX reads to directory descriptors, refusing every symlink hop."""
    path = _path(root, name)
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_NONBLOCK", 0))
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
        if not stat.S_ISREG(opened.st_mode):
            raise HarnessError("Packet input must remain a regular file")
        if _stamp(before, cross_api=True) != _stamp(opened, cross_api=True):
            raise HarnessError("Source changed before packet read")
        data = source.read(limit + 1)
        after = os.fstat(source.fileno())
    _path(root, name)
    last = path.stat()
    if (len(data) > limit or _stamp(opened) != _stamp(after)
            or _stamp(before) != _stamp(last)
            or _stamp(after, cross_api=True) != _stamp(last, cross_api=True)):
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
    try:
        data = read_regular_file(path, max_bytes=MAX_PACKET_BYTES)
    except FileReadError as error:
        raise HarnessError(str(error)) from error
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


def excerpt_source(root, name, *, start_line=1, lines=80, offset=None,
                   max_bytes=8192, expect_sha256=None):
    """Read a bounded view, retaining a hash of the entire observed UTF-8 file.

    The byte budget includes JSON escaping, metadata and a final LF or CRLF.
    ``next_offset`` is a UTF-8 boundary for continuing an overlong line. A caller
    can supply the returned hash on the next read to refuse a changed source.
    This is a local read, not a persistent index or an acceptance check.
    """
    if (type(start_line) is not int or not 1 <= start_line <= MAX_EXCERPT_FILE_BYTES + 1
            or type(lines) is not int or not 1 <= lines <= 2000
            or type(max_bytes) is not int or not 1024 <= max_bytes <= 65536
            or (offset is not None and (type(offset) is not int or offset < 0))):
        raise HarnessError("Excerpt requires positive line bounds, 1..2000 lines and 1024..65536 output bytes")
    if offset is not None and start_line != 1:
        raise HarnessError("Select start_line or offset, not both")
    if expect_sha256 is not None and (not isinstance(expect_sha256, str)
                                     or not re.fullmatch(r"[a-f0-9]{64}", expect_sha256)):
        raise HarnessError("Expected source hash must be a lowercase SHA-256")
    root = _root(root)
    path = _path(root, name)
    data, size = _read(path, MAX_EXCERPT_FILE_BYTES, root=root, name=name)
    if data is None:
        raise HarnessError("Excerpt input exceeds 16 MiB; narrow or rotate the source locally")
    sha = hashlib.sha256(data).hexdigest()
    if expect_sha256 is not None and sha != expect_sha256:
        raise HarnessError("Source hash changed; locate the current passage before continuing")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise HarnessError("Excerpt requires UTF-8 text") from None
    if "\x00" in text:
        raise HarnessError("Excerpt does not read binary files")
    if SECRET.search(text):
        raise HarnessError("Possible credential in selected file; inspect locally: " + name)
    total_lines = data.count(b"\n") + int(bool(data) and not data.endswith(b"\n"))
    if offset is None:
        if start_line > total_lines + 1:
            raise HarnessError("Requested line is beyond the source")
        begin = 0
        for _ in range(start_line - 1):
            newline = data.find(b"\n", begin)
            begin = len(data) if newline < 0 else newline + 1
    else:
        begin = offset
        if begin > len(data) or (begin < len(data) and data[begin] & 0xC0 == 0x80):
            raise HarnessError("Offset must be a UTF-8 boundary within the source")
        start_line = data.count(b"\n", 0, begin) + 1
    requested_end = begin
    for _ in range(lines):
        newline = data.find(b"\n", requested_end)
        requested_end = len(data) if newline < 0 else newline + 1
        if requested_end == len(data):
            break

    def result_for(length):
        # The whole file was decoded strictly above. Only a final partial UTF-8
        # codepoint may be omitted here; returned offsets always reflect bytes
        # actually delivered, so repeated reads never skip or replace content.
        content = data[begin:begin + length].decode("utf-8", errors="ignore")
        end = begin + len(content.encode("utf-8"))
        return {"purpose": "context_excerpt_only", "path": name,
                "root_identity": digest(str(root)), "sha256": sha,
                "source_bytes": size, "total_lines": total_lines,
                "start_line": start_line, "start_offset": begin, "end_offset": end,
                "next_offset": end if end < len(data) else None,
                "selection_complete": end == requested_end,
                "file_complete": begin == 0 and end == len(data),
                "truncated": end < requested_end, "max_output_bytes": max_bytes,
                "content": content}

    def fits(value):
        # Text stdout may translate print's newline to CRLF on Windows.
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 2 <= max_bytes

    if not fits(result_for(0)):
        raise HarnessError("Excerpt metadata exceeds output budget; increase max_bytes")
    low, high = 0, min(requested_end - begin, max_bytes)
    while low < high:
        middle = (low + high + 1) // 2
        if fits(result_for(middle)):
            low = middle
        else:
            high = middle - 1
    result = result_for(low)
    if result["end_offset"] == begin and requested_end > begin:
        raise HarnessError("Excerpt budget cannot include source content; increase max_bytes")
    return result


def dispatch(argv):
    if argv and argv[0] == "map":
        from .repo_map import dispatch as map_dispatch
        return map_dispatch(argv[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("map", help="Task-ranked Rust symbol map; use context map --help")
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
    read = commands.add_parser("read", help="Bounded UTF-8 source/log excerpt with full-file hash")
    read.add_argument("--root", required=True)
    read.add_argument("--file", required=True)
    position = read.add_mutually_exclusive_group()
    position.add_argument("--start-line", type=int, default=1)
    position.add_argument("--offset", type=int)
    read.add_argument("--lines", type=int, default=80)
    read.add_argument("--max-bytes", type=int, default=8192)
    read.add_argument("--expect-sha256")
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
        elif args.command == "verify":
            result = verify_packet(args.root, read_packet(args.packet))
        else:
            result = excerpt_source(args.root, args.file, start_line=args.start_line,
                                    lines=args.lines, offset=args.offset,
                                    max_bytes=args.max_bytes, expect_sha256=args.expect_sha256)
        if args.command == "read":
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get("status") == "stale" else 0
    except (HarnessError, ValueError, OSError, TypeError) as error:
        print(json.dumps({"error": str(error), "type": type(error).__name__}, ensure_ascii=False), file=sys.stderr)
        return 2
