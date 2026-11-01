"""Build and verify a deterministic portable ZIP from a clean Git candidate.

Tracked files come from Git blobs, not platform-specific checkout line endings.
Ignored native bundles are included only after version/source/hash/license checks.
This tool neither builds native binaries nor publishes anything.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tempfile
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]
MAX_FILES = 10000
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
PLATFORM = re.compile(r"(?:linux|windows|macos)-(?:x86_64|aarch64)")
DENIED_NAMES = {"auth.json", "credentials.json", "machine.json", "harness-machine.json", ".env"}
DENIED_PARTS = {".git", "__pycache__", ".venv", ".test-tmp", "private", "runs", "backups"}
SECRET_PATTERNS = (r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b",
                   r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}\b",
                   r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
                   r"https?://[^\s/@:]+:[^\s/@]+@")


class ReleaseError(ValueError):
    pass


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def git(root, *args, data=None):
    result = subprocess.run(["git", *args], cwd=root, input=data, capture_output=True,
                            shell=False, timeout=60)
    if result.returncode:
        raise ReleaseError("Git candidate inspection failed: " + result.stderr.decode("utf-8", errors="replace")[:1000])
    return result.stdout


def safe_name(value):
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or "\\" in value or ":" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or any(part in DENIED_PARTS for part in path.parts)
            or path.name in DENIED_NAMES or path.name.startswith("credentials")
            or path.name.startswith(".env.") and path.name != ".env.example"):
        raise ReleaseError("Private or unsafe distribution path: " + value)
    return path


def regular_read(path, limit=MAX_ARCHIVE_BYTES):
    path = Path(path).absolute()
    for parent in (*reversed(path.parents), path):
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ReleaseError("Linked distribution input is forbidden")
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise ReleaseError("Distribution input must be a bounded regular file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as source:
        opened = os.fstat(source.fileno())
        stamp = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
        if not stat.S_ISREG(opened.st_mode) or stamp(opened) != stamp(before):
            raise ReleaseError("Distribution input changed before reading")
        value = source.read(limit + 1)
        if len(value) > limit or stamp(os.fstat(source.fileno())) != stamp(before) or stamp(path.stat()) != stamp(before):
            raise ReleaseError("Distribution input changed while reading")
    return value


def tracked_blobs(root):
    """Canonical clean-HEAD content; no untracked files or checkout CRLF drift."""
    root = Path(root).resolve(strict=True)
    if git(root, "status", "--porcelain", "--untracked-files=all").strip():
        raise ReleaseError("Release source must be clean and committed; preserve edits and commit the checked candidate first")
    records, total = [], 0
    for raw in filter(None, git(root, "ls-tree", "-rlz", "--full-tree", "HEAD").split(b"\0")):
        header, name = raw.split(b"\t", 1)
        mode, kind, oid, size = header.split()
        name = name.decode("utf-8")
        safe_name(name)
        if kind != b"blob" or mode not in {b"100644", b"100755"}:
            raise ReleaseError("Submodules, symlinks, or special Git entries are not portable release inputs")
        total += int(size)
        records.append((name, oid.decode(), int(size), int(mode, 8)))
    if not records or len(records) > MAX_FILES or total > MAX_SOURCE_BYTES:
        raise ReleaseError("Tracked release input exceeds source file/byte limits")
    output = git(root, "cat-file", "--batch", data=("\n".join(row[1] for row in records) + "\n").encode())
    offset, files = 0, {}
    for name, oid, size, mode in records:
        end = output.find(b"\n", offset)
        if end < 0 or output[offset:end] != (oid + " blob " + str(size)).encode():
            raise ReleaseError("Unexpected Git object stream")
        offset = end + 1
        data = output[offset:offset + size]
        if len(data) != size or output[offset + size:offset + size + 1] != b"\n":
            raise ReleaseError("Truncated Git object stream")
        files[name] = (data, mode)
        offset += size + 1
    if offset != len(output):
        raise ReleaseError("Trailing Git object stream data")
    return files


def version_of(files):
    try:
        project = tomllib.loads(files["pyproject.toml"][0].decode("utf-8"))["project"]["version"]
        package = re.search(r'^__version__\s*=\s*["\']([^"\']+)', files["src/agent_harness/__init__.py"][0].decode("utf-8"), re.M)[1]
        installer = re.search(r'^VERSION\s*=\s*["\']([^"\']+)', files["src/agent_harness/installation.py"][0].decode("utf-8"), re.M)[1]
    except (KeyError, TypeError, ValueError, UnicodeError) as error:
        raise ReleaseError("Missing or invalid canonical release version") from error
    if project != package or project != installer or not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+.][A-Za-z0-9.-]+)?", project):
        raise ReleaseError("Package/project/installer release versions disagree")
    return project


def native_source_identity(files):
    entries = {name: sha(data) for name, (data, _) in sorted(files.items()) if name.startswith("native/")
               and (PurePosixPath(name).name in {"Cargo.toml", "Cargo.lock", "rust-toolchain.toml"}
                    or PurePosixPath(name).suffix in {".rs", ".scm"})}
    data = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    return sha(data), entries


def native_bundles(root, files, version, *, required=(), source_only=False):
    if source_only:
        if required:
            raise ReleaseError("--source-only cannot require native platforms")
        return {}, []
    native = Path(root) / "bin"
    found, additions = [], {}
    fingerprint, sources = native_source_identity(files)
    if native.is_dir():
        for directory in sorted(native.iterdir()):
            if not directory.is_dir() and not directory.is_symlink():
                continue
            key = directory.name
            if not PLATFORM.fullmatch(key):
                raise ReleaseError("Unreviewed native directory: " + key)
            if directory.is_symlink():
                raise ReleaseError("Linked native bundle is forbidden")
            try:
                info = json.loads(regular_read(directory / "build-info.json", 2 * 1024 * 1024))
                binary_name = "harness-native.exe" if key.startswith("windows-") else "harness-native"
                binary = regular_read(directory / binary_name, 128 * 1024 * 1024)
                if (info.get("schema_version") != 1 or info.get("protocol_version") != 1
                        or info.get("version") != version or info.get("platform") != key
                        or info.get("binary") != binary_name or info.get("source_digest") != fingerprint
                        or info.get("source_files") != sources or info.get("binary_sha256") != sha(binary)
                        or info.get("binary_bytes") != len(binary)):
                    raise ReleaseError("Native bundle version/source/binary identity mismatch: " + key)
                notices = directory / "licenses"
                index = json.loads(regular_read(notices / "index.json", 4 * 1024 * 1024))
                if index.get("schema_version") != 1 or not isinstance(index.get("dependencies"), list):
                    raise ReleaseError("Native dependency notice index invalid")
                for dependency in index["dependencies"]:
                    if not isinstance(dependency, dict) or not dependency.get("notices"):
                        raise ReleaseError("Native dependency notice missing")
                    for notice in dependency["notices"]:
                        safe_name(notice)
                        regular_read(notices / notice, 4 * 1024 * 1024)
                package_files = sorted(path for path in directory.rglob("*") if path.is_file() or path.is_symlink())
                for path in package_files:
                    internal = path.relative_to(directory)
                    if internal.parts[0] not in {binary_name, "build-info.json", "licenses"}:
                        raise ReleaseError("Unreviewed native bundle artifact: " + internal.as_posix())
                    relative = path.relative_to(root).as_posix()
                    safe_name(relative)
                    data = regular_read(path)
                    if path.name == "build-info.json":
                        # Compilation duration is operational telemetry, not an
                        # input or executable identity. Exclude it from packaging.
                        normalized = dict(info)
                        normalized.pop("build_seconds", None)
                        data = encoded(normalized)
                    additions[relative] = (data, 0o100755 if path.name == binary_name else 0o100644)
                found.append(key)
            except (OSError, ValueError, TypeError) as error:
                if isinstance(error, ReleaseError):
                    raise
                raise ReleaseError("Invalid native bundle: " + key + ": " + type(error).__name__) from error
    if not found:
        raise ReleaseError("No verified native bundle; build one or explicitly request --source-only")
    if not set(required) <= set(found):
        raise ReleaseError("Required native platforms missing: " + ", ".join(sorted(set(required) - set(found))))
    return additions, found


def _write_once(path, data):
    path = Path(path)
    if path.is_symlink():
        raise ReleaseError("Linked release output is forbidden")
    if path.exists():
        if regular_read(path) != data:
            raise ReleaseError("Release output already exists with different bytes")
    else:
        with path.open("xb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())


def build_release(root, output_dir, *, required_platforms=(), source_only=False):
    root, output_dir = Path(root).resolve(strict=True), Path(output_dir).absolute()
    files = tracked_blobs(root)
    for name, (data, _) in files.items():
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise ReleaseError("Unreviewed binary in tracked source: " + name) from error
        if any(re.search(pattern, text) for pattern in SECRET_PATTERNS):
            raise ReleaseError("Credential-shaped content in tracked source: " + name)
    version = version_of(files)
    additions, platforms = native_bundles(root, files, version, required=required_platforms, source_only=source_only)
    files.update(additions)
    revision = git(root, "rev-parse", "HEAD").decode().strip()
    epoch = int(git(root, "show", "-s", "--format=%ct", "HEAD").decode().strip())
    date = datetime.fromtimestamp(max(epoch, 315532800), timezone.utc)
    date_tuple = (date.year, date.month, date.day, date.hour, date.minute, date.second // 2 * 2)
    package = "agent-harness-" + version
    manifest = {"schema_version": 1, "version": version, "source_revision": revision,
                "source_epoch": epoch, "package_root": package, "native_platforms": platforms,
                "kind": "source" if source_only else "portable",
                "files": [{"path": name, "bytes": len(data), "sha256": sha(data), "mode": mode}
                          for name, (data, mode) in sorted(files.items())]}
    files["release-manifest.json"] = (encoded(manifest), 0o100644)
    output_dir.mkdir(parents=True, exist_ok=True)
    for parent in (*reversed(output_dir.parents), output_dir):
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ReleaseError("Linked release output parent is forbidden")
    with tempfile.TemporaryFile() as stream:
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, (data, mode) in sorted(files.items()):
                item = zipfile.ZipInfo(package + "/" + name, date_tuple)
                item.create_system = 3
                item.external_attr = mode << 16
                item.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(item, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        stream.seek(0)
        payload = stream.read(MAX_ARCHIVE_BYTES + 1)
    if len(payload) > MAX_ARCHIVE_BYTES:
        raise ReleaseError("Release ZIP exceeds byte budget")
    # Verify the exact candidate, including every embedded hash, before publishing it locally.
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False, dir=output_dir) as temporary:
        temporary.write(payload)
        temporary_path = Path(temporary.name)
    try:
        verify_archive(temporary_path)
    finally:
        temporary_path.unlink()
    filename = package + ("-source" if source_only else "") + ".zip"
    destination = output_dir / filename
    _write_once(destination, payload)
    checksum = sha(payload)
    checksums = (checksum + "  " + filename + "\n").encode()
    _write_once(output_dir / (filename + ".sha256"), checksums)
    _write_once(output_dir / "SHA256SUMS", checksums)
    return {"status": "built", "version": version, "source_revision": revision,
            "archive": str(destination), "sha256": checksum, "bytes": len(payload),
            "native_platforms": platforms, "manifest_files": len(manifest["files"]),
            "kind": manifest["kind"], "reproducibility": "Git blobs and verified native bytes; fixed ZIP metadata; build duration omitted"}


def verify_archive(archive_path, *, extract_to=None):
    archive_path = Path(archive_path)
    regular_read(archive_path)
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        if not members or len(members) > MAX_FILES or sum(row.file_size for row in members) > MAX_ARCHIVE_BYTES:
            raise ReleaseError("Distribution member/byte budget exceeded")
        names, roots = set(), set()
        for row in members:
            safe_name(row.filename)
            path = PurePosixPath(row.filename)
            roots.add(path.parts[0])
            if len(path.parts) < 2 or row.filename in names or row.is_dir() or stat.S_ISLNK(row.external_attr >> 16):
                raise ReleaseError("Duplicate, linked, or invalid ZIP member")
            names.add(row.filename)
        if len(roots) != 1:
            raise ReleaseError("Distribution must have one package directory")
        package = next(iter(roots))
        manifest_name = package + "/release-manifest.json"
        manifest = json.loads(archive.read(manifest_name))
        required = {"schema_version", "version", "source_revision", "source_epoch", "package_root", "native_platforms", "kind", "files"}
        if set(manifest) != required or manifest["schema_version"] != 1 or manifest["package_root"] != package:
            raise ReleaseError("Distribution manifest schema mismatch")
        expected, files = {manifest_name}, {}
        for row in manifest["files"]:
            if set(row) != {"path", "bytes", "sha256", "mode"}:
                raise ReleaseError("Distribution file schema mismatch")
            safe_name(row["path"])
            name = package + "/" + row["path"]
            if name in expected:
                raise ReleaseError("Duplicate distribution manifest entry")
            expected.add(name)
            data = archive.read(name)
            if len(data) != row["bytes"] or sha(data) != row["sha256"] or archive.getinfo(name).external_attr >> 16 != row["mode"]:
                raise ReleaseError("Distribution content hash/size/mode mismatch")
            files[row["path"]] = (data, row["mode"])
        if names != expected or version_of(files) != manifest["version"]:
            raise ReleaseError("Distribution inventory/version mismatch")
        if extract_to is not None:
            destination = Path(extract_to).absolute()
            for parent in (*reversed(destination.parents), destination):
                if parent.exists() or parent.is_symlink():
                    info = parent.lstat()
                    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                        raise ReleaseError("Linked extraction path is forbidden")
            if destination.exists() and any(destination.iterdir()):
                raise ReleaseError("Extraction destination must be empty")
            destination.mkdir(parents=True, exist_ok=True)
            for row in members:
                target = destination / row.filename
                if not target.resolve().is_relative_to(destination.resolve()):
                    raise ReleaseError("Unsafe distribution extraction path")
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as output:
                    output.write(archive.read(row.filename))
                if os.name == "posix":
                    target.chmod((row.external_attr >> 16) & 0o777)
            native_bundles(destination / package, files, manifest["version"], required=manifest["native_platforms"], source_only=manifest["kind"] == "source")
        return {"status": "verified", "version": manifest["version"], "source_revision": manifest["source_revision"],
                "package_root": package, "native_platforms": manifest["native_platforms"], "files": len(files)}


def smoke_archive(archive_path):
    """Fresh isolated home install/verify/reinstall/rollback; no real model calls."""
    commands = []
    with tempfile.TemporaryDirectory(prefix="harness-release-smoke-") as directory:
        base = Path(directory)
        verified = verify_archive(archive_path, extract_to=base / "distribution")
        package = base / "distribution" / verified["package_root"]
        home, skills, state = base / "codex", base / "skills", base / "state"
        home.mkdir()
        original = ('# user-owned fixture\nmodel = "distribution-fixture"\nmodel_provider = "distribution-provider"\nmodel_reasoning_effort = "high"\n'
                    'model_auto_compact_token_limit = 500000\napproval_policy = "never"\nsandbox_mode = "read-only"\n'
                    '[agents]\nmax_concurrent_threads_per_session = 2\n'
                    '[mcp_servers.fixture]\ncommand = "fixture-command-never-executed"\n').encode()
        (home / "config.toml").write_bytes(original)
        user_instructions = b"User-owned instructions stay intact.\n"
        (home / "AGENTS.md").write_bytes(user_instructions)
        env = {**os.environ, "AGENT_HARNESS_BACKEND": "python"}
        def run(action, *extra):
            argv = [sys.executable, "-I", "-B", str(package / "tools" / "harness.py"), action,
                    "--codex-home", str(home), "--skills-home", str(skills), "--state-dir", str(state), *extra]
            completed = subprocess.run(argv, cwd=package, env=env, capture_output=True, text=True,
                                       encoding="utf-8", timeout=90, shell=False)
            if completed.returncode:
                raise ReleaseError("Extracted installer smoke failed: " + action + ": " + completed.stdout[-1000:] + completed.stderr[-1000:])
            value = json.loads(completed.stdout)
            commands.append({"command": action, "apply": "--apply" in extra, "exit_code": 0, "status": value.get("status", "diagnostic")})
            return value
        run("doctor")
        planned = run("install", "--max-child-threads", "5")
        if planned.get("status") != "dry_run":
            raise ReleaseError("Extracted installer did not perform dry-run")
        run("install", "--max-child-threads", "5", "--apply")
        run("verify")
        current = tomllib.loads((home / "config.toml").read_text(encoding="utf-8-sig"))
        previous = tomllib.loads(original.decode())
        if any(current.get(key) != value for key, value in previous.items() if key != "agents") or current["agents"]["max_concurrent_threads_per_session"] != 5:
            raise ReleaseError("Extracted installer changed user model/security/settings")
        if run("install", "--max-child-threads", "5", "--apply").get("status") != "unchanged":
            raise ReleaseError("Extracted installer is not idempotent")
        run("rollback")
        run("rollback", "--apply")
        if (home / "config.toml").read_bytes() != original or (home / "AGENTS.md").read_bytes() != user_instructions:
            raise ReleaseError("Extracted installer rollback failed to restore user bytes")
    return {"status": "passed", "scope": "fresh isolated Codex/skills/state homes; no native model call", "commands": commands}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--require-platform", action="append", default=[])
    parser.add_argument("--source-only", action="store_true", help="Explicit source artifact without native bundles; not a full portable release")
    parser.add_argument("--skip-smoke", action="store_true", help="Build/integrity check only; extracted installer acceptance remains not_run")
    parser.add_argument("--verify", type=Path, help="Verify an existing artifact instead of building")
    args = parser.parse_args(argv)
    try:
        if args.verify:
            value = verify_archive(args.verify)
            archive = args.verify
        else:
            value = build_release(args.source_root, args.output_dir, required_platforms=args.require_platform, source_only=args.source_only)
            archive = Path(value["archive"])
        value["extracted_install_smoke"] = {"status": "not_run"} if args.skip_smoke else smoke_archive(archive)
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    except (ReleaseError, OSError, ValueError, KeyError, subprocess.SubprocessError, zipfile.BadZipFile) as error:
        print(json.dumps({"status": "failed", "error": str(error), "type": type(error).__name__}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
