"""Compiled Linux transport integration; Python semantic policy stays shared.

Set AGENT_HARNESS_TEST_NATIVE_LSP to the current compiled native executable.
Missing compiled capability is explicitly skipped in the portable unit suite;
required native qualification supplies it and runs these cases without skips.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_harness import lsp
from agent_harness.core import HarnessError, process_identity, identity_alive
from test_lsp import FAKE_SERVER

BINARY = os.environ.get("AGENT_HARNESS_TEST_NATIVE_LSP")


class NativeSelectionTests(unittest.TestCase):
    def test_default_and_explicit_backend_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {"AGENT_HARNESS_BACKEND": "native"}, clear=False):
                os.environ.pop("AGENT_HARNESS_LSP_BACKEND", None)
                self.assertEqual("python", lsp.Session(root, [sys.executable]).backend)
                self.assertEqual("native", lsp.Session(root, [sys.executable], backend="native").backend)
            with self.assertRaises(HarnessError):
                lsp.Session(root, [sys.executable], backend="unknown")


@unittest.skipUnless(BINARY and sys.platform.startswith("linux"), "Compiled Linux LSP capability not provided")
class NativeLSPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="native-lsp-test-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "workspace"
        self.root.mkdir()
        (self.root / "a.py").write_bytes('emoji = "😀"; target = 1\r\nvalue = target\r\n'.encode())
        (self.root / "b.py").write_bytes(b"from a import target\r\nother = target\r\n")
        self.receipt = self.base / "server.json"
        self.script = self.base / "server.py"
        marker = 'message=read(); method=message.get("method"); params=message.get("params") or {}'
        recorded = marker + '\n    record=json.loads(receipt.read_text()); record.setdefault("methods",[]).append(method); receipt.write_text(json.dumps(record))'
        self.script.write_text(FAKE_SERVER.replace(marker, recorded), encoding="utf-8")
        self.environment = patch.dict(os.environ, {"AGENT_HARNESS_NATIVE": str(Path(BINARY).resolve()),
                                                 "AGENT_HARNESS_LSP_BACKEND": "native"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def server(self, mode="normal"):
        return [sys.executable, "-I", "-B", str(self.script), str(self.root), mode, str(self.receipt)]

    def test_three_rpc_one_session_server_requests_notifications_and_cleanup(self):
        params = {"textDocument": {"uri": (self.root / "a.py").as_uri()}}
        with lsp.Session(self.root, self.server(), settings={"test": {"flag": True}}) as client:
            transport = client._native
            self.assertIsNone(client.process)  # Helper identity is not server identity.
            client.open({"path": "a.py", "text": (self.root / "a.py").read_bytes().decode()}, "python")
            symbols = client.request("textDocument/documentSymbol", params)
            definition = client.request("textDocument/definition", {**params, "position": {"line": 0, "character": 14}})
            references = client.request("textDocument/references", params)
            self.assertEqual("target", symbols[0]["name"])
            self.assertTrue(definition)
            self.assertTrue(references)
            self.assertNotEqual(transport._helper.pid, transport.child_pid)
            child = process_identity(transport.child_pid)
        record = json.loads(self.receipt.read_text())
        for method in ("initialize", "initialized", "textDocument/didOpen", "textDocument/documentSymbol",
                       "textDocument/definition", "textDocument/references", "shutdown", "exit"):
            self.assertIn(method, record["methods"])
        self.assertTrue(transport.cleanup_complete)
        self.assertTrue(client._native_shutdown_ack)
        self.assertTrue(client._native_exit_admitted)
        self.assertTrue(transport.session_complete)
        self.assertFalse(identity_alive(child))
        self.assertEqual(-9, transport._helper.returncode)

    def test_semantic_query_and_stderr_tail_keeps_262144_positive_control(self):
        result = lsp.query(self.root, "a.py", server=self.server("stderr"), settings={"test": {"flag": True}})
        self.assertEqual("target", result["items"][0]["name"])
        self.assertEqual(262144, result["stderr_bytes"])
        self.assertTrue(result["stderr_truncated"])

    def test_zero_exit_without_shutdown_reply_cannot_accept_query(self):
        from agent_harness.lsp_native import NativeTransport
        source=self.script.read_text().replace('elif method=="shutdown": respond(message,None)',
                                             'elif method=="shutdown": raise SystemExit(0)')
        self.script.write_text(source,encoding="utf-8")
        observed=[];original=NativeTransport.close
        def closing(transport,*args,**kwargs):
            observed.append(transport)
            return original(transport,*args,**kwargs)
        with patch.object(NativeTransport,"close",closing):
            with self.assertRaisesRegex(HarnessError,"shutdown"):
                lsp.query(self.root,"a.py",server=self.server(),settings={"test":{"flag":True}})
        transport=observed[-1]
        self.assertEqual(0,transport.terminal["child_exit_code"])
        self.assertTrue(transport.terminal["stdout_complete"])
        self.assertTrue(transport.cleanup_complete)
        self.assertFalse(transport.session_complete)
        self.assertIsNotNone(transport.error)

    def test_timeout_stops_server_tree(self):
        start = time.monotonic()
        with self.assertRaises(HarnessError):
            with lsp.Session(self.root, self.server("child_timeout"), timeout=0.4):
                self.fail("Timed-out initialize must not succeed")
        self.assertLess(time.monotonic() - start, 3)
        record = json.loads(self.receipt.read_text())
        self.assertIsNone(process_identity(record["pid"]))
        self.assertIsNone(process_identity(record["child"]))


FAULT_SERVER = r'''
import json,os,subprocess,sys,threading,time
from pathlib import Path
mode,receipt= sys.argv[1],Path(sys.argv[2])
child=None
if mode in {'sleep','flood','partial_descendant','devnull_descendant'}:
    stdio={} if mode=='partial_descendant' else {'stdin':subprocess.DEVNULL,'stdout':subprocess.DEVNULL,'stderr':subprocess.DEVNULL}
    child=subprocess.Popen([sys.executable,'-I','-c','import time;time.sleep(30)'],**stdio)
fds={}
for name in os.listdir('/proc/self/fd'):
    try:fds[name]=os.readlink('/proc/self/fd/'+name)
    except OSError:pass
record={'pid':os.getpid(),'child':child.pid if child else None,'pgid':os.getpgrp(),'sid':os.getsid(0),
        'argv':sys.argv[3:],'cwd':os.getcwd(),'env':os.environ.get('HARNESS_LSP_TEST_VALUE'),'fds':fds,'sent':0}
receipt.write_text(json.dumps(record))
def send(value):
    raw=json.dumps(value,ensure_ascii=False,separators=(',',':')).encode()
    frame=b'Content-Length: '+str(len(raw)).encode()+b'\r\n\r\n'+raw
    for n in range(0,len(frame),65536):sys.stdout.buffer.write(frame[n:n+65536]);sys.stdout.buffer.flush()
def read():
    headers={}
    while True:
        line=sys.stdin.buffer.readline()
        if not line:raise SystemExit()
        if line==b'\r\n':break
        key,value=line.split(b':',1);headers[key.lower()]=value.strip()
    size=int(headers[b'content-length']);body=bytearray()
    while len(body)<size:
        part=sys.stdin.buffer.read(size-len(body))
        if not part:raise SystemExit()
        body.extend(part)
    return json.loads(body)
if mode=='sleep':time.sleep(30);raise SystemExit()
if mode=='flood':
    def err():
        while True:sys.stderr.buffer.write(b'x'*4096);sys.stderr.buffer.flush()
    threading.Thread(target=err,daemon=True).start()
    while True:
        send({'jsonrpc':'2.0','method':'fixture/notification','params':{'data':'x'*1048576}})
        record['sent']+=1;receipt.write_text(json.dumps(record))
if mode=='devnull_descendant':raise SystemExit(0)
while True:
    message=read();method=message.get('method')
    if mode=='partial_descendant':
        sys.stdout.buffer.write(b'Content-Length: 999\r\n\r\n{"jsonrpc":');sys.stdout.buffer.flush();os._exit(7)
    if method=='shutdown':
        if mode=='no_shutdown_reply':os._exit(0)
        send({'jsonrpc':'2.0','id':message['id'],'result':None})
        if mode=='ignore_exit':time.sleep(30)
        continue
    if method=='exit':raise SystemExit()
    if method=='initialize':
        send({'jsonrpc':'2.0','id':message['id'],'result':{'capabilities':{},'serverInfo':{'name':'fault-fixture'}}});continue
    if 'id' in message:
        send({'jsonrpc':'2.0','id':message['id'] if mode!='wrong_id' else 999,'result':message.get('params')})
    if mode=='post_result_bad_frame' and 'id' in message:
        sys.stdout.buffer.write(b'Content-Length: 1\r\n\r\n{');sys.stdout.buffer.flush();time.sleep(30)
    if mode=='post_result_exit' and 'id' in message:os._exit(17)
'''


def wait_until(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("Fixture condition did not arrive before deadline")


class NativeLSPFixture:
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="native-lsp-fault-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "workspace with 雪"
        self.root.mkdir()
        self.script = self.base / "fault_server.py"
        self.script.write_text(FAULT_SERVER, encoding="utf-8")
        self.receipt = self.base / "server.json"
        self.environment = patch.dict(os.environ, {"AGENT_HARNESS_NATIVE": str(Path(BINARY).resolve()),
                                                 "AGENT_HARNESS_LSP_BACKEND": "native"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def argv(self, mode, *extra):
        return [sys.executable, "-I", "-B", str(self.script), mode, str(self.receipt), *extra]

    def record(self):
        try:
            return json.loads(self.receipt.read_text())
        except (FileNotFoundError, ValueError):
            return None

    def assert_stopped(self, record):
        for key in ("pid", "child"):
            if record.get(key):
                wait_until(lambda: process_identity(record[key]) is None)


@unittest.skipUnless(BINARY and sys.platform.startswith("linux"), "Compiled Linux LSP capability not provided")
class NativeLSPFaultTests(NativeLSPFixture,unittest.TestCase):
    def test_outer_exception_survives_missing_shutdown_ack_after_cleanup(self):
        class ApplicationError(Exception):pass
        cause=ApplicationError("original application cause")
        with self.assertRaises(ApplicationError) as observed:
            with lsp.Session(self.root,self.argv("no_shutdown_reply"),backend="native") as client:
                transport=client._native
                raise cause
        self.assertIs(cause,observed.exception)
        self.assertTrue(any("shutdown" in note for note in observed.exception.__notes__))
        self.assertTrue(transport.cleanup_complete)
        self.assertFalse(transport.session_complete)
        self.assertFalse(client._native_shutdown_ack)
        self.assertFalse(client._native_exit_admitted)

    def test_missing_terminal_after_shutdown_reply_is_not_session_completion(self):
        received=False
        with self.assertRaises(HarnessError):
            with lsp.Session(self.root,self.argv("ignore_exit"),backend="native",timeout=3) as client:
                transport=client._native
                self.assertEqual({"ok":True},client.request("fixture/echo",{"ok":True}))
                received=True
        self.assertTrue(received)
        self.assertTrue(transport.cleanup_complete)
        self.assertFalse(transport.session_complete)
        self.assertIsNone(transport.terminal)
        self.assert_stopped(wait_until(self.record))

    def test_wrong_response_id_remains_a_python_semantic_failure(self):
        with self.assertRaisesRegex(HarnessError,"Unexpected LSP response ID"):
            with lsp.Session(self.root,self.argv("wrong_id"),backend="native") as client:
                transport=client._native
                client.request("fixture/echo",{})
        self.assertTrue(transport.cleanup_complete)


    def test_post_result_protocol_failure_is_not_hidden_by_close(self):
        received=False
        with self.assertRaises(HarnessError):
            with lsp.Session(self.root, self.argv("post_result_bad_frame"), backend="native") as client:
                transport = client._native
                self.assertEqual({"ok": True}, client.request("fixture/echo", {"ok": True}))
                received=True
                wait_until(lambda: transport.error is not None)
        self.assertTrue(received,"The valid reply must precede the injected protocol failure")
        self.assertTrue(transport.cleanup_complete)
        self.assert_stopped(wait_until(self.record))

    def test_post_result_nonzero_server_exit_is_not_hidden_by_close(self):
        received=False
        with self.assertRaises(HarnessError):
            with lsp.Session(self.root,self.argv("post_result_exit"),backend="native") as client:
                transport=client._native
                self.assertEqual({"ok":True},client.request("fixture/echo",{"ok":True}))
                received=True
                wait_until(lambda:transport.terminal is not None)
        self.assertTrue(received)
        self.assertTrue(transport.cleanup_complete)

    def test_unavailable_birth_before_start_is_controlled_and_never_spawns_server(self):
        from agent_harness import lsp_native
        with patch.object(lsp_native, "_identity", side_effect=FileNotFoundError("injected unavailable birth")):
            with self.assertRaises(HarnessError):
                with lsp.Session(self.root, self.argv("sleep"), backend="native"):
                    self.fail("Unavailable birth identity cannot permit start")
        self.assertFalse(self.receipt.exists())

    def test_custom_sigchld_is_rejected_before_helper_creation(self):
        import signal
        from agent_harness import lsp_native
        prior = signal.getsignal(signal.SIGCHLD)
        try:
            for handler in (signal.SIG_IGN, lambda *_: None):
                signal.signal(signal.SIGCHLD, handler)
                with patch.object(lsp_native.subprocess, "Popen") as spawn:
                    with self.assertRaises(HarnessError):
                        lsp_native.NativeTransport(self.root, self.argv("sleep"), time.monotonic()+2, lambda _: None)
                    spawn.assert_not_called()
        finally:
            signal.signal(signal.SIGCHLD, prior)

    def test_kernel_sa_nocldwait_is_rejected_before_helper_creation(self):
        import ctypes
        import signal
        from agent_harness import lsp_native
        class Action(ctypes.Structure):
            _fields_=[("handler",ctypes.c_void_p),("mask",ctypes.c_ulong*16),
                      ("flags",ctypes.c_int),("restorer",ctypes.c_void_p)]
        libc=ctypes.CDLL(None,use_errno=True)
        libc.sigaction.argtypes=[ctypes.c_int,ctypes.POINTER(Action),ctypes.POINTER(Action)]
        libc.sigaction.restype=ctypes.c_int
        prior=Action()
        self.assertEqual(0,libc.sigaction(signal.SIGCHLD,None,ctypes.byref(prior)))
        changed=Action.from_buffer_copy(bytes(prior));changed.flags|=2
        try:
            self.assertEqual(0,libc.sigaction(signal.SIGCHLD,ctypes.byref(changed),None))
            with patch.object(lsp_native.subprocess,"Popen") as spawn:
                with self.assertRaises(HarnessError):
                    lsp_native.NativeTransport(self.root,self.argv("sleep"),time.monotonic()+2,lambda _:None)
                spawn.assert_not_called()
        finally:
            self.assertEqual(0,libc.sigaction(signal.SIGCHLD,ctypes.byref(prior),None))


@unittest.skipUnless(BINARY and sys.platform.startswith("linux"), "Compiled Linux LSP capability not provided")
class NativeLSPLifecycleTests(NativeLSPFixture,unittest.TestCase):
    def test_cancel_interrupts_a_live_blocked_write_before_overload(self):
        import queue
        from agent_harness import lsp_native
        incoming=queue.Queue()
        transport=lsp_native.NativeTransport(self.root,self.argv("sleep"),time.monotonic()+5,incoming.put).start()
        record=wait_until(self.record)
        try:
            body=json.dumps({"jsonrpc":"2.0","method":"fixture/blocked","params":{"data":"x"*1048576}}).encode()
            transport.send(body,time.monotonic()+2)
            time.sleep(0.05)
            self.assertIsNone(transport.error)
            self.assertFalse(transport._killed)
            start=time.monotonic();transport.cancel()
            self.assertLess(time.monotonic()-start,0.5)
            self.assert_stopped(record)
        finally:transport.close(grace=0)
        self.assertTrue(transport.cleanup_complete)
    def test_helper_sigkill_before_child_observation_uses_unreaped_zombie_anchor(self):
        import queue
        import signal
        import threading
        from agent_harness import lsp_native
        incoming=queue.Queue()
        transport=lsp_native.NativeTransport(self.root,self.argv("sleep"),time.monotonic()+5,incoming.put)
        gate,blocked=threading.Event(),threading.Event()
        exact=transport._read_exact
        def gated_read(count):
            if count==4 and transport._ready.is_set() and transport.child_pid is None and not gate.is_set():
                blocked.set()
                if not gate.wait(4):
                    raise HarnessError("Test observation gate timed out")
            return exact(count)
        states=[]
        kill=transport._kill_owned
        def observed_kill():
            try:states.append(lsp_native._identity(transport._helper.pid)["state"])
            except OSError:pass
            return kill()
        with patch.object(transport,"_read_exact",side_effect=gated_read),patch.object(transport,"_kill_owned",side_effect=observed_kill):
            try:
                transport.start()
                self.assertTrue(blocked.wait(1))
                record=wait_until(self.record)
                self.assertIsNone(transport.child_pid)
                with patch.object(transport._helper,"poll",side_effect=AssertionError("Unsafe early poll")):
                    os.kill(transport._helper.pid,signal.SIGKILL)
                    wait_until(lambda:transport._killed)
                    self.assert_stopped(record)
                    self.assertIn("Z",states)
                    gate.set()
                    with self.assertRaises(HarnessError):transport.close(grace=0)
            finally:
                gate.set()
                if not transport._reaped:
                    try:transport.close(grace=0)
                    except HarnessError:pass
        self.assertTrue(transport.cleanup_complete)

    def test_blocked_server_stdin_and_full_data_queues_have_out_of_band_cancel(self):
        import queue
        import threading
        from agent_harness import lsp_native
        incoming=queue.Queue()
        transport=lsp_native.NativeTransport(self.root,self.argv("sleep"),time.monotonic()+5,incoming.put).start()
        record=wait_until(self.record)
        body=json.dumps({"jsonrpc":"2.0","method":"fixture/large","params":{"data":"x"*1048576}},separators=(",",":")).encode()
        failures=[]
        def fill():
            try:
                for _ in range(30):transport.send(body,time.monotonic()+3)
            except (HarnessError,Exception) as error:failures.append(type(error).__name__)
        thread=threading.Thread(target=fill)
        thread.start()
        try:
            wait_until(lambda:transport._outgoing.full() or transport.error is not None)
            start=time.monotonic();transport.cancel()
            self.assertLess(time.monotonic()-start,0.5)
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assert_stopped(record)
        finally:
            try:transport.close(grace=0)
            except HarnessError:pass
            thread.join(2)
        self.assertTrue(transport.cleanup_complete)

    def test_semantic_callback_failure_stops_owned_processes(self):
        from agent_harness import lsp_native
        def broken(_):raise RuntimeError("Injected semantic callback failure")
        transport=lsp_native.NativeTransport(self.root,self.argv("echo"),time.monotonic()+4,broken).start()
        try:
            transport.send(b'{"jsonrpc":"2.0","id":1,"method":"echo","params":{}}',time.monotonic()+2)
            record=wait_until(self.record)
            wait_until(lambda:transport.error is not None)
            wait_until(lambda:transport._killed)
            self.assert_stopped(record)
        finally:
            with self.assertRaises(HarnessError):transport.close(grace=0)
        self.assertTrue(transport.cleanup_complete)

    def test_abrupt_server_with_partial_frame_and_descendant_pipes(self):
        start=time.monotonic()
        with self.assertRaises(HarnessError):
            with lsp.Session(self.root,self.argv("partial_descendant"),backend="native",timeout=3):
                self.fail("Partial initialize cannot be accepted")
        self.assertLess(time.monotonic()-start,2.5)
        self.assert_stopped(wait_until(self.record))

    def test_descendant_with_devnull_is_stopped_after_root_exit(self):
        import queue
        from agent_harness import lsp_native
        incoming=queue.Queue()
        transport=lsp_native.NativeTransport(self.root,self.argv("devnull_descendant"),time.monotonic()+3,incoming.put).start()
        record=wait_until(self.record)
        try:
            wait_until(lambda:transport._closed.is_set())
            self.assert_stopped(record)
        finally:
            try:transport.close(grace=0)
            except HarnessError:pass
        self.assertTrue(transport.cleanup_complete)


PARENT_DRIVER = r'''
import json,queue,sys,threading,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from agent_harness.lsp_native import NativeTransport
root,server,receipt,driver_receipt,blocked=map(Path,sys.argv[2:7])
mode=sys.argv[7]
gate=threading.Event()
def callback(_):
    blocked.write_text('callback blocked')
    gate.wait(30)
transport=NativeTransport(root,[sys.executable,'-I','-B',str(server),mode,str(receipt)],time.monotonic()+8,callback).start()
driver_receipt.write_text(json.dumps({'helper':transport.helper_identity}))
if mode=='sleep':
    body=json.dumps({'jsonrpc':'2.0','method':'fixture/blocked','params':{'data':'x'*1048576}}).encode()
    transport.send(body,time.monotonic()+5)
while True:time.sleep(1)
'''


def stop_fixture_identities(identities):
    """Cleanup only recorded fixture processes using identity-bound pidfds."""
    import signal
    from agent_harness.lsp_native import _identity
    for identity in identities:
        if not identity:
            continue
        try:
            current=_identity(identity["pid"])
            if current["created"]!=identity["created"]:
                continue
            descriptor=os.pidfd_open(identity["pid"])
            try:
                if _identity(identity["pid"])["created"]==identity["created"]:
                    signal.pidfd_send_signal(descriptor,signal.SIGKILL)
            finally:os.close(descriptor)
        except (ProcessLookupError,FileNotFoundError):
            pass


@unittest.skipUnless(BINARY and sys.platform.startswith("linux"), "Compiled Linux LSP capability not provided")
class NativeLSPParentExitTests(NativeLSPFixture,unittest.TestCase):
    def parent_exit(self,mode):
        import signal
        import subprocess
        from agent_harness.lsp_native import _identity
        driver=self.base/"parent.py"
        driver.write_text(PARENT_DRIVER,encoding="utf-8")
        saved=self.base/"parent.json"
        blocked=self.base/"blocked"
        package=Path(lsp.__file__).resolve().parent.parent
        parent=subprocess.Popen([sys.executable,"-I","-B",str(driver),str(package),str(self.root),
                                 str(self.script),str(self.receipt),str(saved),str(blocked),mode],
                                stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,
                                start_new_session=True)
        owned=[]
        try:
            def parent_record():
                try:return json.loads(saved.read_text())
                except (FileNotFoundError,ValueError):return None
            record=wait_until(self.record)
            helper=wait_until(parent_record)["helper"]
            owned=[helper,_identity(record["pid"]),_identity(record["child"])]
            if mode=="flood":
                wait_until(blocked.exists)
                wait_until(lambda:(self.record() or {}).get("sent",0)>=2)
            else:
                time.sleep(0.1)
            start=time.monotonic()
            os.kill(parent.pid,signal.SIGKILL)
            parent.wait(timeout=2)
            wait_until(lambda:process_identity(helper["pid"]) is None,seconds=3)
            self.assert_stopped(record)
            self.assertLess(time.monotonic()-start,3)
        finally:
            if parent.returncode is None:  # Unreaped direct-child anchor only.
                try:os.kill(parent.pid,signal.SIGKILL)
                except ProcessLookupError:pass
            parent.wait(timeout=2)
            stop_fixture_identities(owned)
            parent.stderr.close()

    def test_parent_hard_exit_stops_blocked_server_and_devnull_descendant(self):
        self.parent_exit("sleep")

    def test_parent_hard_exit_with_output_backpressure_and_stderr_flood(self):
        self.parent_exit("flood")


class RawNativeHelper:
    """Owner fixture using the real helper wire without a semantic adapter."""
    def __init__(self,root):
        import subprocess
        import uuid
        self.session=uuid.uuid4().hex
        self.process=subprocess.Popen([str(Path(BINARY).resolve()),"--lsp-session",self.session],cwd=root,
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,bufsize=0,start_new_session=True)
        self.sequence=0
        self.ready=self.read()
        assert self.ready["helper_pid"]==self.process.pid

    def read(self,seconds=2):
        import select
        import struct
        deadline=time.monotonic()+seconds
        def exact(count):
            chunks=[]
            while count:
                remaining=deadline-time.monotonic()
                if remaining<=0 or not select.select([self.process.stdout],[],[],remaining)[0]:
                    raise TimeoutError("helper output timed out")
                data=os.read(self.process.stdout.fileno(),count)
                if not data:raise EOFError()
                chunks.append(data);count-=len(data)
            return b"".join(chunks)
        length=struct.unpack(">I",exact(4))[0]
        if not 0<length<=2*16*1024*1024+4096:raise AssertionError(length)
        return json.loads(exact(length))

    def send(self,kind,**payload):
        import struct
        self.sequence+=1
        value={"protocol_version":1,"session_id":self.session,"sequence":self.sequence,"kind":kind,**payload}
        raw=json.dumps(value,ensure_ascii=False,separators=(",",":")).encode()
        view=memoryview(struct.pack(">I",len(raw))+raw)
        while view:
            size=self.process.stdin.write(view)
            if not size:raise BrokenPipeError()
            view=view[size:]

    def close(self):
        import signal
        from agent_harness.lsp_native import _identity
        current=_identity(self.process.pid)
        assert current["created"]==self.ready["created"] and os.getpgid(self.process.pid)==self.process.pid
        os.killpg(self.process.pid,signal.SIGKILL)
        self.process.wait(timeout=2)
        self.process.stdin.close();self.process.stdout.close()


@unittest.skipUnless(BINARY and sys.platform.startswith("linux"), "Compiled Linux LSP capability not provided")
class NativeLSPWireTests(NativeLSPFixture,unittest.TestCase):
    def test_in_band_cancel_is_not_stalled_behind_full_server_input(self):
        raw=RawNativeHelper(self.root)
        try:
            raw.send("start",argv=self.argv("sleep"),cwd=str(self.root),timeout_ms=5000)
            record=wait_until(self.record)
            body=json.dumps({"jsonrpc":"2.0","method":"fixture/fill","params":{"data":"x"*(4*1024*1024)}})
            start=time.monotonic()
            try:
                for _ in range(5):raw.send("message",body=body)
                raw.send("cancel")
            except (BrokenPipeError,OSError):
                pass  # Explicit bounded capacity failure may precede cancel.
            terminal=None
            try:
                while True:
                    event=raw.read(seconds=2)
                    if event["kind"]=="terminal":terminal=event
            except EOFError:pass
            self.assertLess(time.monotonic()-start,2)
            self.assertIsNotNone(terminal)
            self.assertIn(terminal["reason"],{"cancelled","input_queue_capacity_exceeded"})
            self.assert_stopped(record)
        finally:raw.close()

    def test_wrong_start_subject_types_and_unknown_fields_never_spawn_server(self):
        for change in ({"session_id":"foreign"},{"sequence":True},{"timeout_ms":True},{"extra":1}):
            with self.subTest(change=change):
                raw=RawNativeHelper(self.root)
                try:
                    payload={"argv":self.argv("sleep"),"cwd":str(self.root),"timeout_ms":1000,**change}
                    raw.send("start",**payload)
                    with self.assertRaises(EOFError):raw.read()
                    self.assertFalse(self.receipt.exists())
                finally:raw.close()


@unittest.skipUnless(BINARY and sys.platform.startswith("linux"), "Compiled Linux LSP capability not provided")
class NativeLSPFramingTests(NativeLSPFixture,unittest.TestCase):
    def test_malformed_fragmented_partial_and_oversized_server_frames(self):
        bodies=[
            b'Content-Length: banana\r\n\r\n',
            b'Content-Length: 1\r\ncontent-length: 1\r\n\r\nx',
            b'Content-Length: 1\n\nx',
            b'X: y\r\n\r\n',
            b'Content-Length: +1\r\n\r\nx',
            b'Content-Length: 0\r\n\r\n',
            b'Content-Length: 16777217\r\n\r\n',
            b'X:'+b'x'*8192+b'\r\n\r\n',
            b'Content-Len',
            b'Content-Length: 99\r\n\r\n{"jsonrpc":',
            b'Content-Length: 1\r\n\r\n\xff',
        ]
        invalid_json=[b'{"jsonrpc":"2.0","id":1,"id":1,"result":{}}',
                      b'{"jsonrpc":"2.0","id":true,"result":{}}',
                      b'{"jsonrpc":"2.0","id":1.0,"result":{}}',
                      b'{"jsonrpc":"1.0","id":1,"result":{}}',
                      b'{"jsonrpc":"2.0","id":1,"method":"x","result":{}}']
        for body in invalid_json:
            bodies.append(b'Content-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body)
        for index,frame in enumerate(bodies):
            with self.subTest(case=index):
                script=self.base/f"bad-{index}.py"
                script.write_text("import os,sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_text(str(os.getpid()))\n"
                    "sys.stdin.buffer.read(1)\nframe="+repr(frame)+"\n"
                    "for byte in frame:\n sys.stdout.buffer.write(bytes([byte]));sys.stdout.buffer.flush()\n"
                    "os._exit(0)\n",encoding="utf-8")
                receipt=self.base/f"bad-{index}.pid"
                start=time.monotonic()
                with self.assertRaises(HarnessError):
                    with lsp.Session(self.root,[sys.executable,"-I","-B",str(script),str(receipt)],backend="native",timeout=2):
                        self.fail("Invalid initialize framing must fail")
                self.assertLess(time.monotonic()-start,3)
                self.assertIsNone(process_identity(int(receipt.read_text())))

    def test_exact_16mib_rpc_and_oversize_local_refusal_preserve_session(self):
        import queue
        from agent_harness import lsp_native
        incoming=queue.Queue()
        transport=lsp_native.NativeTransport(self.root,self.argv("echo"),time.monotonic()+8,incoming.put).start()
        prefix=b'{"jsonrpc":"2.0","id":"large-id","method":"fixture/echo","params":{"data":"'
        suffix=b'"}}'
        count=lsp_native.MAX_FRAME-len(prefix)-len(suffix)
        body=prefix+b'x'*count+suffix
        self.assertEqual(lsp_native.MAX_FRAME,len(body))
        try:
            transport.send(body,time.monotonic()+5)
            reply=incoming.get(timeout=5)
            if isinstance(reply,BaseException):raise reply
            self.assertEqual("large-id",reply["id"])
            self.assertEqual(count,len(reply["result"]["data"]))
            with self.assertRaises(HarnessError):transport.send(b' '+body,time.monotonic()+1)
            with self.assertRaises(HarnessError):transport.send(b'\xff',time.monotonic()+1)
            transport.closing=True
            transport.send(b'{"jsonrpc":"2.0","id":2,"method":"shutdown","params":null}',time.monotonic()+2)
            self.assertEqual(2,incoming.get(timeout=2)["id"])
            transport.send(b'{"jsonrpc":"2.0","method":"exit","params":null}',time.monotonic()+2)
        finally:
            transport.close(require_terminal=True)
        self.assertTrue(transport.session_complete)
        self.assertTrue(transport.cleanup_complete)

    def test_argv_environment_cwd_and_control_descriptor_inheritance(self):
        import queue
        from agent_harness import lsp_native
        incoming=queue.Queue()
        arguments=["", "space argument", 'quote"here', "back\\slash\\", "雪🙂"]
        read_fd,write_fd=os.pipe()
        os.set_inheritable(read_fd,True)
        os.set_inheritable(write_fd,True)
        sentinel=os.readlink(f"/proc/self/fd/{read_fd}")
        try:
            with patch.dict(os.environ,{"HARNESS_LSP_TEST_VALUE":"caller-owned-value"}):
                transport=lsp_native.NativeTransport(self.root,self.argv("echo",*arguments),time.monotonic()+4,incoming.put).start()
            try:
                record=wait_until(self.record)
                self.assertEqual(arguments,record["argv"])
                self.assertEqual(str(self.root),record["cwd"])
                self.assertEqual("caller-owned-value",record["env"])
                self.assertNotIn(sentinel,record["fds"].values())
                for stream in (transport._helper.stdin,transport._helper.stdout):
                    self.assertNotIn(os.readlink(f"/proc/self/fd/{stream.fileno()}"),record["fds"].values())
                transport.closing=True
                transport.send(b'{"jsonrpc":"2.0","id":1,"method":"shutdown"}',time.monotonic()+2)
                self.assertEqual(1,incoming.get(timeout=2)["id"])
                transport.send(b'{"jsonrpc":"2.0","method":"exit"}',time.monotonic()+2)
            finally:transport.close(require_terminal=True)
            self.assertTrue(transport.session_complete)
        finally:os.close(read_fd);os.close(write_fd)

    def test_exact_frame_notification_followed_by_small_rpc_uses_metadata_reserve(self):
        import queue
        from agent_harness import lsp_native
        incoming=queue.Queue()
        transport=lsp_native.NativeTransport(self.root,self.argv("echo"),time.monotonic()+8,incoming.put).start()
        prefix=b'{"jsonrpc":"2.0","method":"fixture/data","params":{"data":"'
        suffix=b'"}}'
        body=prefix+b'x'*(lsp_native.MAX_FRAME-len(prefix)-len(suffix))+suffix
        try:
            transport.send(body,time.monotonic()+5)
            transport.send(b'{"jsonrpc":"2.0","id":1,"method":"fixture/echo","params":{"ok":true}}',time.monotonic()+5)
            value=incoming.get(timeout=5)
            if isinstance(value,BaseException):raise value
            self.assertEqual({"ok":True},value["result"])
            transport.closing=True
            transport.send(b'{"jsonrpc":"2.0","id":2,"method":"shutdown"}',time.monotonic()+2)
            self.assertEqual(2,incoming.get(timeout=2)["id"])
            transport.send(b'{"jsonrpc":"2.0","method":"exit"}',time.monotonic()+2)
        finally:transport.close(require_terminal=True)
        self.assertTrue(transport.session_complete)
