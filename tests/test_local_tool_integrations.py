"""Optional tool launch/provenance unit checks and separately opt-in real tools."""
from concurrent.futures import ThreadPoolExecutor
import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.core import HarnessError, identity_alive
from agent_harness.local_tool_integrations import (prepare_serena, serena_stdio_argv,
    graphify_build, build_local_graph_index, context7_server, context7_smoke, prepare_context7, context7_stdio_argv)
from agent_harness.tooling_probe import StdioProbe
from agent_harness.tooling_provenance import source_snapshot, verify_graph_index, ast_grep_migrate


class LocalToolIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "symbol.py").write_text("def shared_symbol(): return 'left'\n")

    def executable(self, text):
        path = self.root / "fake_tool.py"
        path.write_text("#!" + sys.executable + "\n" + text)
        path.chmod(0o700)
        return str(path)

    def test_serena_private_policy_preserves_native_state_without_source_writes(self):
        exe = self.executable("import sys\nprint('Serena 2.1.0' if '--version' in sys.argv else '--project --agent-interface --transport --enable-web-dashboard --enable-gui-log-window --open-web-dashboard')\n")
        first = prepare_serena(self.source, self.root / "cache-a", executable=exe)
        second = prepare_serena(self.source, self.root / "cache-b", executable=exe)
        self.assertNotEqual(first["project_state"], second["project_state"])
        self.assertNotEqual(first["environment"]["SERENA_HOME"], second["environment"]["SERENA_HOME"])
        self.assertIn(str(self.source), first["argv"])
        self.assertFalse((self.source / ".serena").exists())
        config = Path(first["environment"]["SERENA_HOME"]) / "serena_config.yml"
        value = json.loads(config.read_text());value["projects"] = [str(self.source)];value["legitimate_native_default"] = False
        config.write_text(json.dumps(value))
        self.assertEqual(prepare_serena(self.source, self.root / "cache-a", executable=exe)["argv"], first["argv"])
        value["agent_interface"] = "REPL";config.write_text(json.dumps(value))
        with self.assertRaisesRegex(HarnessError, "policy changed"):
            prepare_serena(self.source, self.root / "cache-a", executable=exe)
        self.assertTrue(Path(serena_stdio_argv(self.source, self.root / "other", executable=exe)[1]).is_absolute())

    def test_serena_blocks_source_cache_unknown_cli_and_unbounded_timeout(self):
        for timeout in (True, 0, float('nan'), 301):
            with self.assertRaises(HarnessError):prepare_serena(self.source, self.root / "cache", timeout=timeout)
        with self.assertRaisesRegex(HarnessError, "outside"):
            prepare_serena(self.source, self.source / "cache")
        exe = self.executable("print('Serena 9.9.9')\n")
        with self.assertRaisesRegex(HarnessError, "VERSION"):
            prepare_serena(self.source, self.root / "cache", executable=exe)

    def test_context7_private_launcher_rejects_ambient_injection_and_code_drift(self):
        exe = self.executable("import sys\nprint('4.6.0' if '--version' in sys.argv else '--transport')\n")
        with patch.dict(os.environ, {"NODE_OPTIONS": "--require fixture-injection", "HTTP_PROXY": "fixture-proxy", "CONTEXT7_API_KEY": "fixture-secret"}):
            invocation = prepare_context7(self.source, self.root / "context7", executable=exe)
        self.assertEqual(invocation["mode"], "public-anonymous")
        self.assertEqual(invocation["argv"][-2:], ["--transport", "stdio"])
        self.assertEqual(invocation["cwd"], str(self.source))
        self.assertFalse({"NODE_OPTIONS", "HTTP_PROXY", "CONTEXT7_API_KEY"} & set(invocation["environment"]))
        self.assertNotEqual(invocation["environment"]["HOME"], os.environ.get("HOME"))
        self.assertIn("--implementation-digest", context7_stdio_argv(self.source, self.root / "context7", executable=exe))
        Path(exe).write_text(Path(exe).read_text()+"# changed implementation\n")
        with self.assertRaisesRegex(HarnessError, "IMPLEMENTATION_DRIFT"):
            prepare_context7(self.source, self.root / "context7", executable=exe, expected_implementation=invocation["implementation_digest"])

    def test_graphify_real_subprocess_fixture_honors_output_scope_and_provenance(self):
        exe = self.executable("""import sys,json,pathlib,os
if '--version' in sys.argv: print('graphify 0.9.111');raise SystemExit
assert '--code-only' in sys.argv and '--no-cluster' in sys.argv
assert 'OPENAI_API_KEY' not in os.environ
out=pathlib.Path(sys.argv[sys.argv.index('--out')+1])/'graphify-out';out.mkdir(parents=True)
(out/'extraction.json').write_text(json.dumps({'nodes':[{'id':'symbol.py:shared_symbol'}],'edges':[]}))
""")
        implementation = self.root / "lib/python3.13/site-packages/graphify/extract.py"
        implementation.parent.mkdir(parents=True)
        implementation.write_text("MODE = 'initial'\n")
        frozen = source_snapshot(self.source)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fixture-only-not-a-real-key"}):
            receipt = build_local_graph_index(self.source, self.root / "indexes", executable=exe)
        self.assertEqual(receipt["source_snapshot_digest"], frozen["digest"])
        verify_graph_index(receipt, frozen)
        payload = json.loads(Path(receipt["index_path"]).read_text())
        self.assertEqual(payload["mode"], "local-code-only")
        self.assertEqual(payload["graph"]["nodes"][0]["id"], "symbol.py:shared_symbol")
        implementation.write_text("MODE = 'changed'\n")
        with self.assertRaises(HarnessError):verify_graph_index(receipt, frozen)
        self.assertEqual(verify_graph_index(receipt, frozen, required=False)["status"], "HISTORICAL_ONLY")
        implementation.write_text("MODE = 'initial'\n")
        verify_graph_index(receipt, frozen)
        (self.source / "symbol.py").write_text("def changed(): return 2\n")
        with self.assertRaises(HarnessError):verify_graph_index(receipt, source_snapshot(self.source))

    def test_serena_rejects_project_startup_and_local_overrides_before_launch(self):
        exe = self.executable("import sys\nprint('Serena 2.1.0' if '--version' in sys.argv else '--project --agent-interface --transport --enable-web-dashboard --enable-gui-log-window --open-web-dashboard')\n")
        invocation = prepare_serena(self.source, self.root / "cache", executable=exe)
        self.assertEqual(invocation["environment"]["SERENA_USAGE_REPORTING"], "false")
        policy = json.loads((Path(invocation["environment"]["SERENA_HOME"]) / "serena_config.yml").read_text())
        self.assertEqual(policy["trusted_project_path_patterns"], [])
        project = Path(invocation["project_state"])
        for filename in ("project.yml", "project.local.yml"):
            for item in ({"activation_command": "printf sentinel"}, {"agent_interface": "REPL"},
                         {"fixed_tools": ["execute_shell_command"]}, {"ls_specific_settings": {"python": {"ls_base_cmd": ["sh"]}}},
                         {"ls_additional_workspace_folders": ["/tmp"]}):
                with self.subTest(filename=filename,item=item):
                    (project/filename).write_text(json.dumps(item))
                    with self.assertRaisesRegex(HarnessError,"SERENA_PROJECT_POLICY_FORBIDDEN"):
                        prepare_serena(self.source,self.root/"cache",executable=exe)
                    (project/filename).unlink()
        config = Path(invocation["environment"]["SERENA_HOME"]) / "serena_config.yml"
        original = json.loads(config.read_text())
        for change in ({"projects": [str(self.root / "foreign.yml")]}, {"language_backend": "JetBrains"},
                       {"ls_specific_settings": {"python": {"ls_base_cmd": ["sh"]}}}):
            config.write_text(json.dumps({**original, **change}))
            with self.assertRaisesRegex(HarnessError,"policy changed"):
                prepare_serena(self.source,self.root/"cache",executable=exe)
        config.write_text(json.dumps(original))
        other = self.root / "other-source";other.mkdir()
        with self.assertRaisesRegex(HarnessError,"WORKTREE_DRIFT"):
            prepare_serena(other,self.root/"cache",executable=exe)

    def test_graphify_failure_keeps_only_bounded_exit_and_output_hashes(self):
        exe = self.executable("import sys\nif '--version' in sys.argv: print('graphify 0.9.111');raise SystemExit\nprint('fixture public output');print('fixture diagnostics',file=sys.stderr);raise SystemExit(23)\n")
        with self.assertRaises(HarnessError):
            build_local_graph_index(self.source,self.root/"indexes",executable=exe)
        files = list((self.root/"indexes/failures").glob("*.json"))
        self.assertEqual(len(files),1)
        value = json.loads(files[0].read_text())
        self.assertEqual(value["exit_code"],23)
        self.assertEqual(len(value["stderr_sha256"]),64)
        self.assertNotIn("fixture diagnostics",files[0].read_text())

    def test_stdio_cleanup_cannot_be_skipped_by_broken_pipe_close(self):
        spec = {"transport":"stdio","command":[sys.executable,"-c","import time;time.sleep(60)"],
            "cwd":str(self.source),"startup_timeout":.2,"tool_timeout":.2}
        connection = StdioProbe(spec);connection.__enter__()
        original = connection.process.stdin
        class ClosingPipe:
            def close(self):original.close();raise BrokenPipeError('fixture closed pipe')
        connection.process.stdin = ClosingPipe()
        connection.close()
        self.assertTrue(connection.stopped)
        self.assertIsNotNone(connection.process.poll())
        self.assertFalse(identity_alive(connection.identity))

    def test_context7_policy_is_only_public_read_tools(self):
        spec = context7_server()
        self.assertEqual(spec['allowed_tools'], ['resolve-library-id', 'query-docs'])
        self.assertEqual(spec['allowed_tools'],spec['read_only_tools'])
        self.assertNotIn('environment',spec)
        self.assertNotIn('--api-key',spec['command'])


@unittest.skipUnless(os.environ.get('HARNESS_LIVE_LOCAL_TOOLS') == '1', 'Real optional tool qualification requires explicit HARNESS_LIVE_LOCAL_TOOLS=1')
class LiveLocalToolsTests(unittest.TestCase):
    setUp = LocalToolIntegrationTests.setUp

    def tool(self, relative):
        base=os.environ.get('HARNESS_OPTIONAL_TOOL_ROOT')
        path=Path(base)/relative if base else Path('/missing-optional-tool')
        if not path.is_file():self.skipTest('Pinned actual tool missing: '+relative)
        return str(path)

    def test_two_parallel_actual_serena_worktrees_have_private_state_and_scope(self):
        exe=self.tool('serena/bin/serena')
        for args in (('init',),('add','.'),('-c','user.name=Fixture','-c','user.email=fixture@localhost','commit','-m','source')):
            subprocess.run(['git',*args],cwd=self.source,check=True,capture_output=True)
        trees=[]
        for marker in ('left','right'):
            root=self.root/marker
            subprocess.run(['git','worktree','add','--detach',str(root),'HEAD'],cwd=self.source,check=True,capture_output=True)
            (root/'symbol.py').write_text("def shared_symbol(): return '"+marker+"'\n")
            trees.append((marker,root))
        barrier=threading.Barrier(2,timeout=120)
        def worker(pair):
            marker,root=pair
            spec={'id':'serena','transport':'stdio','required':True,'cwd':str(root),
                'command':serena_stdio_argv(root,self.root/('cache-'+marker),executable=exe),
                'allowed_tools':['find_symbol','read_file'],'read_only_tools':['find_symbol','read_file'],
                'forbidden_tools':[],'required_tools':['find_symbol'],'startup_timeout':90,'tool_timeout':90}
            client=StdioProbe(spec)
            with client:
                client.inventory();barrier.wait()
                if os.name=='posix' and Path('/proc').exists():self.assertEqual(Path('/proc',str(client.process.pid),'cwd').resolve(),root)
                result=client.call_readonly('find_symbol',{'name_path_pattern':'shared_symbol','relative_path':'symbol.py','include_body':True})
                value=json.dumps(result)
                self.assertIn("return '"+marker+"'",value)
                self.assertNotIn("return '"+('right' if marker=='left' else 'left')+"'",value)
                self.assertNotIn('serena_repl',[tool['name'] for tool in client.advertised_catalog])
                process=client.identity
            self.assertTrue(client.stopped)
            self.assertFalse(identity_alive(process))
            changed=subprocess.check_output(['git','status','--porcelain','--untracked-files=all'],cwd=root,text=True)
            self.assertEqual([line[3:] for line in changed.splitlines()],['symbol.py'])
            return process
        with ThreadPoolExecutor(max_workers=2) as pool:processes=list(pool.map(worker,trees))
        self.assertNotEqual(processes[0],processes[1])

    def test_actual_graphify_frozen_snapshot_index_and_invalidation(self):
        exe=self.tool('graphify/bin/graphify')
        (self.source/'use.py').write_text('from symbol import shared_symbol\nVALUE = shared_symbol()\n')
        frozen=source_snapshot(self.source)
        receipt=build_local_graph_index(self.source,self.root/'graph-indexes',executable=exe,timeout=120)
        verify_graph_index(receipt,frozen)
        graph=json.loads(Path(receipt['index_path']).read_text())['graph']
        self.assertTrue(graph['nodes'])
        self.assertIn('shared_symbol',json.dumps(graph))
        tree=ast.parse((self.source/'use.py').read_text())
        self.assertTrue(any(isinstance(n,ast.ImportFrom) and n.module=='symbol' for n in ast.walk(tree)))
        (self.source/'symbol.py').write_text('def shared_symbol(): return 2\n')
        with self.assertRaises(HarnessError):verify_graph_index(receipt,source_snapshot(self.source))

    def test_actual_ast_grep_structural_rewrite_scans_scope_and_tests(self):
        exe=self.tool('node/node_modules/.bin/ast-grep')
        (self.source/'symbol.py').write_text('def old_call(x): return x+1\ndef new_call(x): return x+1\nVALUE = old_call(1)\n')
        receipt=ast_grep_migrate(self.source,['symbol.py'],pattern='old_call($A)',rewrite='new_call($A)',language='python',
            executable=exe,checks=[[sys.executable,'-c','from symbol import VALUE; assert VALUE == 2']],timeout=30)
        self.assertEqual(receipt['status'],'verified_migration')
        self.assertEqual(receipt['old_pattern_scan']['count'],0)
        self.assertEqual(receipt['changed_paths'],['symbol.py'])

    def test_actual_context7_stdio_catalog_and_public_read(self):
        exe=self.tool('node/node_modules/.bin/context7-mcp')
        receipt=context7_smoke(exe,self.source,timeout=30)
        self.assertEqual(receipt.get('tools'),['query-docs','resolve-library-id'])
        self.assertTrue(receipt['process_tree_stopped'])
        if receipt['status']!='OBSERVED_READ':self.skipTest('Actual stdio catalog observed; public read blocked: '+str(receipt.get('reason')))
        self.assertEqual([r['tool'] for r in receipt['reads']],['resolve-library-id','query-docs'])


if __name__=='__main__':unittest.main()
