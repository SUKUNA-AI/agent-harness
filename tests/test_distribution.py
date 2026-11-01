"""Reproducibility, fail-closed bundle identity, hostile ZIP and installer smoke."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("harness_build_release", ROOT / "tools" / "build_release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def command(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True).stdout


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self.source.mkdir()
        files = {"pyproject.toml": '[project]\nname="fixture"\nversion="' + VERSION + '"\n',
                 "src/agent_harness/__init__.py": '__version__ = "' + VERSION + '"\n',
                 "src/agent_harness/installation.py": 'VERSION = "' + VERSION + '"\n',
                 "native/Cargo.toml": '[package]\nname="fixture"\nversion="' + VERSION + '"\n',
                 "native/src/main.rs": 'fn main() {}\n', "README.md": "Portable fixture\nsecond line\n",
                 ".gitignore": "bin/*/\n.env\nauth.json\n__pycache__/\n"}
        for name, content in files.items():
            target = self.source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
        command(self.source, "init", "--quiet")
        command(self.source, "config", "core.autocrlf", "false")
        command(self.source, "add", ".")
        command(self.source, "-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "commit", "--quiet", "-m", "distribution fixture")

    def tearDown(self):
        self.temp.cleanup()

    def bundle(self, *, version=VERSION):
        files = release.tracked_blobs(self.source)
        source_digest, sources = release.native_source_identity(files)
        folder = self.source / "bin" / "windows-x86_64"
        notices = folder / "licenses" / "fixture-1.0"
        notices.mkdir(parents=True)
        binary = b"fixture executable bytes; not a claimed runtime binary"
        (folder / "harness-native.exe").write_bytes(binary)
        (notices / "LICENSE").write_text("Fixture license text\n")
        (folder / "licenses" / "index.json").write_text(json.dumps({"schema_version": 1,
            "dependencies": [{"name": "fixture", "version": "1.0", "notices": ["fixture-1.0/LICENSE"]}]}))
        info = {"schema_version": 1, "protocol_version": 1, "version": version,
            "platform": "windows-x86_64", "binary": "harness-native.exe",
            "source_digest": source_digest, "source_files": sources,
            "binary_sha256": release.sha(binary), "binary_bytes": len(binary), "build_seconds": 1.2345}
        (folder / "build-info.json").write_text(json.dumps(info))
        return folder

    def test_git_blob_zip_is_reproducible_and_omits_ignored_private_files(self):
        (self.source / ".env").write_text("PRIVATE_VALUE_NEVER_ARCHIVED=opaque\n")
        (self.source / "auth.json").write_text('{"private_fixture":"never_archive"}')
        first = release.build_release(self.source, self.base / "first", source_only=True)
        command(self.source, "config", "core.autocrlf", "true")
        (self.source / "README.md").write_bytes(b"Portable fixture\r\nsecond line\r\n")
        # Refresh this owned fixture through Git's new checkout conversion so
        # the working tree is demonstrably clean under the changed setting.
        command(self.source, "checkout", "--", "README.md")
        second = release.build_release(self.source, self.base / "second", source_only=True)
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual(Path(first["archive"]).read_bytes(), Path(second["archive"]).read_bytes())
        with zipfile.ZipFile(first["archive"]) as archive:
            self.assertFalse(any(name.endswith(("/.env", "/auth.json")) for name in archive.namelist()))
            self.assertEqual(archive.read("agent-harness-" + VERSION + "/README.md"), b"Portable fixture\nsecond line\n")
        self.assertEqual(release.verify_archive(first["archive"])["version"], VERSION)
        self.assertTrue((self.base / "first" / "SHA256SUMS").read_text().startswith(first["sha256"]))

    def test_dirty_source_is_rejected_before_output(self):
        (self.source / "README.md").write_text("unchecked edit\n")
        with self.assertRaisesRegex(release.ReleaseError, "clean and committed"):
            release.build_release(self.source, self.base / "output", source_only=True)
        self.assertFalse((self.base / "output").exists())

    def test_outdated_native_bundle_and_missing_required_platform_fail_closed(self):
        self.bundle(version="0.0.0")
        with self.assertRaisesRegex(release.ReleaseError, "identity mismatch"):
            release.build_release(self.source, self.base / "bad")
        info_path = self.source / "bin" / "windows-x86_64" / "build-info.json"
        info = json.loads(info_path.read_text())
        info["version"] = VERSION
        info_path.write_text(json.dumps(info))
        with self.assertRaisesRegex(release.ReleaseError, "Required native platforms missing"):
            release.build_release(self.source, self.base / "missing", required_platforms=["linux-x86_64"])

    def test_verified_bundle_extracts_and_binary_tampering_rejects(self):
        self.bundle()
        result = release.build_release(self.source, self.base / "release", required_platforms=["windows-x86_64"])
        extracted = self.base / "extracted"
        checked = release.verify_archive(result["archive"], extract_to=extracted)
        self.assertEqual(checked["native_platforms"], ["windows-x86_64"])
        metadata = json.loads((extracted / checked["package_root"] / "bin/windows-x86_64/build-info.json").read_text())
        self.assertNotIn("build_seconds", metadata)
        damaged = self.base / "tampered.zip"
        with zipfile.ZipFile(result["archive"]) as before, zipfile.ZipFile(damaged, "w") as after:
            for item in before.infolist():
                value = before.read(item.filename)
                after.writestr(item, b"tampered" if item.filename.endswith("/harness-native.exe") else value)
        with self.assertRaisesRegex(release.ReleaseError, "content hash/size/mode mismatch"):
            release.verify_archive(damaged)

    def test_unknown_native_file_or_missing_license_is_rejected(self):
        folder = self.bundle()
        (folder / "unreviewed.dat").write_bytes(b"unknown")
        with self.assertRaisesRegex(release.ReleaseError, "Unreviewed native bundle"):
            release.build_release(self.source, self.base / "unknown")
        (folder / "unreviewed.dat").unlink()
        (folder / "licenses/fixture-1.0/LICENSE").unlink()
        with self.assertRaisesRegex(release.ReleaseError, "Invalid native bundle"):
            release.build_release(self.source, self.base / "license")

    def test_hostile_archive_member_is_rejected_without_extracting(self):
        result = release.build_release(self.source, self.base / "valid", source_only=True)
        hostile = self.base / "hostile.zip"
        with zipfile.ZipFile(result["archive"]) as before, zipfile.ZipFile(hostile, "w") as after:
            for item in before.infolist():
                after.writestr(item, before.read(item.filename))
            after.writestr("../outside.txt", "no")
        destination = self.base / "destination"
        with self.assertRaisesRegex(release.ReleaseError, "unsafe distribution path"):
            release.verify_archive(hostile, extract_to=destination)
        self.assertFalse(destination.exists())
        self.assertFalse((self.base / "outside.txt").exists())

    def test_tracked_private_file_is_not_publishable(self):
        private = self.source / "credentials.json"
        private.write_text('{"fixture":"private"}')
        command(self.source, "add", "credentials.json")
        command(self.source, "-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "commit", "--quiet", "-m", "private fixture")
        with self.assertRaisesRegex(release.ReleaseError, "Private or unsafe"):
            release.build_release(self.source, self.base / "private", source_only=True)

    def test_extracted_real_package_installs_verifies_and_rolls_back_isolated_homes(self):
        # Clone committed input, not the currently edited checkout. This is a
        # release-mechanism scenario; root runs it again on the final revision.
        source = self.base / "real-source"
        subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", str(ROOT), str(source)],
                       check=True, capture_output=True)
        result = release.build_release(source, self.base / "real-output", source_only=True)
        smoke = release.smoke_archive(result["archive"])
        self.assertEqual(smoke["status"], "passed")
        self.assertEqual([row["command"] for row in smoke["commands"]],
                         ["doctor", "install", "install", "verify", "install", "rollback", "rollback"])


if __name__ == "__main__":
    unittest.main()
