from pathlib import Path, PureWindowsPath
import tempfile
import unittest
from urllib.parse import quote

from agent_harness.core import HarnessError
from agent_harness.lsp import _file_uri_path, _uri_name


class LSPFileURITests(unittest.TestCase):
    def test_windows_literal_and_encoded_drive_are_absolute(self):
        expected = PureWindowsPath("C:/workspace/project/module.py")
        for uri in ("file:///C:/workspace/project/module.py",
                    "file:///c%3A/workspace/project/module.py",
                    "file:///C%3a/workspace/project/module.py",
                    "file://localhost/C%3A/workspace/project/module.py"):
            with self.subTest(uri=uri):
                result = _file_uri_path(uri, windows=True)
                self.assertTrue(result.is_absolute())
                self.assertEqual(expected, result)
                self.assertEqual("module.py", result.relative_to(PureWindowsPath("c:/workspace/project")).as_posix())

    def test_decodes_unicode_spaces_and_literal_percent_sequences_once(self):
        uri = "file:///C%3A/project/na%C3%AFve%20%E7%A9%BA%20%252F%252E.py"
        self.assertEqual(PureWindowsPath("C:/project/naïve 空 %2F%2E.py"),
                         _file_uri_path(uri, windows=True))
        self.assertEqual("%2e%2e", _file_uri_path("file:///C%3A/project/%252e%252e/module.py", windows=True).parts[-2])

    def test_native_workspace_paths_preserve_literal_percent_and_localhost(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            name = "module 空 %2F%2E.py"
            target = root / name
            target.write_text("value = 1\n", encoding="utf-8")
            uri = target.as_uri()
            self.assertEqual(name, _uri_name(root, uri))
            self.assertEqual(name, _uri_name(root, uri.replace("file://", "file://localhost", 1)))
            if root.drive:
                # The same encoded-drive spelling produced by real Pyright.
                encoded_drive = uri.replace(root.drive, quote(root.drive, safe=""), 1)
                self.assertEqual(name, _uri_name(root, encoded_drive))

    def test_remote_authorities_unc_and_non_file_schemes_are_not_local(self):
        for windows in (False, True):
            for uri in ("https://example.invalid/module.py", "file://server/share/module.py",
                        "file:////server/share/module.py", "file://localhost//server/share/module.py",
                        "file:///%2Fserver/share/module.py", "file:///C:/project/module.py?query=1",
                        "file:///C:/project/module.py#fragment"):
                with self.subTest(windows=windows, uri=uri):
                    self.assertIsNone(_file_uri_path(uri, windows=windows))

    def test_malformed_encoding_and_controls_are_rejected_before_path_use(self):
        for uri in ("file:///C:/project/%zz.py", "file:///C:/project/end%",
                    "file:///C:/project/%ff.py", "file:///C:/project/%00.py",
                    "file:///C:/project/raw\nname.py", "file:///C:/project/raw\tname.py",
                    "file:///C:/project/%5c..%5coutside.py"):
            with self.subTest(uri=uri), self.assertRaises(HarnessError):
                _file_uri_path(uri, windows=True)

    def test_windows_rooted_or_drive_relative_paths_remain_invalid(self):
        for uri in ("file:///project/module.py", "file:C:project/module.py", "file:///C:project/module.py"):
            with self.subTest(uri=uri), self.assertRaisesRegex(HarnessError, "absolute"):
                _file_uri_path(uri, windows=True)

    def test_native_traversal_and_outside_root_guards_remain_active(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "workspace"
            root.mkdir()
            self.assertIsNone(_uri_name(root, (base / "outside.py").as_uri()))
            for tail in ("../outside.py", "%2e%2e/outside.py", "child/%2e%2e/module.py"):
                with self.subTest(tail=tail), self.assertRaises(HarnessError):
                    _uri_name(root, root.as_uri() + "/" + tail)


if __name__ == "__main__":
    unittest.main()
