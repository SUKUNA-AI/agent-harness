"""Linux/Windows Tokio LSP transport; Python retains semantic decisions.

On Linux a helper remains an UNREAPED SID/PGID leader until cleanup. Do not
replace the WNOWAIT observation with Popen.poll/wait: that would allow PGID reuse
and lose children after a helper crash. This is cooperative containment, not an
OS sandbox against deliberately escaping servers. Windows retains a private
Job handle, assigns the suspended helper before execution, and verifies an
empty Job before reporting cleanup.
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import platform
import queue
import signal
import struct
import subprocess
import threading
import time

from . import __version__
from .core import HarnessError
from .native import _stamp, _strict_json, find_binary

MAX_FRAME = 16 * 1024 * 1024
MAX_WIRE = 2 * MAX_FRAME + 4096
MAX_WIRE_STREAM = 256 * 1024 * 1024
MAX_PACKETS = 40000
MAX_RPC_NODES = 1000000
LIMITS = {"max_lsp_frame_bytes": MAX_FRAME, "max_header_bytes": 8192,
          "max_lsp_stream_bytes": 64 * 1024 * 1024, "max_lsp_frames": 20000,
          "max_wire_frame_bytes": MAX_WIRE, "max_control_bytes": 1024 * 1024,
          "max_wire_stream_bytes": MAX_WIRE_STREAM, "max_wire_packets": MAX_PACKETS,
          "max_rpc_nodes": MAX_RPC_NODES, "queue_slots": 4,
          "queue_decoded_bytes": MAX_FRAME, "input_metadata_reserve_bytes":4096, "stderr_chunk_bytes": 4096,
          "stderr_tail_bytes": 65536}


def _default_reaping():
    """Reject autoreaping/custom SIGCHLD before creating the containment anchor."""
    if signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL:
        raise HarnessError("Native LSP requires the default SIGCHLD disposition")
    # Linux x86_64 glibc sigaction ABI. Other platforms are not silently guessed.
    if platform.machine().lower() not in {"x86_64", "amd64"} or platform.libc_ver()[0] != "glibc":
        raise HarnessError("Native LSP reaping capability is not qualified on this platform")
    class Action(ctypes.Structure):
        _fields_ = [("handler", ctypes.c_void_p), ("mask", ctypes.c_ulong * 16),
                    ("flags", ctypes.c_int), ("restorer", ctypes.c_void_p)]
    action = Action()
    libc = ctypes.CDLL(None, use_errno=True)
    libc.sigaction.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(Action)]
    libc.sigaction.restype = ctypes.c_int
    if libc.sigaction(signal.SIGCHLD, None, ctypes.byref(action)) != 0 or action.handler or action.flags & 2:
        raise HarnessError("Native LSP cannot retain an unreaped helper with this SIGCHLD policy")


def _identity(pid):
    raw = Path(f"/proc/{pid}/stat").read_bytes()
    if len(raw) > 8192 or int(raw.split(b" ", 1)[0]) != pid:
        raise HarnessError("Native LSP process identity is unavailable")
    fields = raw.rsplit(b")", 1)[1].split()
    return {"pid": pid, "created": fields[19].decode("ascii"),
            "pgid": int(fields[2]), "sid": int(fields[3]), "state": fields[0].decode("ascii")}


def select_backend(requested):
    """Independent LSP policy; a present helper must pass its live handshake."""
    if not isinstance(requested, str) or requested not in {"auto", "python", "native"}:
        raise HarnessError("AGENT_HARNESS_LSP_BACKEND must be auto, python or native")
    if requested != "auto":
        return requested
    if not (sys_platform_linux() or os.name == "nt"):
        return "python"
    return "native" if find_binary() is not None else "python"


class _WindowsTree:
    """Retain a private Job until all contained processes have stopped."""
    def __init__(self, process):
        from .runner import WindowsJob
        from ctypes import wintypes
        self.job = WindowsJob(process)
        self.job.kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.job.kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                             ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]

    def resume(self, process):
        self.job.resume_suspended_process(process)

    def stop(self):
        class Accounting(ctypes.Structure):
            _fields_ = [(name, ctypes.c_int64) for name in ("user", "kernel", "period_user", "period_kernel")] + [
                (name, ctypes.c_uint32) for name in ("faults", "total", "active", "terminated")]
        handle = self.job.handle
        if not handle:
            return False
        try:
            if not self.job.kernel.TerminateJobObject(handle, 1):
                return False
            deadline = time.monotonic() + 1
            while True:
                info = Accounting()
                if not self.job.kernel.QueryInformationJobObject(handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
                    return False
                if info.active == 0:
                    return True
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.01)
        finally:
            self.job.close()


def _rpc(body):
    if not isinstance(body, str) or len(body.encode("utf-8")) > MAX_FRAME:
        raise HarnessError("Invalid native LSP message body")
    value = _strict_json(body)
    if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
        raise HarnessError("Invalid native LSP JSON-RPC")
    if "id" in value:
        identifier = value["id"]
        valid = type(identifier) is int or (type(identifier) is str and len(identifier.encode()) <= 1024)
        valid = valid or (identifier is None and "method" not in value and "error" in value)
        if not valid:
            raise HarnessError("Invalid native LSP JSON-RPC ID")
    if "method" in value:
        if not isinstance(value["method"], str) or not value["method"] or "result" in value or "error" in value:
            raise HarnessError("Invalid native LSP JSON-RPC request")
    elif "id" not in value or ("result" in value) == ("error" in value):
        raise HarnessError("Invalid native LSP JSON-RPC response")
    # A total node budget also bounds queued Python DOM amplification.
    stack, nodes = [value], 0
    while stack:
        item = stack.pop()
        nodes += 1
        if nodes > MAX_RPC_NODES:
            raise HarnessError("Native LSP JSON node budget exceeded")
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return value, nodes


class NativeTransport:
    """One bounded helper and one owned language-server tree per Session."""
    def __init__(self, root, argv, deadline, on_message):
        if not (sys_platform_linux() or os.name == "nt"):
            raise HarnessError("Native LSP transport is not qualified on this platform; select python")
        if os.name != "nt":
            _default_reaping()
        import uuid
        self.session_id = uuid.uuid4().hex
        self.root, self.argv, self.deadline, self.on_message = root, argv, deadline, on_message
        self._helper = None
        self._windows_tree = None
        self.helper_identity = self.child_identity = None
        self.child_pid = None
        self.stderr_tail, self.stderr_bytes = bytearray(), 0
        self.terminal = None
        self.error = None
        self.closing = False
        self.cleanup_complete = False
        self.session_complete = False
        self._ready, self._closed, self._stop = threading.Event(), threading.Event(), threading.Event()
        self._outgoing = queue.Queue(maxsize=4)
        self._condition = threading.Condition()
        self._queued_bytes = 0
        self._lifecycle = threading.RLock()
        self._error_lock = threading.Lock()
        self._sequence = 0
        self._sent_bytes = self._sent_packets = 0
        self._received_bytes = self._received_packets = 0
        self._rpc_nodes = 0
        self._killed = self._reaped = False
        self._start_requested = self._cancelled = self._intentional_kill = False
        self._threads = []

    def start(self):
        binary = find_binary(required=True)
        before = _stamp(binary)
        self._helper = subprocess.Popen([str(binary), "--lsp-session", self.session_id], cwd=self.root,
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        bufsize=0, shell=False, start_new_session=os.name != "nt", close_fds=True,
                                        creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000004) if os.name == "nt" else 0)
        try:
            if os.name == "nt":
                self._windows_tree = _WindowsTree(self._helper)
                self.helper_identity = {"pid": self._helper.pid, "created": self.session_id, "pgid": None, "sid": None}
                self._windows_tree.resume(self._helper)
            else:
                self.helper_identity = _identity(self._helper.pid)
                if (self.helper_identity["pgid"], self.helper_identity["sid"]) != (self._helper.pid, self._helper.pid):
                    raise HarnessError("Native LSP helper is not its owned SID/PGID leader")
            for target in (self._read, self._write, self._watch):
                thread = threading.Thread(target=target, daemon=True)
                self._threads.append(thread)
                thread.start()
            remaining = min(2.0, self.deadline - time.monotonic())
            if remaining <= 0 or not self._ready.wait(remaining) or self.error:
                raise self.error or HarnessError("Native LSP ready handshake timed out")
            if _stamp(binary) != before:
                raise HarnessError("Native LSP executable changed during handshake")
            timeout_ms = int((self.deadline - time.monotonic()) * 1000)
            if not 0 < timeout_ms <= 120000:
                raise HarnessError("Native LSP start deadline expired")
            self._start_requested = True
            self._enqueue("start", {"argv": self.argv, "cwd": str(self.root), "timeout_ms": timeout_ms},
                          1, self.deadline)
            return self
        except BaseException as error:
            self.closing = True
            if not self._start_requested:
                # Before a validated start packet there is no server. The
                # unreaped direct child is still ours even if /proc birth
                # observation failed. Signal it directly, never an unknown
                # numeric process group and never Popen.kill (which polls).
                if os.name == "nt":
                    if self._windows_tree is not None:
                        self._windows_tree.stop()
                    else:
                        self._helper.kill()  # Still suspended: no user-mode descendants exist.
                else:
                    os.kill(self._helper.pid,signal.SIGKILL)
                self._killed = True
                self._stop.set()
                self._helper.wait(timeout=2)
                self._reaped = self.cleanup_complete = True
                for stream in (self._helper.stdin,self._helper.stdout):
                    stream.close()
            else:
                self.close(grace=0)
            if isinstance(error,HarnessError):
                raise
            raise HarnessError("Native LSP startup capability/identity unavailable") from error

    def _fail(self, error):
        with self._error_lock:
            if self.error is not None:
                return
            self.error = error if isinstance(error, HarnessError) else HarnessError("Native LSP transport failed")
        self._ready.set()
        # Kill before posting: the semantic consumer can itself be backpressured.
        try:
            self._kill_owned()
        except (OSError, HarnessError):
            pass
        if not self.closing:
            try:
                self.on_message(self.error)
            except BaseException:
                pass  # Delivery failure is already recorded; group was killed.

    def _kill_owned(self):
        with self._lifecycle:
            if self._helper is None or self._killed or self._reaped:
                return
            if os.name == "nt":
                self.cleanup_complete = self._windows_tree.stop()
                self._killed = True
                with self._condition:
                    self._condition.notify_all()
                if not self.cleanup_complete:
                    raise HarnessError("Native LSP process job cleanup is unknown")
                return
            pid = self._helper.pid
            try:
                current = _identity(pid)  # Deliberately accepts a zombie anchor.
            except (OSError,ValueError,IndexError) as error:
                raise HarnessError("Native LSP helper identity unavailable; cleanup is unknown") from error
            if self.helper_identity and current["created"] != self.helper_identity["created"]:
                raise HarnessError("Native LSP helper birth identity changed; cleanup is unknown")
            if current["pgid"] != pid or current["sid"] != pid or os.getpgid(pid) != pid or os.getsid(pid) != pid:
                raise HarnessError("Native LSP containment identity changed; cleanup is unknown")
            os.killpg(pid, signal.SIGKILL)
            self._killed = True
            with self._condition:
                self._condition.notify_all()

    def _watch(self):
        try:
            while not self._closed.wait(0.05):
                if self._stop.is_set():
                    return
                # WNOWAIT observes exit without releasing the group-ID anchor.
                exited = (self._helper.poll() is not None) if os.name == "nt" else (
                    os.waitid(os.P_PID, self._helper.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None)
                if exited or time.monotonic() >= self.deadline:
                    self._kill_owned()
                    if not self.closing:
                        self._fail(HarnessError("Native LSP helper exited or session timed out"))
                    return
        except (OSError, ValueError, HarnessError):
            self._fail(HarnessError("Native LSP helper lifecycle observation failed"))

    def _enqueue(self, kind, payload, size, deadline):
        if self.error or self._closed.is_set() or self._killed:
            raise self.error or HarnessError("Native LSP transport is closed")
        with self._condition:
            while self._queued_bytes + size > MAX_FRAME+4096:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._killed:
                    raise HarnessError("Native LSP request queue timed out")
                self._condition.wait(min(remaining, 0.1))
            self._queued_bytes += size
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HarnessError("Native LSP session timed out")
            while True:
                if self._killed or self._stop.is_set() or self.error:
                    raise self.error or HarnessError("Native LSP transport stopped while queuing input")
                remaining=deadline-time.monotonic()
                if remaining<=0:
                    raise HarnessError("Native LSP request queue timed out")
                try:
                    self._outgoing.put((kind,payload,size),timeout=min(remaining,0.05))
                    break
                except queue.Full:
                    continue
        except BaseException as error:
            with self._condition:
                self._queued_bytes -= size
                self._condition.notify_all()
            if isinstance(error,queue.Full):
                raise HarnessError("Native LSP request queue timed out") from None
            raise

    def send(self, body, deadline):
        # Body is the exact UTF-8 serialization produced by the semantic layer.
        if len(body) > MAX_FRAME:
            raise HarnessError("Native LSP request exceeds the frame limit")
        try:
            text=body.decode("utf-8")
        except UnicodeDecodeError as error:
            raise HarnessError("Native LSP request requires UTF-8") from error
        self._enqueue("message", {"body": text}, max(1, len(body)), deadline)

    def _write(self):
        try:
            while not self._stop.is_set():
                try:
                    kind, payload, size = self._outgoing.get(timeout=0.1)
                except queue.Empty:
                    continue
                try:
                    self._sequence += 1
                    value = {"protocol_version": 1, "session_id": self.session_id,
                             "sequence": self._sequence, "kind": kind, **payload}
                    body = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
                    self._sent_bytes += len(body) + 4
                    self._sent_packets += 1
                    if (len(body) > MAX_WIRE or self._sent_bytes > MAX_WIRE_STREAM
                            or self._sent_packets > MAX_PACKETS):
                        raise HarnessError("Native LSP control stream exceeded its byte budget")
                    view = memoryview(struct.pack(">I", len(body)) + body)
                    while view and not self._stop.is_set():
                        written = self._helper.stdin.write(view)
                        if not written:
                            raise HarnessError("Native LSP helper stopped reading")
                        view = view[written:]
                finally:
                    with self._condition:
                        self._queued_bytes -= size
                        self._condition.notify_all()
        except (OSError, ValueError, HarnessError):
            if not self._intentional_kill and not self._cancelled:
                self._fail(HarnessError("Native LSP control write failed"))

    def _read_exact(self, count):
        parts, used = [], 0
        while used < count:
            block = self._helper.stdout.read(count - used)
            if not block:
                raise EOFError()
            parts.append(block)
            used += len(block)
        return b"".join(parts)

    def _read(self):
        sequence = 0
        try:
            while not self._stop.is_set():
                length = struct.unpack(">I", self._read_exact(4))[0]
                if not 0 < length <= MAX_WIRE:
                    raise HarnessError("Invalid native LSP frame length")
                self._received_bytes += length + 4
                self._received_packets += 1
                if self._received_bytes > MAX_WIRE_STREAM or self._received_packets > MAX_PACKETS:
                    raise HarnessError("Native LSP response stream exceeded its byte budget")
                value = _strict_json(self._read_exact(length).decode("utf-8"))
                sequence += 1
                if (not isinstance(value, dict) or type(value.get("protocol_version")) is not int
                        or value["protocol_version"] != 1 or value.get("session_id") != self.session_id
                        or type(value.get("sequence")) is not int or value["sequence"] != sequence):
                    raise HarnessError("Native LSP stream binding conflict")
                common = {"protocol_version", "session_id", "sequence", "kind"}
                kind = value.get("kind")
                if kind == "ready":
                    group = None if os.name == "nt" else self._helper.pid
                    if (sequence != 1 or set(value) != common | {"helper_pid", "pgid", "sid", "created", "version", "platform", "limits"}
                            or value["helper_pid"] != self._helper.pid or value["pgid"] != group
                            or value["sid"] != group or value["created"] != self.helper_identity["created"]
                            or value["version"] != __version__ or value["platform"] != ("windows" if os.name == "nt" else "linux") or value["limits"] != LIMITS):
                        raise HarnessError("Native LSP ready capability mismatch")
                    self._ready.set()
                elif kind == "process_started":
                    if (set(value) != common | {"child_pid", "pgid"} or self.child_pid is not None
                            or type(value["child_pid"]) is not int or value["child_pid"] <= 1
                            or value["child_pid"] == self._helper.pid or value["pgid"] != (None if os.name == "nt" else self._helper.pid)):
                        raise HarnessError("Invalid native LSP child identity")
                    self.child_pid = value["child_pid"]
                    if os.name != "nt":
                        try:
                            self.child_identity = _identity(self.child_pid)
                        except (OSError, ValueError, HarnessError):
                            self.child_identity = None
                elif kind == "message":
                    if set(value) != common | {"body"} or self.child_pid is None:
                        raise HarnessError("Invalid native LSP message envelope")
                    message, nodes = _rpc(value["body"])
                    self._rpc_nodes += nodes
                    if self._rpc_nodes > MAX_RPC_NODES:
                        raise HarnessError("Native LSP session JSON node budget exceeded")
                    try:
                        self.on_message(message)
                    except BaseException as error:
                        raise HarnessError("Native LSP semantic delivery failed") from error
                elif kind == "stderr":
                    if (set(value) != common | {"hex"} or not isinstance(value["hex"], str)
                            or len(value["hex"]) > 8192 or len(value["hex"]) % 2
                            or any(char not in "0123456789abcdef" for char in value["hex"])):
                        raise HarnessError("Invalid native LSP stderr chunk")
                    data = bytes.fromhex(value["hex"])
                    self.stderr_bytes += len(data)
                    self.stderr_tail.extend(data)
                    del self.stderr_tail[:-65536]
                elif kind == "terminal":
                    if (set(value) != common | {"reason", "child_exit_code", "stdout_complete", "stderr_complete", "process_tree_stopped"}
                            or self.terminal is not None or value["process_tree_stopped"] is not None
                            or type(value["stdout_complete"]) is not bool or type(value["stderr_complete"]) is not bool
                            or not isinstance(value["reason"], str)
                            or (value["child_exit_code"] is not None and type(value["child_exit_code"]) is not int)):
                        raise HarnessError("Invalid native LSP terminal facts")
                    self.terminal = value
                else:
                    raise HarnessError("Unknown native LSP event")
        except EOFError:
            try:
                self._kill_owned()
            except (OSError, HarnessError):
                self.error = HarnessError("Native LSP cleanup is unknown")
            if not self.closing:
                self._fail(HarnessError("Native LSP helper closed its event stream"))
        except BaseException:
            self._fail(HarnessError("Native LSP event stream failed validation"))
        finally:
            self._closed.set()
            self._ready.set()

    def cancel(self):
        # Out-of-band cancellation cannot be trapped behind a full data queue.
        self._cancelled = True
        self._kill_owned()

    def _stopped_group(self):
        if os.name == "nt":
            return self.cleanup_complete
        pid = self._helper.pid
        deadline = time.monotonic() + 1
        while True:
            live = []
            for count,entry in enumerate(Path("/proc").iterdir()):
                if count>65536:
                    return False
                if not entry.name.isdigit():
                    continue
                try:
                    item = _identity(int(entry.name))
                except (OSError, ValueError, HarnessError):
                    continue
                if item["pgid"] == pid and item["state"] not in {"Z", "X"}:
                    live.append(item["pid"])
            if not live:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)

    def close(self, grace=0.3,require_terminal=False):
        self.closing = True
        if self._helper is None or self._reaped:
            if require_terminal and not self.session_complete:
                raise HarnessError("Native LSP Session did not complete its terminal protocol")
            return
        self._closed.wait(max(0, grace))
        self._intentional_kill = True
        self._kill_owned()  # MUST precede every wait/reap, including a zombie.
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
        self.cleanup_complete = self._stopped_group()
        self._helper.wait(timeout=2)
        self._reaped = True
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=0.5)
        while True:
            try:
                _,_,size=self._outgoing.get_nowait()
            except queue.Empty:
                break
            with self._condition:
                self._queued_bytes-=size
                self._condition.notify_all()
        for stream in (self._helper.stdin, self._helper.stdout):
            if stream is not None:
                stream.close()
        if not self.cleanup_complete:
            raise HarnessError("Native LSP process group cleanup is unknown")
        self.session_complete=bool(self.error is None and self.terminal is not None
            and self.terminal["reason"]=="child_exited" and self.terminal["child_exit_code"]==0
            and self.terminal["stdout_complete"] and self.terminal["stderr_complete"])
        if not self._cancelled and self.error is not None:
            raise self.error
        if (not self._cancelled and self.terminal is not None
                and (self.terminal["reason"]!="child_exited" or self.terminal["child_exit_code"]!=0)):
            raise HarnessError("Native LSP server/transport failed: "+self.terminal["reason"][:100])
        if not self._cancelled and self.terminal is not None and not (self.terminal["stdout_complete"] and self.terminal["stderr_complete"]):
            raise HarnessError("Native LSP stream ended incompletely")
        if require_terminal and not self.session_complete:
            raise HarnessError("Native LSP Session terminal facts are missing or incomplete")


def sys_platform_linux():
    import sys
    return sys.platform.startswith("linux")
