"""Run the real guard against native Web Streams, including terminal SSE cancel."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.core import digest
from agent_harness.opencode_execution import OpenCodeEventNormalizer
from agent_harness.opencode_qualification import GUARD_SOURCE


@unittest.skipUnless(shutil.which("node"), "Guard response lifecycle needs Node Web Streams")
class ResponseLifecycleTests(unittest.TestCase):
    def run_stream(self, mode):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "source"
            root.mkdir()
            runtime = base / "runtime.jsonl"
            guard = base / "guard.mjs"
            guard.write_bytes(GUARD_SOURCE.encode("utf-8"))
            spec = base / "spec.json"
            spec.write_text(json.dumps({
                "root": str(root), "allowed_tools": [], "agent": "harness-leaf",
                "role": "worker", "phase": "execution", "proof_id": "fixture-proof",
                "connected_servers": ["fixture"], "runtime_dir": str(base),
                "guard_source_path": str(guard),
                "guard_source_digest": hashlib.sha256(GUARD_SOURCE.encode()).hexdigest(),
                "expected_catalog_digest": digest([]),
            }), encoding="utf-8")
            script = r'''
import assert from "node:assert/strict";
import fs from "node:fs";
const mode=process.argv[1], hooks={}; let connected=true, cancelled=false, upstream, probes=0;
const plugin=await import(process.env.GUARD_URL);
await plugin.default.setup({
  location:{directory:process.env.PWD},
  session:{hook:async(name,callback)=>{hooks[name]=callback}},
  tool:{hook:async()=>{}}, permission:{hook:async()=>{}},
  mcp:{list:async()=>{
    if(++probes>=3 && mode==="delayed-cancel")await new Promise(resolve=>setTimeout(resolve,20));
    return {data:[{name:"fixture",status:{status:connected?"connected":"failed"}}]};
  }}
});
const event={sessionID:"ses_fixture",kind:"primary",agent:"harness-leaf",tools:{},model:{}};
await hooks.context(event);
await hooks["http.request"]({...event,request:new Request("http://fixture.invalid",{
  method:"POST",body:JSON.stringify({tools:[]})
})});
const input=new ReadableStream({
  start(controller){upstream=controller;controller.enqueue(new TextEncoder().encode("data: [DONE]\n\n"));
    if(mode==="eof")controller.close();},
  async cancel(){
    cancelled=true;
    if(mode==="cancel-error"){
      await new Promise(resolve=>setTimeout(resolve,20));
      throw new Error("fixture cancellation failure");
    }
  }
});
const responseEvent={...event,response:new Response(input)};
await hooks["http.response"](responseEvent);
const reader=responseEvent.response.body.getReader();
assert.equal((await reader.read()).done,false);
if(mode==="eof")assert.equal((await reader.read()).done,true);
else if(mode==="disconnect"){
  connected=false;
  await assert.rejects(reader.cancel("SDK consumed terminal SSE"),/MCP_CONNECTION_LOST/);
} else if(mode==="cancel-error"){
  // Leave the wrapper's prefetch read pending before cancellation rejects.
  await new Promise(resolve=>setTimeout(resolve,0));
  await assert.rejects(reader.cancel("SDK consumed terminal SSE"),/fixture cancellation failure/);
} else if(mode==="error"){
  upstream.error(new Error("fixture stream failure"));
  await assert.rejects(reader.read(),/fixture stream failure/);
} else {
  await reader.cancel("SDK consumed terminal SSE");
  assert.equal(cancelled,true);
}
const rows=fs.readFileSync(process.env.HARNESS_OPENCODE_GUARD_RUNTIME,"utf8")
  .trim().split("\n").map(line=>JSON.parse(line));
const completed=rows.filter(row=>row.stage==="after_response");
assert.equal(completed.length,["disconnect","error","cancel-error"].includes(mode)?0:1);
if(completed.length)assert.equal(completed[0].request_sequence,1);
assert.equal(rows.filter(row=>row.stage==="context").length,1);
assert.equal(rows.filter(row=>row.stage==="before_dispatch").length,1);
'''
            env = {"PATH": os.environ.get("PATH", ""), "PWD": str(root),
                   "HARNESS_OPENCODE_GUARD_SPEC": str(spec),
                   "HARNESS_OPENCODE_GUARD_RUNTIME": str(runtime), "GUARD_URL": guard.as_uri()}
            result = subprocess.run([shutil.which("node"), "--input-type=module", "-e", script, mode],
                                    cwd=root, env=env, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))

    def test_clean_eof_has_one_live_post_response_audit(self):
        self.run_stream("eof")

    def test_sdk_terminal_sse_cancel_has_one_live_post_response_audit(self):
        self.run_stream("cancel")

    def test_disconnect_during_consumer_cancel_still_fails_closed(self):
        self.run_stream("disconnect")

    def test_consumer_cancel_awaits_pending_live_audit(self):
        self.run_stream("delayed-cancel")

    def test_rejected_upstream_cancel_does_not_issue_response_audit(self):
        self.run_stream("cancel-error")

    def test_stream_error_does_not_issue_successful_response_audit(self):
        self.run_stream("error")

    def test_cancel_audit_alone_cannot_complete_an_unfinished_native_step(self):
        self.run_stream("cancel")
        normalizer = OpenCodeEventNormalizer()
        normalizer.feed({"type": "step_start", "sessionID": "ses_fixture",
                         "part": {"messageID": "msg_fixture"}})
        with self.assertRaisesRegex(ValueError, "does not prove a complete execution"):
            normalizer.finish()
