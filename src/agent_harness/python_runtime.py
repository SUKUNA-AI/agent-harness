"""Narrow, finite Windows Python runtime proof for optional literal read caching.

Ordinary installations/venvs/POSIX discovery are deliberately unsupported. A
local ._pth must close startup over a local, trusted encodings ZIP and bundled
runtime DLLs. No caller purity flag can substitute for this runtime proof.
"""
from __future__ import annotations

import encodings
import hashlib
import os
from pathlib import Path
import stat
import re
import sys
import time
import zipfile

from .core import HarnessError, digest
from .execution_ledger import HASH, SHORT, object_schema, safe_private
from .file_io import read_regular_file

MAX_RUNTIME_FILE_BYTES = 16 * 1024 * 1024
MAX_RUNTIME_BYTES = 32 * 1024 * 1024
MAX_RUNTIME_FILES = 16
MAX_RUNTIME_SECONDS = 5.0
RUNTIME_SCHEMA = object_schema({"schema_version": {"const": 1},
    "policy": {"const": "closed_windows_literal_python_runtime_v1"}, "root": SHORT,
    "files": {"type": "object", "maxProperties": MAX_RUNTIME_FILES,
        "additionalProperties": object_schema({"sha256": HASH, "bytes": {"type": "integer", "minimum": 0,
            "maximum": MAX_RUNTIME_FILE_BYTES}}, ("sha256", "bytes"))},
    "absent_startup_paths": {"type": "array", "maxItems": 4, "items": SHORT},
    "stdlib_reference_digest": HASH, "total_bytes": {"type": "integer", "minimum": 0, "maximum": MAX_RUNTIME_BYTES},
    "digest": HASH}, ("schema_version", "policy", "root", "files", "absent_startup_paths",
    "stdlib_reference_digest", "total_bytes", "digest"))


def runtime_closure(executable):
    try:
        return _runtime_closure(executable)
    except (zipfile.BadZipFile, UnicodeError, EOFError) as error:
        raise HarnessError("Python runtime startup/stdlib closure is invalid") from error


def _runtime_closure(executable):
    from .codex_execution import _binary_hash
    if os.name != "nt":
        raise HarnessError("Optional Python cache requires a proven self-contained runtime; platform unsupported")
    executable = safe_private(executable)
    if executable.name.casefold() != "python.exe":
        raise HarnessError("Optional cache does not infer arbitrary interpreter startup")
    root = executable.parent
    if _binary_hash(executable) != _binary_hash(sys.executable):
        raise HarnessError("Optional cache runtime must match the running trusted Python binary")
    version = str(sys.version_info.major) + str(sys.version_info.minor)
    stem = "python" + version
    required = {"python.exe", stem + ".dll", "python3.dll", "vcruntime140.dll", "vcruntime140_1.dll",
                stem + "._pth", stem + ".zip"}
    # A shared-library ._pth overrides registry, environment and other sys.path
    # discovery. Reject competing/venv configs rather than infer precedence.
    absent = [root / "python._pth", root / "pyvenv.cfg", root.parent / "pyvenv.cfg"]
    for path in absent:
        safe_private(path)
        if path.exists():
            raise HarnessError("Alternate Python startup/venv configuration prevents complete cache proof")
    started, files, total = time.monotonic(), {}, 0
    root_stamp = root.stat().st_mtime_ns
    children = list(root.iterdir())
    if len(children) > MAX_RUNTIME_FILES or {p.name.casefold() for p in children} != required:
        raise HarnessError("Optional runtime must contain exactly the finite bundled DLL/ZIP/startup inputs")
    for path in children:
        safe_private(path)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RUNTIME_FILE_BYTES:
            raise HarnessError("Runtime input is unsupported or exceeds per-file budget")
        total += info.st_size
        if total > MAX_RUNTIME_BYTES or time.monotonic() - started > MAX_RUNTIME_SECONDS:
            raise HarnessError("Runtime closure exceeds finite byte/time budget")
        if path.suffix.casefold() in {".exe", ".dll"}:
            first, second = _binary_hash(path), _binary_hash(path)
            if first != second:
                raise HarnessError("Runtime binary changed during bounded verification")
            sha = first
        else:
            sha = hashlib.sha256(read_regular_file(path, max_bytes=MAX_RUNTIME_FILE_BYTES)).hexdigest()
        files[path.name.casefold()] = {"sha256": sha, "bytes": info.st_size}
    config = read_regular_file(root / (stem + "._pth"), max_bytes=4096).decode("utf-8-sig")
    paths = [line.strip() for line in config.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if paths != [stem + ".zip"]:
        raise HarnessError("Python startup must name only the local literal stdlib ZIP; no external paths/import site")
    reference_root = Path(encodings.__file__).parent
    if not reference_root.is_dir():
        raise HarnessError("Trusted stdlib reference is unavailable; do not infer cache closure")
    reference = {}
    with zipfile.ZipFile(root / (stem + ".zip")) as archive:
        members = archive.infolist()
        if not members or len(members) > 256 or sum(item.file_size for item in members) > 4 * 1024 * 1024:
            raise HarnessError("Runtime ZIP exceeds the bounded encodings reference")
        seen = set()
        for member in members:
            name = member.filename
            codec = (name.startswith("encodings/") and len(Path(name).parts) == 2
                     and re.fullmatch(r"[A-Za-z0-9_]+\.py", Path(name).name))
            if name in seen or not (codec or name == "linecache.py"):
                raise HarnessError("Runtime ZIP contains unknown startup code")
            seen.add(name)
            source = reference_root / Path(name).name if codec else reference_root.parent / "linecache.py"
            trusted = read_regular_file(safe_private(source), max_bytes=MAX_RUNTIME_FILE_BYTES)
            if archive.read(member) != trusted:
                raise HarnessError("Runtime startup module differs from the trusted stdlib reference")
            reference[name] = hashlib.sha256(trusted).hexdigest()
        if not {"encodings/__init__.py", "encodings/aliases.py", "encodings/utf_8.py", "linecache.py"} <= seen:
            raise HarnessError("Runtime ZIP lacks mandatory startup codec modules")
    if root.stat().st_mtime_ns != root_stamp or time.monotonic() - started > MAX_RUNTIME_SECONDS:
        raise HarnessError("Runtime directory changed or proof exceeded time budget")
    # Repeat complete byte bindings after parsing/reference reads.
    for name, entry in files.items():
        path = root / name
        current = _binary_hash(path) if path.suffix in {".exe", ".dll"} else hashlib.sha256(read_regular_file(path, max_bytes=MAX_RUNTIME_FILE_BYTES)).hexdigest()
        if current != entry["sha256"]:
            raise HarnessError("Runtime source changed during closure proof")
    if any(path.exists() for path in absent):
        raise HarnessError("Python startup configuration appeared during proof")
    if (root.stat().st_mtime_ns != root_stamp or {p.name.casefold() for p in root.iterdir()} != required
            or time.monotonic() - started > MAX_RUNTIME_SECONDS):
        raise HarnessError("Python runtime inventory changed or exceeded its proof budget")
    value = {"schema_version": 1, "policy": "closed_windows_literal_python_runtime_v1", "root": str(root),
             "files": files, "absent_startup_paths": [str(p) for p in absent],
             "stdlib_reference_digest": digest(reference), "total_bytes": total}
    value["digest"] = digest(value)
    return value
