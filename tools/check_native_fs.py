"""Compare compiled Linux fs_read with the unchanged Python read/hash reference.

Build the development probe with:
  cargo build --release --locked --example fs_probe --manifest-path native/Cargo.toml
Then pass its path using --probe. No installed profile is changed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from agent_harness.file_io import read_regular_file, FileReadError, DEFAULT_MAX_BYTES
from agent_harness.core import file_hash, HarnessError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not sys.platform.startswith("linux"):
        parser.error("Strict native fs_read is qualified only for Linux; no platform PASS is inferred")
    probe = args.probe.resolve(strict=True)
    rows = []
    start = time.monotonic()

    def run(operation, path, limit=None):
        command = [str(probe), operation, str(path)] + ([] if limit is None else [str(limit)])
        out = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        assert out.returncode in (0, 1), (operation, path.name, out.returncode, out.stderr[-1000:])
        assert len(out.stdout) < 4096 and not out.stderr
        value = json.loads(out.stdout)
        assert (value["status"] == "complete") == (out.returncode == 0)
        return value

    with tempfile.TemporaryDirectory(prefix="native-fs-conformance-") as temporary:
        root = Path(temporary)
        recipes = {
            "empty.py": b"", "lf.py": b"A=1\nB=2\n", "crlf.py": b"A=1\r\nB=2\r\n",
            "bom.py": b"\xef\xbb\xbfA=1\n", "no-final-lf.py": b"A=1",
            "unicode.py": "я🙂e\u0301\n".encode(),
            "line-separators.py": "a\u2028b\u0085c\vd\fe\n".encode(),
            "long-line.py": b"x" * 32769, "back\\slash.py": b"literal\n",
            "new\nline.py": b"filename\n", "trailing-space ": b"space\n", "trailing-cr\r": b"cr\n",
        }
        for name, data in recipes.items():
            (root / name).write_bytes(data)
        for name, size in [("exact-cap.bin", DEFAULT_MAX_BYTES), ("over-cap.bin", DEFAULT_MAX_BYTES + 1)]:
            with (root / name).open("wb") as target:
                block = b"R" * 65536
                whole, rest = divmod(size, len(block))
                for _ in range(whole):
                    target.write(block)
                target.write(block[:rest])
        os.link(root / "lf.py", root / "hardlink.py")
        for name in [*recipes, "exact-cap.bin", "over-cap.bin", "hardlink.py"]:
            path = root / name
            expected_hash = file_hash(path)
            native_hash = run("hash", path)
            assert native_hash == {"status": "complete", "size": path.stat().st_size, "sha256": expected_hash}
            native_read = run("read", path)
            try:
                data = read_regular_file(path)
            except FileReadError:
                assert path.stat().st_size > DEFAULT_MAX_BYTES
                assert native_read == {"status": "refused", "kind": "TooLarge"}
            else:
                assert native_read == {"status": "complete", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            rows.append({"case": name, "hash": native_hash, "read": native_read})
        (root / "leaf-link").symlink_to(root / "lf.py")
        (root / "ancestor-link").symlink_to(root, target_is_directory=True)
        os.mkfifo(root / "fifo")
        for name in ["leaf-link", "ancestor-link/lf.py", "fifo", "missing.py"]:
            path = root / name
            for operation, python_fn in [("read", read_regular_file), ("hash", file_hash)]:
                try:
                    python_fn(path)
                except (FileReadError, FileNotFoundError, HarnessError):
                    pass
                else:
                    raise AssertionError((name, operation, "Python unexpectedly accepted"))
                observed = run(operation, path)
                assert observed["status"] == "refused"
                assert observed["kind"] in {"NotFound", "NotRegularFile", "UnsafeTopology"}, observed
                rows.append({"case": name, "operation": operation, "result": observed})
        assert run("read", root / "empty.py", 0)["status"] == "complete"
        assert run("read", root / "lf.py", 0) == {"status": "refused", "kind": "TooLarge"}
        resources = run("resources", root / "lf.py")
        assert resources["before_fds"] == resources["after_fds"]
    result = {"status": "passed", "platform": sys.platform, "cases": rows,
              "resource_check": resources, "elapsed_seconds": time.monotonic() - start,
              "probe_sha256": hashlib.sha256(probe.read_bytes()).hexdigest(),
              "python": sys.version, "limits": ["Linux only; Windows/macOS not qualified",
              "Generic read/hash parity; Python file consumers and installed profile unchanged",
              "Deterministic mutation races are covered by compiled Rust tests, not these static cases"]}
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "cases": len(rows), "resource_check": resources,
                      "elapsed_seconds": result["elapsed_seconds"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
