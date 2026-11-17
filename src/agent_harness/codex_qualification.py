"""Core-owned native Codex policy proofs; never trust an operator attestation.

The policy narrows the selected model's native tool metadata only. Provider,
model identity, reasoning settings, prompts and sandbox defaults are preserved.
"""
from __future__ import annotations
import copy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import time
from types import MappingProxyType

from .codex_execution import _binary_hash, _codex_home, _duplicate_free, sanitized
from .codex_tool_catalog import _AppServer, _toml, MAX_SERVERS, MAX_TOOLS, MAX_CURSOR_BYTES
from .core import HarnessError, digest, now
from .file_io import canonical_directory, read_regular_file
from .opencode_qualification import read_context_manifest
from .supervisor_store import atomic_write

ISSUER=object()
OBSERVED_VERSION='codex-cli 0.167.0'
TTL=60
DISABLED=('shell_tool','multi_agent','multi_agent_v2','apps','plugins','remote_plugin','browser_use',
    'computer_use','image_generation','code_mode','code_mode_only','code_mode_host','tool_suggest',
    'view_image','goals','daemon_auto_start')
READ_TOOLS=[{'type':'function','deferLoading':False,'name':'harness_read_file','description':'Read a regular source file or one exact declared evidence file. No commands or writes.',
    'inputSchema':{'type':'object','properties':{'path':{'type':'string'},'offset':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':1,'maximum':2000}},'required':['path'],'additionalProperties':False}},
    {'type':'function','deferLoading':False,'name':'harness_list_files','description':'List regular files within the assigned worktree. No external directory access.',
    'inputSchema':{'type':'object','properties':{},'additionalProperties':False}}]


def strict_json(raw):
    return json.loads(raw,object_pairs_hook=_duplicate_free,
        parse_constant=lambda _:(_ for _ in ()).throw(ValueError('Nonfinite JSON')))


def sources(adapter,root):
    environment=adapter.environment if adapter.environment is not None else os.environ
    home=_codex_home(adapter.environment)
    paths={home/'config.toml'}
    for parent in (Path(root),*Path(root).parents):
        paths.add(parent/'.codex'/'config.toml')
        if (parent/'.git').exists():break
    result=[]
    for path in sorted(paths,key=str):
        if path.exists():
            raw=read_regular_file(path,max_bytes=2*1024*1024)
            result.append({'path':str(path.resolve()),'sha256':hashlib.sha256(raw).hexdigest()})
    return result


def identity(adapter,root):
    root=canonical_directory(root);info=root.stat()
    environment=dict(adapter.environment if adapter.environment is not None else os.environ)
    return {'binary_sha256':_binary_hash(adapter.executable[0]),'argv':list(adapter.executable),
        'launchers':[{'path':str(Path(x).resolve()),'sha256':hashlib.sha256(read_regular_file(x)).hexdigest()}
            for x in adapter.executable[1:] if Path(x).is_file()],
        'root':str(root),'directory_identity':[info.st_dev,info.st_ino],
        'config':digest(adapter.tool_config),'sources':sources(adapter,root),'environment_digest':digest(environment),
        'read_context':read_context_manifest([x['path'] for x in adapter.read_context]),
        'metadata_sha256':hashlib.sha256(read_regular_file(adapter.metadata_path,max_bytes=4*1024*1024)).hexdigest(),
        'implementation':[{'path':str(Path(__file__).with_name(name)),
            'sha256':hashlib.sha256(read_regular_file(Path(__file__).with_name(name))).hexdigest()}
            for name in ('codex_qualification.py','codex_app_server.py','codex_policy_probe.py','codex_tool_catalog.py','codex_execution.py','runner.py')]}


def args(adapter,*,discovery=False):
    config={'agents':{'enabled':False},'features':{name:False for name in DISABLED},'web_search':'disabled',
        'tools':{'update_plan':{'enabled':False},'experimental_request_user_input':{'enabled':False}}}
    if not discovery:config.update(adapter.tool_config)
    # Disable declared user/project servers before initialization. Unknown
    # managed servers are subsequently rejected by the native census.
    import tomllib
    declared=set(config.get('mcp_servers',{}))
    servers={}
    for source in sources(adapter,adapter.tool_root):
        native=tomllib.loads(read_regular_file(source['path']).decode('utf-8-sig'))
        servers.update(native.get('mcp_servers',{}))
    argv=list(adapter.executable)
    for key,value in sorted(config.items()):argv+=['-c',key+'='+_toml(value)]
    for name,server in servers.items():
        if name not in declared:
            if not isinstance(server,dict):raise HarnessError('Invalid installed MCP declaration')
            # Never copy installed command/env/auth values into argv/receipts.
            if not __import__('re').fullmatch(r'[A-Za-z0-9_-]+',name):
                raise HarnessError('Unsupported installed MCP server identity')
            argv+=['-c','mcp_servers.'+name+'.enabled=false']
    return [*argv,'app-server','--stdio']


def initialized(adapter,root,timeout,*,discovery=False,on_notification=None,on_request=None):
    client=_AppServer(args(adapter,discovery=discovery),root,adapter.environment,timeout,
        on_notification=on_notification,on_request=on_request)
    try:
        response=client.request('initialize',{'clientInfo':{'name':'agent-harness-native','version':'1'},
            'capabilities':{'experimentalApi':True}})
        if 'rpc_error' in response:raise HarnessError('Native initialize failed')
        client.notify('initialized')
        return client
    except BaseException:
        client.close();raise


def thread(client,root,role,*,config=None,session_id=None,ephemeral=False):
    params={'cwd':str(root),'config':config or {},'environments':[],'runtimeWorkspaceRoots':[str(root)],
        'sandbox':'workspace-write' if role=='worker' else 'read-only'}
    if session_id is None:
        params.update(ephemeral=ephemeral,dynamicTools=READ_TOOLS,allowProviderModelFallback=False)
        response=client.request('thread/start',params)
    else:
        params.update(threadId=session_id)
        response=client.request('thread/resume',params)
    value=response.get('thread')
    if (not isinstance(value,dict) or not isinstance(value.get('id'),str) or value.get('cwd')!=str(root)
            or session_id is not None and value['id']!=session_id):
        raise HarnessError('EXACT_RESUME_DRIFT: native thread identity/worktree mismatch')
    sandbox=response.get('sandbox',{})
    if sandbox.get('type')!=('workspaceWrite' if role=='worker' else 'readOnly'):
        raise HarnessError('Native effective sandbox differs from its managed role')
    if role=='worker' and any(str(canonical_directory(path))!=str(root) for path in sandbox.get('writableRoots',[])):
        raise HarnessError('Native worker sandbox grants an undeclared writable root')
    return response


def census(client,thread_id,config):
    rows={};cursor=None;seen=set()
    while True:
        params={'threadId':thread_id,'limit':100,'detail':'toolsAndAuthOnly'}
        if cursor is not None:params['cursor']=cursor
        page=client.request('mcpServerStatus/list',params)
        if not isinstance(page.get('data'),list):raise HarnessError('Native MCP census unavailable')
        for row in page['data']:
            if not isinstance(row,dict) or not isinstance(row.get('name'),str) or row['name'] in rows:
                raise HarnessError('Native MCP census identity conflict')
            if row.get('runtimeStatus')=='disabled':continue
            rows[row['name']]=row
        if len(rows)>MAX_SERVERS:raise HarnessError('Native MCP server bound exceeded')
        cursor=page.get('nextCursor')
        if cursor is None:break
        if not isinstance(cursor,str) or not cursor or len(cursor)>MAX_CURSOR_BYTES or cursor in seen:
            raise HarnessError('Invalid native MCP census cursor')
        seen.add(cursor)
    expected=config.get('mcp_servers',{})
    expected={name:value for name,value in expected.items() if value.get('enabled',True)}
    if set(rows)!=set(expected):raise HarnessError('Native undeclared/missing MCP server')
    catalog=[];connected=[]
    for name,spec in sorted(expected.items()):
        actual=rows[name]
        if actual.get('runtimeStatus')!='connected' or actual.get('toolsError') is not None:
            raise HarnessError('Native live MCP connection unverified: '+name)
        tools=actual.get('tools')
        if not isinstance(tools,dict):raise HarnessError('Native MCP tools unavailable')
        allowed=spec.get('enabled_tools');denied=spec.get('disabled_tools',[])
        if allowed is None or set(tools)-set(allowed) or set(tools)&set(denied):
            raise HarnessError('Native MCP tool ceiling unverified: '+name)
        if spec.get('required') and set(allowed)-set(denied)-set(tools):
            raise HarnessError('Native required MCP tool missing: '+name)
        connected.append(name)
        for tool,value in sorted(tools.items()):
            if not isinstance(value,dict) or value.get('name')!=tool:raise HarnessError('Malformed native MCP tool')
            catalog.append({'server':name,'name':tool,'schema_digest':digest(value),'definition':sanitized(value)})
        if len(catalog)>MAX_TOOLS:raise HarnessError('Native MCP tool bound exceeded')
    return catalog,connected


def complete_catalog(mcp,builtins):
    result=[{'name':tool['name'],'origin':'core_dynamic','definition':tool} for tool in READ_TOOLS]
    resources={'list_mcp_resources','list_mcp_resource_templates','read_mcp_resource'}
    result.extend({'name':tool['name'],'origin':'native','definition':tool}
        for tool in builtins if mcp or tool['name'] not in resources)
    return [*result,*mcp]


def readonly_call(root,manifest,tool,arguments):
    if not isinstance(arguments,dict):raise HarnessError('Invalid native dynamic tool arguments')
    root=canonical_directory(root)
    if tool=='harness_list_files':
        if arguments:raise HarnessError('Unknown list arguments')
        files=[]
        for path in sorted(root.rglob('*')):
            if '.git' in path.relative_to(root).parts or path.is_symlink() or not path.is_file():continue
            if root not in path.resolve().parents:continue
            files.append(path.relative_to(root).as_posix())
            if len(files)>10000:raise HarnessError('Source listing bound exceeded')
        return {'files':files}
    if tool!='harness_read_file' or set(arguments)-{'path','offset','limit'}:
        raise HarnessError('Forbidden native dynamic tool')
    value=arguments.get('path')
    if not isinstance(value,str) or not value or '\0' in value:raise HarnessError('Invalid source path')
    path=Path(value) if Path(value).is_absolute() else root/value
    target=path.resolve(strict=True)
    if '.git' in target.relative_to(root).parts if target.is_relative_to(root) else False:
        raise HarnessError('Private Git state is not source context')
    if not target.is_relative_to(root):
        if not any(str(target)==entry['path'] for entry in manifest):
            raise HarnessError('Undeclared external source file')
        current=read_context_manifest([target])
        if not any(current[0]==entry for entry in manifest):raise HarnessError('Undeclared external source file')
    raw=read_regular_file(path,max_bytes=4*1024*1024)
    offset=arguments.get('offset',0);limit=arguments.get('limit',2000)
    if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=2000:
        raise HarnessError('Invalid source pagination')
    lines=raw.decode('utf-8').splitlines()
    return {'path':str(target),'sha256':hashlib.sha256(raw).hexdigest(),'offset':offset,'total_lines':len(lines),
        'text':'\n'.join(lines[offset:offset+limit])}


@dataclass(frozen=True)
class Qualification:
    issuer:object
    subject:dict
    role:str
    created:float
    receipt:dict
    catalog:tuple
    connected:tuple
    def valid(self,adapter,root):
        return self.issuer is ISSUER and time.monotonic()-self.created<TTL and self.subject==identity(adapter,root)


def hook_identity(adapter,client,root):
    """Keep the required harness bootstrap; reject opaque/agent model hooks."""
    value=client.request('hooks/list',{'cwds':[str(root)]})
    if not isinstance(value.get('data'),list):raise HarnessError('Native hook inventory unavailable')
    hooks=[]
    for entry in value['data']:
        if entry.get('errors'):raise HarnessError('Native hook discovery is incomplete')
        hooks.extend(hook for hook in entry.get('hooks',[]) if hook.get('enabled'))
    if not hooks:return []
    home=_codex_home(adapter.environment)
    pointer=strict_json(read_regular_file(home/'harness-machine.json',max_bytes=65536))
    package=canonical_directory(pointer['package_root'])
    from .installation import hook_payload
    launcher,payload,_=hook_payload(package,home)
    if read_regular_file(launcher,max_bytes=65536)!=payload:
        raise HarnessError('Native harness lifecycle launcher drift')
    import shlex
    from .receipt_identity import package_build_digest
    library=package/'src'/'agent_harness'
    if not library.is_dir():library=package/'agent_harness'
    package_digest=package_build_digest(library)
    result=[]
    for hook in hooks:
        command=hook.get('command','')
        argv=shlex.split(command)
        if (hook.get('handlerType')!='command' or hook.get('eventName')!='sessionStart'
                or hook.get('trustStatus') not in {'trusted','managed'} or len(argv)!=2
                or argv[1]!=str(launcher) or not Path(argv[0]).samefile(__import__('sys').executable)):
            raise HarnessError('BLOCKED_NOT_QUALIFIED: unsupported native hook authority')
        result.append({'event':hook['eventName'],'command_digest':digest(command),
            'native_hash':hook['currentHash'],'source':hook['sourcePath'],
            'source_digest':hashlib.sha256(read_regular_file(hook['sourcePath'])).hexdigest(),
            'launcher':str(launcher),'launcher_digest':hashlib.sha256(payload).hexdigest(),
            'package_root':str(package),'package_digest':package_digest})
    return result


def _write(path,value):
    raw=(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False)+'\n').encode()
    if path.exists():
        if read_regular_file(path,max_bytes=4*1024*1024)!=raw:raise HarnessError('Immutable native Codex evidence changed')
    else:atomic_write(path,raw,exclusive=True)
    return {'path':str(path.resolve()),'sha256':hashlib.sha256(raw).hexdigest()}


def qualify(adapter,*,root,role,evidence_dir,timeout=60,session_id=None):
    if role not in {'worker','reviewer','planner'}:raise HarnessError('Unknown strict Codex role')
    if type(timeout) not in (int,float) or not 0<timeout<=120:raise HarnessError('Native Codex qualification timeout bound')
    from .codex_execution import CodexAdapter
    if CodexAdapter.capabilities(adapter).get('version')!=OBSERVED_VERSION:
        raise HarnessError('UNSUPPORTED_CAPABILITY: native Codex version is not qualified')
    root=canonical_directory(root)
    if str(root)!=adapter.tool_root:raise HarnessError('Native Codex qualification worktree mismatch')
    if adapter.read_context and role=='worker':raise HarnessError('External read context requires a read-only role')
    deadline=time.monotonic()+timeout
    client=None
    try:
        client=initialized(adapter,root,min(15,timeout),discovery=True)
        config=client.request('config/read',{'cwd':str(root),'includeLayers':False}).get('config',{})
        if role=='worker' and (config.get('sandbox_mode')=='read-only' or config.get('default_permissions') is not None):
            raise HarnessError('Installed native permissions prohibit or require separate qualification of task writes')
        model=config.get('model');provider=config.get('model_provider') or 'openai'
        if not isinstance(model,str) or not model:
            models=client.request('model/list',{'includeHidden':True}).get('data',[])
            defaults=[item['id'] for item in models if item.get('isDefault')]
            if len(defaults)!=1:raise HarnessError('Selected native model is not resolved')
            model=defaults[0]
        hooks=hook_identity(adapter,client,root)
    finally:
        if client is not None:client.close();client.drain()
    cache=_codex_home(adapter.environment)/'models_cache.json'
    source=strict_json(read_regular_file(cache,max_bytes=4*1024*1024))
    matching=[item for item in source.get('models',[]) if item.get('slug')==model]
    if len(matching)!=1:raise HarnessError('Selected native model metadata is unavailable; no model fallback')
    original=matching[0];metadata=copy.deepcopy(original)
    metadata.update(shell_type='disabled',tool_mode='direct',supports_search_tool=False,experimental_supported_tools=[])
    if role!='worker':metadata['apply_patch_tool_type']=None
    evidence=canonical_directory(evidence_dir);evidence.mkdir(parents=True,exist_ok=True,mode=0o700)
    if evidence==root or root in evidence.parents:raise HarnessError('Native Codex proof must be outside its worktree')
    stable=digest({'metadata':metadata,'role':role,'root':str(root),'servers':adapter._tool_config,
        'binary':_binary_hash(adapter.executable[0])})
    artifact=evidence.parent/('codex-policy-'+stable[:32]);artifact.mkdir(parents=True,exist_ok=True,mode=0o700)
    metadata_path=artifact/'models.json';metadata_ref=_write(metadata_path,{'models':[metadata]})
    original_ref=_write(artifact/'original-model.json',original)
    clone=copy.copy(adapter);clone.metadata_path=str(metadata_path)
    clone.tool_config={**copy.deepcopy(adapter._tool_config),'model_catalog_json':str(metadata_path),
        'agents':{'enabled':False},'features':{name:False for name in DISABLED},'web_search':'disabled',
        'tools':{'update_plan':{'enabled':False},'experimental_request_user_input':{'enabled':False}}}
    if role=='worker':clone.tool_config['sandbox_workspace_write']={'writable_roots':[]}
    folder=evidence/('qualification-'+__import__('secrets').token_hex(12));folder.mkdir(mode=0o700)
    runtime=folder/'runtime';runtime.mkdir(mode=0o700)
    from .codex_policy_probe import probe
    native_probe,builtins=probe(clone,metadata,role,folder/'native-policy-probe',timeout=min(90,deadline-time.monotonic()))
    client=None
    try:
        remaining=deadline-time.monotonic()
        if remaining<=0:raise HarnessError('Native Codex qualification deadline exhausted')
        client=initialized(clone,root,remaining)
        if hook_identity(clone,client,root)!=hooks:raise HarnessError('Native hook authority changed')
        response=thread(client,root,role,config=clone.tool_config,ephemeral=True)
        if response.get('model')!=model or response.get('modelProvider')!=provider:
            raise HarnessError('Native selected model/provider changed')
        mcp_catalog,connected=census(client,response['thread']['id'],clone.tool_config)
        catalog=complete_catalog(mcp_catalog,builtins)
        if session_id is not None:thread(client,root,role,config=clone.tool_config,session_id=session_id)
    finally:
        if client is not None:client.close();client.drain()
    subject=identity(clone,root)
    proof={'schema_version':1,'kind':'codex-native-core-qualification','proof_id':__import__('uuid').uuid4().hex,
        'root':str(root),'role':role,'model':model,'model_provider':provider,'identity':subject,
        'metadata':metadata_ref,'metadata_source':{**original_ref,'model_digest':digest(original)},
        'native_policy_probe':native_probe,'hooks':hooks,'runtime_dir':str(runtime),'catalog':catalog,
        'catalog_digest':digest(catalog),'mcp_catalog':mcp_catalog,'builtins':builtins,'connected_mcp_servers':connected,
        'read_context':list(clone.read_context),'created_at':now()}
    ref=_write(folder/'proof.json',proof)
    clone._native_qualification=Qualification(ISSUER,subject,role,time.monotonic(),ref,tuple(catalog),tuple(connected))
    return clone


def verify_qualification_evidence(ref,*,root,role,require_execution=False,session_id=None,catalog_artifacts=None,execution_log=None):
    if not isinstance(ref,dict) or set(ref)!={'path','sha256'}:raise HarnessError('Invalid native Codex qualification reference')
    raw=read_regular_file(ref['path'],max_bytes=4*1024*1024)
    if hashlib.sha256(raw).hexdigest()!=ref['sha256']:raise HarnessError('Native Codex qualification evidence changed')
    proof=strict_json(raw)
    fields={'schema_version','kind','proof_id','root','role','model','model_provider','identity','metadata','metadata_source',
        'native_policy_probe','hooks','runtime_dir','catalog','catalog_digest','mcp_catalog','builtins','connected_mcp_servers','read_context','created_at'}
    if (not isinstance(proof,dict) or set(proof)!=fields or proof['schema_version']!=1
            or proof['kind']!='codex-native-core-qualification' or proof['root']!=str(canonical_directory(root))
            or proof['role']!=role or digest(proof['catalog'])!=proof['catalog_digest']):
        raise HarnessError('Native Codex qualification subject changed')
    for field in ('metadata','native_policy_probe'):
        item=proof[field]
        if (not isinstance(item,dict) or set(item)!={'path','sha256'}
                or hashlib.sha256(read_regular_file(item['path'],max_bytes=4*1024*1024)).hexdigest()!=item['sha256']):
            raise HarnessError('Native Codex qualification closure changed')
    original_raw=read_regular_file(proof['metadata_source']['path'],max_bytes=4*1024*1024)
    if hashlib.sha256(original_raw).hexdigest()!=proof['metadata_source']['sha256']:
        raise HarnessError('Native Codex original metadata changed')
    original=strict_json(original_raw);expected=copy.deepcopy(original)
    expected.update(shell_type='disabled',tool_mode='direct',supports_search_tool=False,experimental_supported_tools=[])
    if role!='worker':expected['apply_patch_tool_type']=None
    actual_metadata=strict_json(read_regular_file(proof['metadata']['path'],max_bytes=4*1024*1024))
    if (digest(original)!=proof['metadata_source']['model_digest'] or actual_metadata!={'models':[expected]}
            or expected['slug']!=proof['model']):
        raise HarnessError('Native Codex model/context/reasoning metadata changed outside its tool ceiling')
    for implementation in proof['identity'].get('implementation',[]):
        if hashlib.sha256(read_regular_file(implementation['path'])).hexdigest()!=implementation['sha256']:
            raise HarnessError('Native Codex implementation source changed')
    if read_context_manifest([item['path']for item in proof['read_context']])!=proof['read_context']:
        raise HarnessError('Native Codex read-context identity changed')
    from .receipt_identity import package_build_digest
    for hook in proof['hooks']:
        if (hashlib.sha256(read_regular_file(hook['source'])).hexdigest()!=hook['source_digest']
                or hashlib.sha256(read_regular_file(hook['launcher'],max_bytes=65536)).hexdigest()!=hook['launcher_digest']):
            raise HarnessError('Native Codex lifecycle hook source changed')
        package=Path(hook['package_root']);library=package/'src'/'agent_harness'
        if not library.is_dir():library=package/'agent_harness'
        if package_build_digest(library)!=hook['package_digest']:
            raise HarnessError('Native Codex lifecycle hook package changed')
    native_probe=strict_json(read_regular_file(proof['native_policy_probe']['path'],max_bytes=4*1024*1024))
    scenarios={'mcp__fixture__read_file','exec_command','spawn_agent','mcp__fixture__write_file','mcp__fixture__execute_command'}
    if role!='worker':scenarios.add('apply_patch')
    observations=native_probe.get('observations',[])
    if (native_probe.get('kind')!='codex-native-policy-probe' or native_probe.get('metadata_digest')!=digest(expected)
            or native_probe.get('role')!=role or native_probe.get('builtins')!=proof['builtins']
            or len(observations)!=len(scenarios) or {item.get('scenario')for item in observations}!=scenarios
            or complete_catalog(proof['mcp_catalog'],proof['builtins'])!=proof['catalog']):
        raise HarnessError('Native Codex policy probe coverage incomplete')
    for item in native_probe['observations']:
        if (item.get('provider_requests',0)<2 or item.get('sentinel_unchanged') is not True or item.get('process_tree_stopped') is not True
                or item.get('mcp_calls')!=(['read_file'] if item['scenario']=='mcp__fixture__read_file' else [])):
            raise HarnessError('Native Codex negative/positive policy evidence is not confirmed')
        for name,path in item['logs'].items():
            if hashlib.sha256(read_regular_file(path,max_bytes=32*1024*1024)).hexdigest()!=item['log_sha256'][name]:
                raise HarnessError('Native Codex policy probe log changed')
    if not require_execution:return proof
    if not session_id or not isinstance(catalog_artifacts,list) or len(catalog_artifacts)!=1:
        raise HarnessError('Completed native Codex execution lacks its catalog')
    artifact=catalog_artifacts[0];path=Path(artifact['path'])
    if path.parent!=Path(proof['runtime_dir']):raise HarnessError('Native Codex runtime catalog escaped its proof')
    raw=read_regular_file(path,max_bytes=4*1024*1024)
    if hashlib.sha256(raw).hexdigest()!=artifact['sha256'] or not raw.endswith(b'\n'):
        raise HarnessError('Native Codex runtime catalog changed')
    rows=[strict_json(line)for line in raw.splitlines()]
    if len(rows)!=2 or [row.get('stage')for row in rows]!=['before_dispatch','after_completion']:
        raise HarnessError('Native Codex actual-thread catalog closure incomplete')
    for row in rows:
        if (row.get('proof_id')!=proof['proof_id'] or row.get('session_id')!=session_id or row.get('root')!=proof['root']
                or row.get('role')!=role or row.get('catalog')!=proof['catalog'] or row.get('catalog_digest')!=proof['catalog_digest']):
            raise HarnessError('Native Codex actual-thread catalog subject changed')
    if rows[0]['process_identity']!=rows[1]['process_identity']:raise HarnessError('Native Codex client process changed')
    execution_id=rows[0].get('execution_id')
    if (not isinstance(execution_id,str) or not execution_id or rows[1].get('execution_id')!=execution_id
            or rows[0].get('turn_id') is not None or not isinstance(rows[1].get('turn_id'),str)):
        raise HarnessError('Native Codex runtime invocation/turn identity changed')
    if not isinstance(execution_log,dict) or set(execution_log)!={'path','sha256'}:
        raise HarnessError('Native Codex completion lacks pinned normalized log')
    raw=read_regular_file(execution_log['path'],max_bytes=32*1024*1024)
    if hashlib.sha256(raw).hexdigest()!=execution_log['sha256'] or not raw.endswith(b'\n'):
        raise HarnessError('Native Codex normalized log changed')
    events=[strict_json(line)for line in raw.splitlines()]
    if any(event.get('execution_id')!=execution_id for event in events):
        raise HarnessError('Native Codex catalog/log invocation identity changed')
    if (sum(event.get('type')=='thread.started' and event.get('thread_id')==session_id for event in events)!=1
            or sum(event.get('type')=='turn.started'for event in events)!=1
            or sum(event.get('type')=='turn.completed'for event in events)!=1
            or any(event.get('type')in{'turn.failed','turn.interrupted'}for event in events)):
        raise HarnessError('Native Codex completion stream is incomplete')
    return proof
