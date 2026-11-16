"""Native-client catalog audit: deterministic fault injection plus opt-in CLI."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_harness.codex_execution import CodexAdapter, _stop_owned_tree
from agent_harness.codex_tool_catalog import audit_catalog, _AppServer, _config_args, MAX_REQUEST_BYTES
from agent_harness.core import HarnessError, identity_alive, process_identity


APP_SERVER = r'''
import json,os,pathlib,subprocess,sys,time,tomllib
mode=sys.argv[1]
assert 'app-server' in sys.argv and '--stdio' in sys.argv
assert 'PRIVATE_TEST_CREDENTIAL' not in os.environ
if mode=='no-reader':time.sleep(3);sys.exit(0)
if mode=='closed-stdin':
    os.close(0)
    child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],stdin=subprocess.DEVNULL)
    pathlib.Path('closed-stdin.ready').write_text(str(child.pid))
    time.sleep(60);sys.exit(0)
config={}
for i,arg in enumerate(sys.argv):
    if arg=='-c':
        key,value=sys.argv[i+1].split('=',1)
        config[key.split('.',1)[1]]=tomllib.loads('v='+value)['v']
for line in sys.stdin:
    request=json.loads(line)
    method=request['method'];ident=request.get('id')
    if method=='initialized':continue
    assert method in ['initialize','thread/start','mcpServerStatus/list']
    if method=='initialize':result={'userAgent':'fixture-only/1'}
    elif method=='thread/start':
        assert request['params']['ephemeral'] is True
        result={'thread':{'id':'thread-fixture','ephemeral':True,'cwd':'/wrong-worktree' if mode=='wrong-root' else request['params']['cwd']}}
    else:
        assert request['params']['threadId']=='thread-fixture'
        assert request['params']['detail']=='toolsAndAuthOnly'
        if mode=='timeout':time.sleep(60)
        if mode=='crash':sys.exit(7)
        if mode=='partial':sys.stdout.write('{"id":');sys.stdout.flush();sys.exit(0)
        if mode=='corrupt':print('{not-json}',flush=True);sys.exit(0)
        if mode=='duplicate-key':print('{"id":3,"id":3,"result":{}}',flush=True);sys.exit(0)
        if mode=='server-request':print(json.dumps({'id':99,'method':'item/commandExecution/requestApproval','params':{}}),flush=True);continue
        data=[]
        for name,server in config.items():
            if mode=='absent':continue
            tools=['search_symbol','read_file','write_file','execute_command']
            if mode!='expose-write':
                tools=[x for x in tools if x in server.get('enabled_tools',tools) and x not in server.get('disabled_tools',[])]
            if mode=='missing-tool':tools=[x for x in tools if x!='search_symbol']
            data.append({'name':name,'runtimeStatus':None if mode=='cached' else 'failed' if mode=='startup-failed' else 'connected',
                         'tools':{x:{'name':x,'inputSchema':{'type':'object'}} for x in tools},'toolsError':'failed' if mode=='tools-error' else None})
        if mode=='unexpected':data.append({'name':'foreign','runtimeStatus':'connected','tools':{}})
        result={'data':data,'nextCursor':'x'*100000 if mode=='long-cursor' else 'repeat' if mode=='cursor-loop' else None}
    print(json.dumps({'id':True if mode=='bool-id' and method=='initialize' else ident,'result':result}),flush=True)
    if mode=='long-cursor' and method=='mcpServerStatus/list':time.sleep(3);sys.exit(0)
    if mode=='duplicate-response' and method=='initialize':print(json.dumps({'id':ident,'result':result}),flush=True)
'''

MCP_SERVER = r'''
import json,pathlib,sys
for line in sys.stdin:
    request=json.loads(line);method=request['method'];ident=request.get('id')
    if ident is None:continue
    if method=='initialize':result={'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':'catalog-fixture-only','version':'1'}}
    elif method=='tools/list':result={'tools':[{'name':x,'inputSchema':{'type':'object'}} for x in ['search_symbol','read_file','write_file','execute_command']]}
    elif method=='tools/call':
        tool=request['params']['name']
        if tool in ['write_file','execute_command']:pathlib.Path('FORBIDDEN-'+tool).write_text('forbidden dispatched')
        result={'content':[{'type':'text','text':'fixture'}]}
    else:result={}
    print(json.dumps({'jsonrpc':'2.0','id':ident,'result':result}),flush=True)
'''


class CodexToolCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='harness-catalog-test-')
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / 'project'; self.project.mkdir()
        self.fake = self.root / 'app-server.py'; self.fake.write_text(APP_SERVER)
        self.config = {'mcp_servers': {'fixture': {'command': sys.executable, 'args': ['fixture.py'],
            'required': True, 'enabled_tools': ['search_symbol','read_file'], 'startup_readiness':'connection'}}}

    def tearDown(self):
        self.temp.cleanup()

    def audit(self, mode='ready', **kwargs):
        # Credentials from a caller's environment are not copied to diagnostics.
        adapter=CodexAdapter((sys.executable,str(self.fake),mode),environment=dict(os.environ,
            CODEX_HOME=str(self.root/'user-home'),PRIVATE_TEST_CREDENTIAL='must-not-copy'))
        return audit_catalog(adapter,self.config,self.project,timeout=kwargs.pop('timeout',3),**kwargs)

    def test_native_filtered_catalog_has_process_config_and_thread_binding(self):
        result=self.audit()
        self.assertEqual(result['status'],'READY',result)
        self.assertTrue(result['client_catalog_verified'])
        self.assertTrue(result['live_connection_verified'])
        self.assertFalse(result['qualified_for_exec'])
        self.assertFalse(result['reviewer_isolation_qualified'])
        self.assertEqual({x['name'] for x in result['tools']},{'search_symbol','read_file'})
        self.assertEqual(result['server_connections'],{'fixture':'connected'})
        self.assertEqual(result['thread_id'],'thread-fixture')
        self.assertEqual(len(result['catalog_digest']),64)
        self.assertEqual(len(result['event_stream_digest']),64)
        self.assertTrue(result['process_tree_stopped'])
        self.assertFalse(identity_alive(result['process_identity']))
        self.assertFalse((self.root/'user-home').exists())

    def test_mandatory_missing_cached_failed_or_policy_violating_catalog_blocks(self):
        for mode in ['absent','cached','startup-failed','tools-error','missing-tool','expose-write','unexpected']:
            with self.subTest(mode=mode):
                result=self.audit(mode)
                self.assertEqual(result['status'],'BLOCKED',result)
                self.assertFalse(result['client_catalog_verified'])
                self.assertFalse(result['live_connection_verified'])
                self.assertFalse(result['qualified_for_exec'])
                self.assertTrue(result['process_tree_stopped'])

    def test_corrupt_partial_duplicate_foreign_rpc_and_pagination_never_qualify(self):
        for mode in ['partial','corrupt','duplicate-key','server-request','duplicate-response','cursor-loop','crash','wrong-root','bool-id']:
            with self.subTest(mode=mode):
                result=self.audit(mode)
                self.assertEqual(result['status'],'BLOCKED',result)
                self.assertFalse(result['client_catalog_verified'])
                self.assertTrue(result['process_tree_stopped'])

    def test_long_cursor_peer_that_stops_reading_is_bounded_before_next_write(self):
        started=time.monotonic()
        result=self.audit('long-cursor',timeout=.5)
        self.assertEqual(result['status'],'BLOCKED',result)
        self.assertEqual(result['reason'],'MCP_CLIENT_PAGINATION_INVALID')
        self.assertLess(time.monotonic()-started,2)
        self.assertTrue(result['process_tree_stopped'])
        self.assertFalse(identity_alive(result['process_identity']))

    def test_pipe_backpressure_cannot_escape_absolute_request_deadline(self):
        client=_AppServer([sys.executable,str(self.fake),'no-reader','app-server','--stdio'],
                          self.project,dict(os.environ),.2)
        started=time.monotonic()
        try:
            with self.assertRaisesRegex(HarnessError,'MCP_CLIENT_TIMEOUT'):
                client.request('initialize',{'padding':'x'*32768})
        finally:
            client.close()
        self.assertLess(time.monotonic()-started,2)
        self.assertTrue(client.stopped)
        self.assertFalse(client.writer.is_alive())
        self.assertFalse(identity_alive(client.identity))

    def test_request_byte_bound_rejects_without_starting_a_writer(self):
        client=_AppServer([sys.executable,str(self.fake),'no-reader','app-server','--stdio'],
                          self.project,dict(os.environ),.5)
        try:
            with self.assertRaisesRegex(HarnessError,'MCP_CLIENT_REQUEST_BOUND'):
                client.request('initialize',{'padding':'x'*MAX_REQUEST_BYTES})
            self.assertIsNone(client.writer)
        finally:
            client.close()

    def test_closed_stdin_flush_cannot_leave_live_child_or_process_tree(self):
        client=_AppServer([sys.executable,str(self.fake),'closed-stdin','app-server','--stdio'],
                          self.project,dict(os.environ),2)
        marker=self.project/'closed-stdin.ready'
        try:
            deadline=time.monotonic()+2
            while not marker.exists() and time.monotonic()<deadline:
                time.sleep(.01)
            self.assertTrue(marker.exists())
            child_identity=process_identity(int(marker.read_text()))
            self.assertTrue(identity_alive(client.identity))
            # Small buffered bytes are flushed by close(), which raises EPIPE
            # if cleanup attempts to close stdin before killing the live tree.
            client.process.stdin.write(b'pending')
            client.close()
            self.assertTrue(client.stopped)
            self.assertFalse(identity_alive(client.identity))
            self.assertFalse(identity_alive(child_identity))
        finally:
            if not client.stopped:
                _stop_owned_tree(client.process,client.job,client.identity)
                client.job.close()

    def test_timeout_stops_real_owned_process(self):
        result=self.audit('timeout',timeout=.15)
        self.assertEqual(result['status'],'BLOCKED',result)
        self.assertEqual(result['reason'],'MCP_CLIENT_TIMEOUT')
        self.assertTrue(result['process_tree_stopped'])
        self.assertFalse(identity_alive(result['process_identity']))

    def test_disabled_tools_are_checked_independently_of_allowlist(self):
        self.config['mcp_servers']['fixture'].pop('enabled_tools')
        self.config['mcp_servers']['fixture']['disabled_tools']=['write_file','execute_command']
        self.assertEqual(self.audit()['status'],'READY')
        self.assertEqual(self.audit('expose-write')['status'],'BLOCKED')

    def test_only_native_mcp_fragment_and_bounded_canonical_input_allowed(self):
        adapter=CodexAdapter((sys.executable,str(self.fake),'ready'))
        for value in ({'model':'changed'},{'mcp_servers':{},'auth':{}},{'mcp_servers':{'bad.name':{}}}):
            with self.assertRaises(HarnessError):audit_catalog(adapter,value,self.project)
        for timeout in (0,61,True,float('nan')):
            with self.assertRaises(HarnessError):audit_catalog(adapter,self.config,self.project,timeout=timeout)
        with self.assertRaises(HarnessError):audit_catalog(adapter,self.config,Path('relative'))

    def test_fractional_native_timeouts_remain_supported_but_nonfinite_reject(self):
        self.config['mcp_servers']['fixture']['startup_timeout_sec']=.3
        self.assertEqual(self.audit()['status'],'READY')
        for value in (float('nan'),float('inf')):
            self.config['mcp_servers']['fixture']['startup_timeout_sec']=value
            with self.assertRaises(HarnessError):_config_args(self.config)

    @unittest.skipUnless(os.environ.get('HARNESS_LIVE_CODEX_CATALOG')=='1' and shutil.which('codex'),
                         'Live Codex app-server requires explicit no-inference smoke opt-in')
    def test_live_codex_filters_and_denies_write_capable_fake_mcp_tools(self):
        mcp=self.root/'mcp.py';mcp.write_text(MCP_SERVER)
        native={'mcp_servers':{'fixture':{'command':sys.executable,'args':[str(mcp)],'cwd':str(self.project),
            'required':True,'enabled_tools':['search_symbol','read_file'],'startup_readiness':'connection','startup_timeout_sec':3}}}
        adapter=CodexAdapter()
        result=audit_catalog(adapter,native,self.project,timeout=10)
        self.assertEqual(result['status'],'READY',result)
        self.assertTrue(result['live_connection_verified'])
        self.assertEqual({x['name'] for x in result['tools']},{'search_symbol','read_file'})
        self.assertFalse(result['qualified_for_exec'])
        home=self.root/'isolated-home';home.mkdir()
        environment={k:v for k,v in os.environ.items() if k.upper() in {'PATH','SYSTEMROOT','WINDIR','PATHEXT','COMSPEC','TEMP','TMP','TMPDIR'}}
        environment.update(HOME=str(home),CODEX_HOME=str(home))
        client=_AppServer([*adapter.executable,*_config_args(native),'app-server','--stdio'],self.project,environment,10)
        try:
            client.request('initialize',{'clientInfo':{'name':'harness-negative-tool-probe','version':'1'},'capabilities':{'experimentalApi':True}})
            client.notify('initialized')
            thread=client.request('thread/start',{'cwd':str(self.project),'ephemeral':True})['thread']['id']
            client.request('mcpServerStatus/list',{'threadId':thread,'detail':'toolsAndAuthOnly','limit':100})
            for tool in ['write_file','execute_command']:
                reply=client.request('mcpServer/tool/call',{'threadId':thread,'server':'fixture','tool':tool,'arguments':{}})
                self.assertIn('rpc_error',reply,reply)
                self.assertIn('disabled',reply['rpc_error']['message'])
                self.assertFalse((self.project/('FORBIDDEN-'+tool)).exists())
        finally:
            client.close()
        self.assertTrue(client.stopped)


if __name__=='__main__':
    unittest.main()
