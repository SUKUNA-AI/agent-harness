"""Required native-job regression: actual offline Cargo artifact and recovery.

Uses a tiny disposable crate, never rebuilds or modifies the source checkout.
The only injected failures are publication/restoration rename errors.
"""
from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def main():
    spec = importlib.util.spec_from_file_location("native_build_under_test", ROOT / "tools/build_native.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    with tempfile.TemporaryDirectory(prefix="harness-native-build-gate-") as directory:
        root = Path(directory).resolve()
        crate = root / "native"
        (crate / "src").mkdir(parents=True)
        (crate / "Cargo.toml").write_text(
            f'[package]\nname="harness-native"\nversion="{builder.__version__}"\nedition="2021"\n',
            encoding="utf-8")
        shutil.copyfile(ROOT / "native/rust-toolchain.toml", crate / "rust-toolchain.toml")
        source = crate / "src/main.rs"
        source.write_text('fn main() { println!("old-source"); }\n', encoding="utf-8")
        target = root / "target"
        env = {**os.environ, "CARGO_TARGET_DIR": str(target)}
        env.pop("CARGO_BUILD_TARGET", None)
        host = next(line.removeprefix("host: ") for line in subprocess.check_output(
            ["rustc", "-vV"], cwd=crate, env=env, text=True).splitlines() if line.startswith("host: "))
        manifest = ["--manifest-path", str(crate / "Cargo.toml"), "--offline"]
        subprocess.run(["cargo", "generate-lockfile", *manifest], cwd=crate, env=env, check=True)
        subprocess.run(["cargo", "build", "--release", "--target", host, "--locked", *manifest],
                       cwd=crate, env=env, check=True)
        filename = "harness-native.exe" if os.name == "nt" else "harness-native"
        actual = target / host / "release" / filename
        flat = target / "release" / filename
        flat.parent.mkdir(exist_ok=True)
        shutil.copyfile(actual, flat)
        source.write_text('fn main() { println!("current-source"); }\n', encoding="utf-8")
        env["CARGO_BUILD_TARGET"] = host
        out = io.StringIO()
        with patch.object(builder, "ROOT", root), patch.dict(os.environ, env, clear=True), redirect_stdout(out):
            assert builder.main(["--offline", "--output-dir", str(root / "bin")]) == 0
        info = json.loads(out.getvalue())
        destination = Path(info["path"])
        binary = destination / filename
        assert subprocess.check_output([str(binary)], text=True).strip() == "current-source"
        assert hashlib.sha256(binary.read_bytes()).hexdigest() != hashlib.sha256(flat.read_bytes()).hexdigest()
        assert info["source_files"]["native/src/main.rs"] == hashlib.sha256(source.read_bytes()).hexdigest()
        before = {p.relative_to(destination).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in destination.rglob("*") if p.is_file()}
        original_rename = Path.rename

        for fail_restore in (False, True):
            def failing_rename(path, target_path):
                if Path(target_path) == destination and path.parent.name.startswith("native-build-"):
                    if path.name != "previous" or fail_restore:
                        raise PermissionError("injected native publication/restore failure")
                return original_rename(path, target_path)

            try:
                with patch.object(builder, "ROOT", root), patch.dict(os.environ, env, clear=True), \
                     patch.object(Path, "rename", failing_rename), redirect_stdout(io.StringIO()):
                    builder.main(["--offline", "--output-dir", str(root / "bin")])
            except builder.BuildRecoveryRequired as error:
                assert fail_restore, "A successful restore must not require recovery"
                recovery = Path(error.recovery_path)
                prior = recovery / "previous"
                assert prior.is_dir() and not destination.exists()
                assert before == {p.relative_to(prior).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in prior.rglob("*") if p.is_file()}, "Prior bundle was lost"
            except PermissionError:
                assert not fail_restore
                assert before == {p.relative_to(destination).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in destination.rglob("*") if p.is_file()}
                assert not list((root / "bin").glob("native-build-*"))
            else:
                raise AssertionError("Injected failure was reported as successful publication")
    print(json.dumps({"status": "passed", "checks": ["explicit target rejects stale flat artifact",
                      "failed publication restores prior bundle", "failed restoration preserves recovery directory"],
                      "cargo": "real offline tiny crate", "fixture_only": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
