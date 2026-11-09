"""Bounded diagnostics retain the actual parity mismatch without changing verdict."""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("check_native_diagnostic", Path(__file__).resolve().parents[1] / "tools/check_native.py")
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


class NativeDiagnosticTests(unittest.TestCase):
    def test_physical_root_difference_reports_actual_full_width_values(self):
        expected = {"root_binding": {"device": 0x123456789abcdef0, "inode": 2**100 + 7}, "digest": "expected"}
        observed = {"root_binding": {"device": 0x9abcdef0, "inode": 7}, "digest": "observed"}
        result = check.snapshot_difference(expected, observed)
        rows = {row["path"]: row for row in result["differences"]}
        self.assertEqual(expected["root_binding"]["device"], rows["/root_binding/device"]["python_expected"])
        self.assertEqual(observed["root_binding"]["device"], rows["/root_binding/device"]["rust_observed"])
        self.assertEqual(2**100 + 7, rows["/root_binding/inode"]["python_expected"])
        self.assertFalse(result["truncated"])

    def test_shared_windows_identity_preserves_volume_and_file_high_bits(self):
        from agent_harness.file_io import windows_file_id_identity, FileReadError
        identifier = bytes(range(16))
        result = windows_file_id_identity(0x123456789abcdef0, identifier)
        self.assertEqual({"identity_kind": "windows-file-id-info-v1", "device": "123456789abcdef0",
                          "inode": "000102030405060708090a0b0c0d0e0f"}, result)
        changed = bytearray(identifier)
        changed[15] ^= 1
        self.assertNotEqual(result, windows_file_id_identity(0x123456789abcdef0, bytes(changed)))
        self.assertNotEqual(result, windows_file_id_identity(0x9abcdef0, identifier))
        for volume, file_id in [(True, identifier), (2**64, identifier), (-1, identifier), (1, b'short')]:
            with self.assertRaises(FileReadError):
                windows_file_id_identity(volume, file_id)

    def test_missing_full_windows_identity_capability_refuses_without_fallback(self):
        from agent_harness.file_io import _windows_directory_identity, FileReadError
        from types import SimpleNamespace
        function = unittest.mock.Mock(return_value=0)
        kernel = SimpleNamespace(GetFileInformationByHandleEx=function)
        with self.assertRaisesRegex(FileReadError, 'FileIdInfo root identity is unavailable'):
            _windows_directory_identity(kernel, 42)
        self.assertEqual(18, function.call_args.args[1])
        self.assertEqual(24, function.call_args.args[3])


    def test_snapshot_root_uses_pinned_identity_not_python_stat_representation(self):
        from agent_harness import core
        from agent_harness.file_io import ParentDirectory
        import tempfile
        with tempfile.TemporaryDirectory() as temporary:
            expected = {"identity_kind": "windows-file-id-info-v1", "device": "123456789abcdef0",
                        "inode": "000102030405060708090a0b0c0d0e0f"}
            with patch.object(ParentDirectory, 'physical_identity', return_value=expected):
                result = core.snapshot_python({'cwd': temporary})
            self.assertEqual(expected, result['root_binding'])
            self.assertEqual(core.digest({key:value for key,value in result.items() if key!='digest'}), result['digest'])


    def test_diagnostic_byte_and_inventory_bounds_remain_explicit(self):
        expected = {f"field-{i}": "猫" * 10000 for i in range(100)}
        observed = {f"field-{i}": "犬" * 10000 for i in range(100)}
        result = check.snapshot_difference(expected, observed)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=True, separators=(",", ":")).encode()), 4096)
        self.assertTrue(result["truncated"])
        bounded = check.snapshot_difference({"rows": list(range(100))}, {"rows": list(range(100))}, max_nodes=5)
        self.assertTrue(bounded["truncated"])
        self.assertLessEqual(bounded["nodes_examined"], 5)

    def test_mismatch_still_fails_and_clean_parity_still_passes(self):
        expected = {"root_binding": {"device": 1}}
        with patch.object(check, "snapshot_python", return_value=expected), patch.object(check, "invoke", return_value={"root_binding": {"device": 2}}):
            with self.assertRaisesRegex(AssertionError, 'root_binding/device'):
                check.parity({})
        with patch.object(check, "snapshot_python", return_value=expected), patch.object(check, "invoke", return_value=expected):
            self.assertEqual(expected, check.parity({}))
