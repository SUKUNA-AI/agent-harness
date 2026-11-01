"""Build and package the local Rust helper. Never runs during ordinary tool use."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from agent_harness import __version__


def platform_key():
    system = {"Linux": "linux", "Windows": "windows", "Darwin": "macos"}.get(platform.system())
    arch = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}.get(platform.machine().lower())
    if not system or not arch:
        raise ValueError("Unsupported native distribution platform")
    return f"{system}-{arch}"


def source_files():
    return sorted(path for path in (ROOT / "native").rglob("*") if path.is_file()
                  and "target" not in path.relative_to(ROOT / "native").parts
                  and (path.name in {"Cargo.toml", "Cargo.lock", "rust-toolchain.toml"}
                       or path.suffix in {".rs", ".scm"}))


def source_fingerprint():
    entries = {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
               for path in source_files()}
    encoded = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest(), entries


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n")


def third_party_licenses(metadata, destination):
    """Ship dependency notices beside the binary, including grammar licenses."""
    packages = []
    active = {node["id"] for node in metadata["resolve"]["nodes"]}
    for package in metadata["packages"]:
        if package["source"] is None or package["id"] not in active:
            continue
        directory = Path(package["manifest_path"]).parent
        notices = sorted(path for path in directory.iterdir() if path.is_file()
                         and path.name.upper().startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE")))
        if package.get("license_file"):
            extra = directory / package["license_file"]
            if extra.is_file() and extra not in notices:
                notices.append(extra)
        label = f"{package['name']}-{package['version']}"
        supplied = ROOT / "native" / "third_party" / label / "LICENSE"
        if not notices and supplied.is_file():
            notices.append(supplied)
        if not notices:
            raise ValueError(f"Dependency {label} has no packaged license notice")
        out = destination / label
        out.mkdir(parents=True)
        for notice in notices:
            if notice.is_symlink() or notice.stat().st_size > 1024 * 1024:
                raise ValueError(f"Unsupported dependency notice: {label}/{notice.name}")
            shutil.copyfile(notice, out / notice.name)
        if package["name"] == "tree-sitter":
            unicode_notice = directory / "src" / "unicode" / "LICENSE"
            if unicode_notice.is_file():
                shutil.copyfile(unicode_notice, out / "UNICODE-LICENSE")
        packages.append({"name": package["name"], "version": package["version"],
                         "license": package.get("license"), "repository": package.get("repository"),
                         "notices": [f"{label}/{path.name}" for path in sorted(out.iterdir())]})
    write_json(destination / "index.json", {"schema_version": 1, "dependencies": packages})


def rust_library_notices(destination, env, cwd):
    sysroot = Path(subprocess.check_output(["rustc", "--print", "sysroot"], cwd=cwd, env=env, text=True).strip())
    docs = sysroot / "share" / "doc" / "rust"
    notice = docs / "COPYRIGHT-library.html"
    if not notice.is_file() or not (docs / "licenses").is_dir():
        raise ValueError("Rust standard-library distribution notices are missing from this toolchain")
    out = destination / "rust-standard-library"
    out.mkdir()
    shutil.copyfile(notice, out / notice.name)
    shutil.copytree(docs / "licenses", out / "licenses")


class BuildRecoveryRequired(OSError):
    """An existing bundle remains in a retained recovery directory."""

    def __init__(self, directory, error):
        self.recovery_path = str(directory)
        super().__init__(f"Native bundle publication needs recovery at {directory}: {error}")


def publish_bundle(staged, destination):
    """Publish a sibling staging directory, preserving a failed compensation.

    The caller may remove staged.parent only after this returns or raises an
    ordinary error. BuildRecoveryRequired makes the whole directory persistent.
    """
    old = staged.parent / "previous"
    try:
        if destination.exists():
            if destination.is_symlink() or not destination.is_dir():
                raise ValueError("Native destination must be a regular directory")
            destination.rename(old)
        staged.rename(destination)
    except BaseException:
        if old.exists():
            try:
                # Do not overwrite a destination with an uncertain publication
                # outcome or one created concurrently by another writer.
                if destination.exists() or destination.is_symlink():
                    raise OSError("Destination exists after failed publication; previous bundle retained")
                old.rename(destination)
            except BaseException as recovery_error:
                raise BuildRecoveryRequired(staged.parent, recovery_error) from recovery_error
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="Use an already populated Cargo cache")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "bin")
    args = parser.parse_args(argv)
    cargo = shutil.which("cargo")
    if not cargo:
        raise ValueError("Cargo is unavailable; use the archive containing native binaries, or install Rust 1.90.0")
    key = platform_key()
    target_dir = Path(os.environ.get("CARGO_TARGET_DIR", ROOT / "native" / "target")).resolve()
    env = {**os.environ, "CARGO_TARGET_DIR": str(target_dir)}
    manifest = ROOT / "native" / "Cargo.toml"
    common = ["--manifest-path", str(manifest), "--locked"] + (["--offline"] if args.offline else [])
    verbose_version = subprocess.check_output(["rustc", "-vV"], cwd=manifest.parent, env=env, text=True)
    host = next(line.removeprefix("host: ") for line in verbose_version.splitlines() if line.startswith("host: "))
    fingerprint, inputs = source_fingerprint()
    started = time.perf_counter()
    # An explicit host target overrides CARGO_BUILD_TARGET and build.target in
    # Cargo configuration. Never reuse an unrelated flat target/release file.
    subprocess.run([cargo, "build", "--release", "--target", host, *common], cwd=manifest.parent, env=env, check=True)
    elapsed = time.perf_counter() - started
    metadata = json.loads(subprocess.check_output([cargo, "metadata", "--format-version", "1", "--filter-platform", host, *common], cwd=manifest.parent, env=env))
    binary_name = "harness-native.exe" if os.name == "nt" else "harness-native"
    executable = target_dir / host / "release" / binary_name
    if not executable.is_file():
        raise ValueError("Cargo did not produce the native executable")
    version = subprocess.check_output(["rustc", "--version"], cwd=manifest.parent, env=env, text=True).strip()
    if source_fingerprint() != (fingerprint, inputs):
        raise ValueError("Native sources changed during compilation; no bundle published")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    destination = args.output_dir / key
    # A failed restore must outlive this process, so cleanup is explicit instead
    # of TemporaryDirectory's unconditional removal of the only prior copy.
    directory = Path(tempfile.mkdtemp(prefix="native-build-", dir=args.output_dir))
    retain_recovery = False
    try:
        staged = directory / key
        staged.mkdir()
        binary = staged / binary_name
        shutil.copy2(executable, binary)
        license_dir = staged / "licenses"
        license_dir.mkdir()
        third_party_licenses(metadata, license_dir)
        rust_library_notices(license_dir, env, manifest.parent)
        info = {"schema_version": 1, "version": __version__, "protocol_version": 1,
                "platform": key, "target": host, "binary": binary_name,
                "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                "binary_bytes": binary.stat().st_size,
                "source_digest": fingerprint, "source_files": inputs,
                "rustc": version, "profile": "release", "build_seconds": round(elapsed, 4)}
        write_json(staged / "build-info.json", info)
        publish_bundle(staged, destination)
    except BuildRecoveryRequired:
        retain_recovery = True
        raise
    finally:
        if not retain_recovery:
            shutil.rmtree(directory)
    print(json.dumps({"status": "built", "path": str(destination), **info}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BuildRecoveryRequired as error:
        print(json.dumps({"status": "recovery_required", "recovery_path": error.recovery_path,
                          "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
