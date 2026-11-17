"""Bounded native app-server events for the optional strict Codex transport.

The legacy exec reader is deliberately unchanged. This normalizer is not a
capability issuer; callers must separately prove native tool restrictions.
"""
from __future__ import annotations

from .core import HarnessError


class NativeCodexEvents:
    def __init__(self, thread_id, *, historical_turn_ids=()):
        if not isinstance(thread_id, str) or not thread_id:
            raise HarnessError('Exact native thread identity is required')
        self.thread_id = thread_id
        self.turn_id = None
        self.terminal = None
        self.usage = None
        self.items = set()
        self.expected_turn_id = None
        if any(not isinstance(value,str) or not value for value in historical_turn_ids):
            raise HarnessError('Invalid resumed historical turn identity')
        self.historical_turn_ids=frozenset(historical_turn_ids)

    def bind_turn_response(self,turn):
        if (not isinstance(turn,dict) or not isinstance(turn.get('id'),str) or not turn['id']
                or turn.get('id') in self.historical_turn_ids
                or turn.get('status') not in (None,'inProgress')
                or self.turn_id is not None and self.turn_id!=turn['id']):
            raise HarnessError('Native turn response identity/status changed')
        self.expected_turn_id=turn['id']

    def _subject(self, params, *, active_turn=False):
        if params.get('threadId') != self.thread_id:
            raise HarnessError('Native event thread identity changed')
        if active_turn and (self.turn_id is None or params.get('turnId') != self.turn_id):
            raise HarnessError('Native event lacks its exact active turn identity')
        if self.turn_id is not None and params.get('turnId', self.turn_id) != self.turn_id:
            raise HarnessError('Native event turn identity changed')

    def feed(self, method, params):
        if not isinstance(method, str) or not isinstance(params, dict):
            raise HarnessError('Malformed native event envelope')
        if method == 'thread/started':
            if params.get('thread', {}).get('id') != self.thread_id:
                raise HarnessError('Native started thread identity changed')
            return []
        if method == 'turn/started':
            self._subject(params)
            turn = params.get('turn')
            if (self.terminal is not None or self.turn_id is not None or not isinstance(turn, dict)
                    or not isinstance(turn.get('id'), str) or not turn['id']):
                raise HarnessError('Conflicting native turn start')
            self.turn_id = turn['id']
            if self.expected_turn_id is not None and self.expected_turn_id!=self.turn_id:
                raise HarnessError('Native turn response/event identity changed')
            return [{'type': 'turn.started'}]
        if method == 'thread/tokenUsage/updated':
            self._subject(params)
            usage = params.get('tokenUsage', {}).get('last')
            required = {'inputTokens', 'outputTokens', 'cachedInputTokens', 'reasoningOutputTokens', 'totalTokens'}
            if (not isinstance(usage, dict) or not required <= set(usage)
                    or any(type(usage[key]) is not int or usage[key] < 0 for key in required)):
                raise HarnessError('Malformed native usage event')
            if (self.turn_id is None and self.expected_turn_id is None
                    and params.get('turnId') in self.historical_turn_ids):
                # Resume hydration may publish a known prior turn's counters
                # after its response. They never cover this new invocation.
                return []
            self._subject(params, active_turn=True)
            values = {}
            for native, normalized in [('inputTokens', 'input_tokens'), ('outputTokens', 'output_tokens'),
                    ('cachedInputTokens', 'cached_input_tokens'), ('reasoningOutputTokens', 'reasoning_tokens')]:
                value = usage.get(native)
                if value is not None and (type(value) is not int or value < 0):
                    raise HarnessError('Malformed native usage counter')
                values[normalized] = value
            self.usage = values
            return []
        if method in {'item/started', 'item/completed'}:
            self._subject(params, active_turn=True)
            if self.turn_id is None or self.terminal is not None:
                raise HarnessError('Native item outside its active turn')
            item = params.get('item')
            if not isinstance(item, dict) or not isinstance(item.get('id'), str):
                raise HarnessError('Malformed native item')
            if method == 'item/started':
                return []
            if item['id'] in self.items:
                raise HarnessError('Duplicate native completed item')
            self.items.add(item['id'])
            item = dict(item)
            item['type'] = {'agentMessage': 'agent_message', 'mcpToolCall': 'mcp_tool_call',
                'commandExecution': 'command_execution', 'fileChange': 'file_change',
                'dynamicToolCall': 'dynamic_tool_call'}.get(item.get('type'), item.get('type'))
            if item['type'] == 'agent_message' and not isinstance(item.get('text'), str):
                raise HarnessError('Malformed native assistant message')
            return [{'type': 'item.completed', 'item': item}]
        if method == 'turn/completed':
            self._subject(params)
            turn = params.get('turn')
            if (self.turn_id is None or self.terminal is not None or not isinstance(turn, dict)
                    or turn.get('id') != self.turn_id or turn.get('status') not in {'completed', 'failed', 'interrupted'}):
                raise HarnessError('Conflicting or incomplete native terminal event')
            self.terminal = turn['status']
            if self.terminal == 'completed':
                return [{'type': 'turn.completed', 'usage': self.usage or {}}]
            if self.terminal == 'interrupted':
                return [{'type': 'turn.interrupted'}]
            return [{'type': 'turn.failed', 'error': turn.get('error') or {'message': self.terminal}}]
        if method == 'error':
            self._subject(params)
            raise HarnessError('Native error notification')
        # Legacy codex/event/* notifications duplicate the v2 stream; unrelated
        # service notifications carry no execution completion authority.
        return []

    def finish(self):
        if self.turn_id is None or self.terminal is None:
            raise HarnessError('Incomplete native turn stream')
        return self.terminal


# Imported below the protocol-only normalizer so it stays independently usable.
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import signal
import threading
from contextlib import contextmanager
import time
import uuid
from .codex_execution import (CodexAdapter, MAX_PROMPT_BYTES, MAX_EVENT_BYTES, MAX_LOG_BYTES,
    SESSION, _explicit_environment, _mkdir, _parents, _ordinary, sanitized)
from .file_io import canonical_directory, read_regular_file
from .task_packets import NotDispatched


@contextmanager
def _signal_scope(cancel):
    handlers={}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT,signal.SIGTERM):
            handlers[signum]=signal.getsignal(signum)
            signal.signal(signum,lambda *_:cancel.set())
    try:yield
    finally:
        for signum,handler in handlers.items():signal.signal(signum,handler)


class StrictCodexAdapter(CodexAdapter):
    """Optional per-leaf native app-server transport; no daemon or model client."""
    backend='codex'

    @classmethod
    def configured(cls,base,config,root):
        clone=object.__new__(cls);clone.__dict__.update(base.__dict__)
        clone._tool_config=copy.deepcopy(config);clone.tool_config=copy.deepcopy(config)
        clone.tool_root=str(canonical_directory(root));clone.read_context=()
        clone.metadata_path=None;clone._native_qualification=None
        return clone

    def with_environment(self,environment):
        clone=copy.copy(self);clone.environment=_explicit_environment(environment);return clone

    def with_tool_config(self,config,*,root=None,config_path=None):
        if not isinstance(config,dict) or set(config)-{'mcp_servers'}:
            raise HarnessError('Native Codex profile accepts only MCP declarations')
        clone=self.configured(self,config,root if root is not None else self.tool_root)
        clone.read_context=self.read_context
        return clone

    def with_read_context(self,paths):
        from .codex_qualification import read_context_manifest
        clone=copy.copy(self);clone.read_context=tuple(read_context_manifest(paths));clone._native_qualification=None
        return clone

    def qualify(self,*,root,role,evidence_dir,timeout=60,session_id=None):
        from .codex_qualification import qualify
        return qualify(self,root=root,role=role,evidence_dir=evidence_dir,timeout=timeout,session_id=session_id)

    def qualification_receipt(self):
        if self._native_qualification is None:raise HarnessError('No core-issued native Codex qualification')
        return copy.deepcopy(self._native_qualification.receipt)

    def verify_qualification(self,*,root=None,role=None):
        from .codex_qualification import verify_qualification_evidence
        proof=self._native_qualification;root=root if root is not None else self.tool_root
        if proof is None or not proof.valid(self,root) or role is not None and proof.role!=role:
            raise HarnessError('EXACT_RESUME_DRIFT: native Codex qualification changed or expired')
        verify_qualification_evidence(proof.receipt,root=root,role=proof.role)

    def effective_tool_catalog(self):
        self.verify_qualification();return copy.deepcopy(list(self._native_qualification.catalog))

    def capabilities(self,*,deadline=None):
        result=super().capabilities(deadline=deadline)
        proof=self._native_qualification
        qualified=False
        if proof is not None:
            try:self.verify_qualification();qualified=True
            except (HarnessError,OSError,ValueError):pass
        result.update(transport='native_app_server',managed_leaf_qualified=qualified,
            native_children_disabled=qualified,effective_tool_catalog=qualified,
            reviewer_isolation_qualified=qualified and proof.role!='worker',exact_resume_qualified=qualified,
            qualification_status='QUALIFIED' if qualified else 'BLOCKED_NOT_QUALIFIED',
            connected_mcp_servers=list(proof.connected) if qualified else [],
            suppression_proof='Native tool metadata ceiling and actual-thread MCP census' if qualified else 'Native Codex policy is not qualified')
        if qualified:
            from .core import digest
            result['effective_catalog_digest']=digest(list(proof.catalog));result['qualification_ref']=self.qualification_receipt()
        return result

    def argv(self,prompt,**options):
        # Reuse legacy argument validation without changing legacy execution.
        CodexAdapter.argv(self,prompt,**options)
        from .codex_qualification import args
        return args(self)

    def preflight(self,prompt,**options):
        argv=self.argv(prompt,**options)
        self.verify_qualification()
        proof=self._native_qualification
        if options.get('native_children'):
            raise HarnessError('Native Codex children are disabled')
        if options.get('read_only') and proof.role=='worker':
            raise HarnessError('A read-only invocation requires the read-only native policy')
        if options.get('write_authorized') and proof.role!='worker':
            raise HarnessError('A reviewer native policy cannot authorize writes')
        return argv

    def run(self,prompt,*,cwd,session_id=None,timeout=600,cancel_file=None,on_process=None,on_event=None,
            output_schema=None,native_children=False,read_only=False,log_dir=None,deadline=None,
            write_authorized=False,allow_non_git=False,reasoning_effort=None):
        from .codex_qualification import args,thread,census,complete_catalog,strict_json,readonly_call,verify_qualification_evidence,hook_identity
        from .codex_tool_catalog import _AppServer
        from .core import digest,now
        if type(timeout) not in (int,float) or not 0<timeout<=86400:
            raise NotDispatched('Native Codex timeout outside bound')
        if deadline is not None:
            timeout=min(timeout,(datetime.fromisoformat(deadline.replace('Z','+00:00'))-datetime.now(timezone.utc)).total_seconds())
        try:
            if timeout<=0:raise HarnessError('Native Codex task deadline expired')
            if cancel_file and Path(cancel_file).exists():raise HarnessError('Cancelled before native process start')
            cwd=canonical_directory(cwd)
            if str(cwd)!=self.tool_root:raise HarnessError('Native Codex worktree drift')
            self.preflight(prompt,session_id=session_id,output_schema=output_schema,native_children=native_children,
                read_only=read_only,write_authorized=write_authorized,allow_non_git=allow_non_git,reasoning_effort=reasoning_effort)
            schema=strict_json(read_regular_file(output_schema,max_bytes=2*1024*1024)) if output_schema else None
        except (HarnessError,OSError,ValueError) as error:raise NotDispatched(str(error)) from error
        folder=Path(log_dir) if log_dir else Path(__import__('tempfile').mkdtemp(prefix='harness-codex-native-'))
        _mkdir(folder);_parents(folder);_ordinary(folder,directory=True)
        token=uuid.uuid4().hex;stdout=folder/(token+'.jsonl');stderr=folder/(token+'.stderr.log')
        for path in (stdout,stderr):_ordinary(path,missing_ok=True)
        proof=self._native_qualification
        document=strict_json(read_regular_file(proof.receipt['path'],max_bytes=4*1024*1024))
        catalog_path=Path(document['runtime_dir'])/(token+'.jsonl')
        begin=time.monotonic();started_at=now();client=None;normalizer=None;events=[];terminal=[];errors=[];stream=None;bytes_written=0
        process_meta=None;signal_cancel=threading.Event();spawn_seconds=0
        def publish(event):
            nonlocal bytes_written
            clean=sanitized(dict(event,execution_id=token));raw=(json.dumps(clean,separators=(',',':'),ensure_ascii=False,allow_nan=False)+'\n').encode()
            bytes_written+=len(raw)
            if len(raw)>MAX_EVENT_BYTES or bytes_written>MAX_LOG_BYTES:raise HarnessError('Native normalized log bound exceeded')
            stream.write(raw);stream.flush();os.fsync(stream.fileno());events.append(clean)
            if on_event:on_event(clean)
        def notification(value):
            if normalizer is None:return
            for event in normalizer.feed(value['method'],value.get('params',{})):
                if event['type'] in {'turn.completed','turn.failed','turn.interrupted'}:terminal.append(event)
                else:publish(event)
        def server_request(value):
            params=value.get('params',{})
            if (value['method']!='item/tool/call' or normalizer is None or normalizer.terminal is not None
                    or params.get('threadId')!=normalizer.thread_id or params.get('turnId')!=normalizer.turn_id
                    or params.get('namespace') is not None):
                raise HarnessError('Unsolicited native approval/tool request')
            try:
                output=readonly_call(cwd,self.read_context,params.get('tool'),params.get('arguments'))
                text=json.dumps(output,separators=(',',':'),ensure_ascii=False)
                if len(text.encode())>48000:raise HarnessError('Source page byte bound exceeded; reduce limit')
                return {'success':True,'contentItems':[{'type':'inputText','text':text}]}
            except (HarnessError,OSError,ValueError) as error:
                return {'success':False,'contentItems':[{'type':'inputText','text':str(error)}]}
        def catalog(stage):
            actual,connected=census(client,normalizer.thread_id,self.tool_config)
            if actual!=document['mcp_catalog'] or tuple(connected)!=proof.connected:
                raise HarnessError('Native execution MCP catalog/connection drift')
            actual=complete_catalog(actual,document['builtins'])
            row={'schema_version':1,'proof_id':document['proof_id'],'stage':stage,'root':str(cwd),
                'role':proof.role,'session_id':normalizer.thread_id,'process_identity':client.identity,
                'execution_id':token,'turn_id':normalizer.turn_id,
                'catalog':actual,'catalog_digest':digest(actual)}
            raw=(json.dumps(row,sort_keys=True,separators=(',',':'))+'\n').encode()
            with catalog_path.open('ab') as output:output.write(raw);output.flush();os.fsync(output.fileno())
        outcome='unknown_outcome';known=False
        with _signal_scope(signal_cancel),stdout.open('xb') as stream:
            try:
                if signal_cancel.is_set() or cancel_file and Path(cancel_file).exists():
                    raise HarnessError('Native Codex cancelled before process start')
                native_argv=args(self);spawn=time.monotonic()
                client=_AppServer(native_argv,cwd,self.environment,timeout,on_notification=notification,on_request=server_request)
                spawn_seconds=time.monotonic()-spawn
                client.cancel_check=lambda: signal_cancel.is_set() or bool(cancel_file and Path(cancel_file).exists())
                process_meta={'identity':client.identity,'tree_control':'windows_suspended_job_kill_on_close' if os.name=='nt' else 'private_process_group',
                    'started_at':now(),'cwd':str(cwd),'argv_digest':hashlib.sha256(json.dumps(native_argv,ensure_ascii=False).encode()).hexdigest()}
                if on_process:on_process(process_meta)
                response=client.request('initialize',{'clientInfo':{'name':'agent-harness-native','version':'1'},'capabilities':{'experimentalApi':True}})
                if 'rpc_error' in response:raise HarnessError('Native initialize failed')
                client.notify('initialized')
                if hook_identity(self,client,cwd)!=document['hooks']:raise HarnessError('Native hook authority changed before dispatch')
                response=thread(client,cwd,proof.role,config=self.tool_config,session_id=session_id)
                if response.get('model')!=document['model'] or response.get('modelProvider')!=document['model_provider']:
                    raise HarnessError('Native model/provider drift')
                history=response['thread'].get('turns',[]) if session_id is not None else []
                if not isinstance(history,list) or any(not isinstance(item,dict) or not isinstance(item.get('id'),str) for item in history):
                    raise HarnessError('Malformed native resume history identities')
                normalizer=NativeCodexEvents(response['thread']['id'],historical_turn_ids=[item['id']for item in history])
                publish({'type':'thread.started','thread_id':normalizer.thread_id})
                catalog('before_dispatch')
                params={'threadId':normalizer.thread_id,'input':[{'type':'text','text':prompt}],
                    'environments':[]}
                if schema is not None:params['outputSchema']=schema
                if reasoning_effort is not None:params['effort']=reasoning_effort
                started=client.request('turn/start',params)
                if 'rpc_error' in started:raise HarnessError('Native turn start failed')
                normalizer.bind_turn_response(started.get('turn'))
                while normalizer.terminal is None:client.next_notification()
                # A response barrier consumes all earlier stdout messages. The
                # final census verifies the actual client is still connected.
                catalog('after_completion')
            except BaseException as error:
                errors.append(type(error).__name__+': '+str(error))
            finally:
                if client is not None:
                    try:client.close();client.drain()
                    except BaseException as error:errors.append(type(error).__name__+': '+str(error))
                if signal_cancel.is_set() or cancel_file and Path(cancel_file).exists():
                    errors.append('Cancellation requested; source reconciliation required');outcome='cancelled'
                if not errors and normalizer is not None:
                    try:
                        status=normalizer.finish()
                        if status=='completed':outcome='completed';known=True
                        elif status=='failed':outcome='failed';known=True
                        else:outcome='cancelled'
                        for event in terminal:
                            if event['type']=='turn.completed':event['usage']=normalizer.usage or {}
                            publish(event)
                    except BaseException as error:errors.append(type(error).__name__+': '+str(error));outcome='unknown_outcome';known=False
        stderr.write_text(sanitized('\n'.join(errors)),encoding='utf-8')
        result={'status':outcome,'outcome_known':known,'thread_id':normalizer.thread_id if normalizer else session_id,
            'turn_started':bool(normalizer and normalizer.turn_id),'turn_completed':outcome=='completed',
            'process_tree_stopped':client.stopped if client else True,'process':process_meta,
            'exit_code':client.process.returncode if client else None,'error':'; '.join(errors) if errors else None,
            'events':len(events),'observable_tool_calls':sum(event.get('item',{}).get('type') in {'mcp_tool_call','command_execution','file_change','dynamic_tool_call'} for event in events),
            'usage':normalizer.usage if normalizer else None,'elapsed_seconds':time.monotonic()-begin,'spawn_seconds':spawn_seconds,
            'started_at':started_at,'finished_at':now(),
            'logs':{'stdout':str(stdout),'stderr':str(stderr)},
            'log_sha256':{'stdout':hashlib.sha256(stdout.read_bytes()).hexdigest(),'stderr':hashlib.sha256(stderr.read_bytes()).hexdigest()},
            'backend':'codex','transport':'native_app_server'}
        if catalog_path.exists():result['catalog_artifacts']=[{'path':str(catalog_path),'sha256':hashlib.sha256(catalog_path.read_bytes()).hexdigest()}]
        if outcome=='completed':
            try:verify_qualification_evidence(proof.receipt,root=cwd,role=proof.role,require_execution=True,
                session_id=result['thread_id'],catalog_artifacts=result.get('catalog_artifacts'),
                execution_log={'path':str(stdout),'sha256':result['log_sha256']['stdout']})
            except (HarnessError,OSError,ValueError) as error:result.update(status='unknown_outcome',outcome_known=False,error=str(error))
        return result
