"""Atomic JSON readers retain a generation without denying Windows replacement."""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import errno
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness import core


class JSONStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="json-state-", dir=os.environ.get("HARNESS_TEST_TMP"))
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state-тест.json"

    def test_held_reader_keeps_old_generation_during_atomic_replacement(self):
        old = {"generation": "old", "payload": "previous" * 4096}
        new = {"generation": "new", "payload": "replacement" * 4096}
        core.atomic_json(self.path, old)
        with core._open_json_reader(self.path) as reader:
            self.assertFalse(os.get_inheritable(reader.fileno()))
            # Do not read or prebuffer the old file before replacing its name.
            core.atomic_json(self.path, new)
            self.assertEqual(json.load(reader), old)
            self.assertEqual(core.read_json(self.path), new)
        self.assertTrue(reader.closed)

    def test_utf8_bom_and_universal_newlines_remain_supported(self):
        text = '{\r\n  "message": "Привет",\r\n  "line": "a\\nb"\r\n}\r\n'
        self.path.write_bytes(text.encode("utf-8-sig"))
        self.assertEqual(core.read_json(self.path), {"message": "Привет", "line": "a\nb"})
        with core._open_json_reader(self.path) as reader:
            self.assertEqual(reader.read(), text.replace("\r\n", "\n"))

    def test_missing_file_retains_filename_and_error_type(self):
        with self.assertRaises(FileNotFoundError) as caught:
            core.read_json(self.path)
        self.assertEqual(caught.exception.filename, str(self.path))
        if os.name == "nt":
            self.assertEqual(caught.exception.winerror, 2)

    def test_invalid_json_and_invalid_utf8_are_not_suppressed(self):
        for data, error in ((b'{"unfinished":', json.JSONDecodeError), (b'"\xff"', UnicodeDecodeError)):
            with self.subTest(error=error.__name__):
                self.path.write_bytes(data)
                with self.assertRaises(error):
                    core.read_json(self.path)

    def test_embedded_nul_and_bytes_paths_do_not_open_a_different_file(self):
        core.atomic_json(self.path, {"unchanged": True})
        with self.assertRaises(ValueError):
            core.read_json(str(self.path) + "\0ignored")
        with self.assertRaises(TypeError):
            core.read_json(os.fsencode(self.path))
        self.assertEqual(core.read_json(self.path), {"unchanged": True})


class WindowsJSONReaderContractTests(unittest.TestCase):
    """Portable WinAPI/CRT contract checks, not native Windows evidence."""

    @contextmanager
    def native_api(self, text='{"value": 1}'):
        handle, fd = 0x123456789ABC, 31
        kernel = SimpleNamespace(CreateFileW=Mock(return_value=handle), CloseHandle=Mock(return_value=True))
        crt = SimpleNamespace(open_osfhandle=Mock(return_value=fd))
        stream = io.StringIO(text)
        operating_system = SimpleNamespace(name="nt", O_RDONLY=0, O_BINARY=0x8000, O_NOINHERIT=0x80,
                                           fdopen=Mock(return_value=stream), close=Mock())
        denied = PermissionError(errno.EACCES, "fixture access denied")
        denied.winerror = 5
        foreign = SimpleNamespace(WinDLL=Mock(return_value=kernel), get_last_error=Mock(return_value=5),
                                  WinError=Mock(return_value=denied))
        fixture = SimpleNamespace(kernel=kernel, crt=crt, stream=stream, os=operating_system,
                                  ctypes=foreign, handle=handle, fd=fd, denied=denied)
        with patch.object(core, "os", operating_system), patch.object(core, "ctypes", foreign), \
                patch.dict(sys.modules, {"msvcrt": crt}):
            yield fixture

    def test_typed_shared_noninheritable_read_and_single_fd_owner(self):
        with self.native_api() as api:
            self.assertEqual(core.read_json("state.json"), {"value": 1})
        api.ctypes.WinDLL.assert_called_once_with("kernel32", use_last_error=True)
        api.kernel.CreateFileW.assert_called_once_with("state.json", 0x80000000, 7, None, 3, 0x80, None)
        self.assertEqual(api.kernel.CreateFileW.argtypes,
                         [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                          wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE])
        self.assertIs(api.kernel.CreateFileW.restype, wintypes.HANDLE)
        self.assertEqual(api.kernel.CloseHandle.argtypes, [wintypes.HANDLE])
        self.assertIs(api.kernel.CloseHandle.restype, wintypes.BOOL)
        api.crt.open_osfhandle.assert_called_once_with(api.handle, 0x8080)
        api.os.fdopen.assert_called_once_with(api.fd, "r", encoding="utf-8-sig", closefd=False)
        api.os.close.assert_called_once_with(api.fd)
        api.kernel.CloseHandle.assert_not_called()
        self.assertTrue(api.stream.closed)

    def test_invalid_handle_preserves_native_error_without_conversion_or_close(self):
        with self.native_api() as api:
            api.kernel.CreateFileW.return_value = wintypes.HANDLE(-1).value
            with self.assertRaises(PermissionError) as caught:
                core.read_json("denied.json")
        self.assertIs(caught.exception, api.denied)
        self.assertEqual((caught.exception.winerror, caught.exception.filename), (5, "denied.json"))
        api.ctypes.get_last_error.assert_called_once_with()
        api.ctypes.WinError.assert_called_once_with(5)
        api.crt.open_osfhandle.assert_not_called()
        api.kernel.CloseHandle.assert_not_called()
        api.os.close.assert_not_called()

    def test_failed_crt_transfer_closes_only_the_native_handle(self):
        for error in (OSError(errno.EMFILE, "fixture fd exhaustion"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__), self.native_api() as api:
                api.crt.open_osfhandle.side_effect = error
                with self.assertRaises(type(error)) as caught:
                    core.read_json("state.json")
                self.assertIs(caught.exception, error)
                api.kernel.CloseHandle.assert_called_once_with(api.handle)
                api.os.fdopen.assert_not_called()
                api.os.close.assert_not_called()

    def test_failed_text_wrapper_closes_only_the_owned_fd(self):
        with self.native_api() as api:
            api.os.fdopen.side_effect = RuntimeError("fixture wrapper failure")
            with self.assertRaisesRegex(RuntimeError, "wrapper failure"):
                core.read_json("state.json")
        api.os.close.assert_called_once_with(api.fd)
        api.kernel.CloseHandle.assert_not_called()

    def test_json_failure_closes_text_wrapper_and_fd_once(self):
        with self.native_api('{"unfinished":') as api:
            with self.assertRaises(json.JSONDecodeError):
                core.read_json("state.json")
        self.assertTrue(api.stream.closed)
        api.os.close.assert_called_once_with(api.fd)
        api.kernel.CloseHandle.assert_not_called()

    def test_text_close_failure_still_closes_the_fd_once(self):
        with self.native_api() as api:
            with patch.object(api.stream, "close", side_effect=OSError("fixture close failure")):
                with self.assertRaisesRegex(OSError, "close failure"):
                    core.read_json("state.json")
            api.stream.close()
        api.os.close.assert_called_once_with(api.fd)
        api.kernel.CloseHandle.assert_not_called()

    def test_embedded_nul_is_rejected_before_calling_native_api(self):
        with self.native_api() as api:
            with self.assertRaises(ValueError):
                core.read_json("state.json\0other.json")
        api.ctypes.WinDLL.assert_not_called()
        api.kernel.CreateFileW.assert_not_called()
        api.crt.open_osfhandle.assert_not_called()


class WindowsAtomicRenameContractTests(unittest.TestCase):
    """Portable checks of Win32 FileRenameInfoEx layout and failure handling."""

    @contextmanager
    def native_api(self):
        captured = {}

        def renamed(handle, information_class, payload, size):
            captured.update(handle=handle, information_class=information_class,
                            raw=ctypes.string_at(payload, size), size=size)
            return True

        kernel = SimpleNamespace(CreateFileW=Mock(return_value=0x123456789ABC),
                                 SetFileInformationByHandle=Mock(side_effect=renamed),
                                 CloseHandle=Mock(return_value=True))
        denied = PermissionError(errno.EACCES, "fixture rename denied")
        denied.winerror = 5
        with patch.object(core.ctypes, "WinDLL", return_value=kernel, create=True), \
                patch.object(core.ctypes, "WinError", return_value=denied, create=True), \
                patch.object(core.ctypes, "get_last_error", return_value=5, create=True):
            yield SimpleNamespace(kernel=kernel, captured=captured, denied=denied)

    def test_posix_rename_flags_preserve_open_generations_and_utf16_name(self):
        source = Path("state.tmp")
        target = Path("state-тест.json")
        name = str(target.absolute()).encode("utf-16-le")
        with self.native_api() as api:
            core._replace_json_windows(source, target)
        api.kernel.CreateFileW.assert_called_once_with("state.tmp", 0x10000, 7,
                                                       None, 3, 0x200080, None)
        self.assertEqual(api.captured["handle"], 0x123456789ABC)
        self.assertEqual(api.captured["information_class"], 22)
        raw = api.captured["raw"]
        pointer_width = ctypes.sizeof(ctypes.c_void_p)
        offset = ((4 + pointer_width - 1) // pointer_width) * pointer_width + pointer_width + 4
        self.assertEqual(int.from_bytes(raw[:4], "little"), 3)  # REPLACE + POSIX
        self.assertEqual(int.from_bytes(raw[offset - 4:offset], "little"), len(name))
        self.assertEqual(raw[offset:offset + len(name)], name)
        api.kernel.CloseHandle.assert_called_once_with(0x123456789ABC)

    def test_true_permission_failure_never_uses_legacy_fallback(self):
        source, target = Path("state.tmp"), Path("state.json")
        with self.native_api() as api, patch.object(core.os, "replace") as legacy:
            api.kernel.SetFileInformationByHandle.side_effect = None
            api.kernel.SetFileInformationByHandle.return_value = False
            with self.assertRaises(PermissionError) as caught:
                core._replace_json_windows(source, target)
            legacy.assert_not_called()
        self.assertIs(caught.exception, api.denied)
        self.assertEqual(caught.exception.filename, str(source))
        self.assertEqual(caught.exception.filename2, str(target.absolute()))
        api.kernel.CloseHandle.assert_called_once_with(0x123456789ABC)

    def test_explicitly_unsupported_class_only_can_fall_back(self):
        source, target = Path("state.tmp"), Path("state.json")
        with self.native_api() as api, patch.object(core.os, "replace") as legacy, \
                patch.object(core.ctypes, "get_last_error", return_value=87, create=True):
            api.kernel.SetFileInformationByHandle.side_effect = None
            api.kernel.SetFileInformationByHandle.return_value = False
            core._replace_json_windows(source, target)
            legacy.assert_called_once_with(source, target)
        api.kernel.CloseHandle.assert_called_once_with(0x123456789ABC)


@unittest.skipUnless(os.name == "nt", "native Windows file-sharing and HANDLE checks")
class NativeWindowsJSONReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="json-handle-", dir=os.environ.get("HARNESS_TEST_TMP"))
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.GetHandleInformation.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.GetHandleInformation.restype = wintypes.BOOL

    def assert_handle_closed(self, handle):
        flags = wintypes.DWORD()
        self.assertFalse(self.kernel.GetHandleInformation(handle, ctypes.byref(flags)))
        self.assertEqual(ctypes.get_last_error(), 6)  # ERROR_INVALID_HANDLE

    def test_native_handle_is_noninheritable_and_closed_after_reader(self):
        import msvcrt
        core.atomic_json(self.path, {"value": 1})
        with core._open_json_reader(self.path) as reader:
            handle = msvcrt.get_osfhandle(reader.fileno())
            flags = wintypes.DWORD()
            self.assertTrue(self.kernel.GetHandleInformation(handle, ctypes.byref(flags)))
            self.assertEqual(flags.value & 1, 0)  # HANDLE_FLAG_INHERIT
            self.assertFalse(os.get_inheritable(reader.fileno()))
        self.assert_handle_closed(handle)

    def test_native_handle_closes_when_json_parsing_raises(self):
        import msvcrt
        self.path.write_bytes(b'{"unfinished":')
        original, handles = core._open_json_reader, []

        @contextmanager
        def observed(path):
            with original(path) as reader:
                handles.append(msvcrt.get_osfhandle(reader.fileno()))
                yield reader

        with patch.object(core, "_open_json_reader", side_effect=observed):
            with self.assertRaises(json.JSONDecodeError):
                core.read_json(self.path)
        self.assertEqual(len(handles), 1)
        self.assert_handle_closed(handles[0])

    def test_plain_text_reader_is_a_negative_control_for_atomic_replacement(self):
        old = {"generation": "old"}
        core.atomic_json(self.path, old)
        with self.path.open("r", encoding="utf-8-sig") as reader:
            # Win32 POSIX replacement does not override incompatible sharing
            # modes. The harness reader shares DELETE; Python's plain open does
            # not. A blocked replacement must preserve the old generation.
            with self.assertRaises(PermissionError) as caught:
                core.atomic_json(self.path, {"generation": "new"})
            self.assertIn(caught.exception.winerror, (5, 32))
            self.assertEqual(json.load(reader), old)
        self.assertEqual(core.read_json(self.path), old)
        self.assertEqual(list(self.path.parent.glob(self.path.name + ".tmp-*")), [])

    def test_native_access_denied_retains_winerror_and_filename(self):
        # Opening a directory as an ordinary file requires unsupported access;
        # the reader must not turn a real CreateFileW denial into a retry/pass.
        with self.assertRaises(PermissionError) as caught:
            core.read_json(self.path.parent)
        self.assertEqual((caught.exception.winerror, caught.exception.filename), (5, str(self.path.parent)))


if __name__ == "__main__":
    unittest.main()
