"""Fresh per-arm client homes and pinned launch plans, never qualification.

Preparing files does not prove effective client configuration, tool isolation,
child role mapping, hook trust, or a hard provider token cap. No model is called.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import shutil
import time
import tomllib
import uuid

from .core import HarnessError, digest
from .file_io import ParentDirectory, parent_binding, read_regular_file, verify_parent_binding
from . import installation, strict_yaml
from .receipt_identity import package_build_digest
from .execution_ledger import HASH, SHORT, object_schema
from .runner import _contained_capture

ARMS = {'plain_stock', 'plain_matched', 'harness_full', 'harness_matched'}


REF=object_schema({'path':{'type':'string','minLength':1,'maxLength':4096},'sha256':HASH},('path','sha256'))
PROFILE_V1=object_schema({'schema_version':{'const':1},'record_type':{'const':'benchmark_prepared_profile'},'profile_id':SHORT,**{name:REF for name in ('resources_ref','images_ref','network_ref','state_ref','root_policy_ref','child_policy_ref')},'suites':{'type':'array','minItems':3,'maxItems':3,'items':object_schema({'suite':{'enum':['swe-bench-verified','terminal-bench-2.1','aider-polyglot']},'cohort_ref':REF,'evaluator_ref':REF},('suite','cohort_ref','evaluator_ref'))}},('schema_version','record_type','profile_id','resources_ref','images_ref','network_ref','state_ref','root_policy_ref','child_policy_ref','suites'))
IDENTITY={'type':'object','additionalProperties':False,'properties':{'device':{'type':['integer','string']},'inode':{'type':['integer','string']},'identity_kind':{'const':'windows-file-id-info-v1'}},'required':['device','inode']}
BINDING=object_schema({'parents':{'type':'array','minItems':1,'maxItems':256,'items':object_schema({'path':SHORT,'device':{'type':['integer','string']},'inode':{'type':['integer','string']}},('path','device','inode'))},'physical':IDENTITY},('parents','physical'))
HOME_KEYS=('environment','user','codex_home','skills_home','state_home')
ARM_HOME_RECEIPT=object_schema({'schema_version':{'const':1},'record_type':{'const':'benchmark_arm_home_receipt'},'status':{'const':'prepared'},'trial_id':HASH,'arm':{'enum':sorted(ARMS)},'manifest_digest':HASH,'profile_digest':HASH,
 'profile':PROFILE_V1,'profile_ref':{'anyOf':[REF,{'type':'null'}]},
 'package':object_schema({'build_digest':HASH,'payload_digest':HASH},('build_digest','payload_digest')),
 'package_root':SHORT,'assets_root':SHORT,'trial_root':SHORT,
 'homes':object_schema({name:SHORT for name in HOME_KEYS},HOME_KEYS),
 'root_bindings':object_schema({name:BINDING for name in HOME_KEYS},HOME_KEYS),
 'file_manifest_ref':REF,'launch_plan_ref':REF,'prepare_observation_ref':REF,'root_policy_ref':REF,'child_policy_ref':REF,
 'policy_coverage':object_schema({name:SHORT for name in ('root','child','max_compaction_tokens','max_tokens')},('root','child','max_compaction_tokens','max_tokens')),
 'ancestry':object_schema({'workspace':{'type':['string','null'],'maxLength':4096},'git_root_observed':{'type':'boolean'},'ancestor_documents':{'type':'array','maxItems':512,'items':REF},'client_document_boundary':{'enum':['unavailable','declared_git_root_not_runtime_qualified']},'qualification':{'const':'unavailable'}},('workspace','git_root_observed','ancestor_documents','client_document_boundary','qualification')),'blockers':{'type':'array','maxItems':32,'items':SHORT}},
 ('schema_version','record_type','status','trial_id','arm','manifest_digest','profile_digest','profile','profile_ref','package','package_root','assets_root','trial_root','homes','root_bindings','file_manifest_ref','launch_plan_ref','prepare_observation_ref','root_policy_ref','child_policy_ref','policy_coverage','ancestry','blockers'))


ARM_LAUNCH_OBSERVATION=object_schema({'schema_version':{'const':1},'record_type':{'const':'benchmark_arm_launch_observation'},
 'trial_id':HASH,'manifest_digest':HASH,'profile_digest':HASH,'environment_digest':HASH,
 'environment_binding':{'enum':['observed_explicit_transport_argument_mapping','declared_mapping_not_launched']},
 'process':{'anyOf':[object_schema({'identity':object_schema({'pid':{'type':'integer','minimum':1},'created':SHORT},('pid','created')),
     'tree_control':{'enum':['private_process_group','windows_suspended_job_kill_on_close']},'started_at':SHORT,'cwd':SHORT,'argv_digest':HASH},
     ('identity','tree_control','started_at','cwd','argv_digest')),{'type':'null'}]},
 'status':{'enum':['completed','failed','timed_out','cancelled','unknown_outcome']},'outcome_known':{'type':'boolean'},
 'transport_ref':REF,'arm_home_ref':REF,'ancestry':ARM_HOME_RECEIPT['properties']['ancestry'],
 'policy_blockers':{'type':'array','maxItems':32,'items':SHORT},'effective_client_configuration_observed':{'const':False}},
 ('schema_version','record_type','trial_id','manifest_digest','profile_digest','environment_digest','environment_binding','process','status','outcome_known','transport_ref','arm_home_ref','ancestry','policy_blockers','effective_client_configuration_observed'))


def _safe_join(root,relative):
    if not isinstance(relative,str) or '\\' in relative or ':' in relative or any(part in ('','.','..') for part in relative.split('/')):
        raise HarnessError('BENCH_ENVIRONMENT_PATH_ESCAPE')
    return Path(root).joinpath(*relative.split('/'))


def _present(path):
    try:info=Path(path).lstat()
    except FileNotFoundError:return False
    if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
        raise HarnessError('BENCH_ENVIRONMENT_LINK_REFUSED')
    return True


def _check(deadline):
    if deadline is not None:
        if not isinstance(deadline,str) or len(deadline)>128:raise HarnessError('BENCH_ENVIRONMENT_DEADLINE_INVALID')
        try:end = datetime.fromisoformat(deadline.replace('Z','+00:00'))
        except ValueError as error:raise HarnessError('BENCH_ENVIRONMENT_DEADLINE_INVALID') from error
        if end.tzinfo is None or datetime.now(timezone.utc) >= end:
            raise HarnessError('BENCH_ENVIRONMENT_DEADLINE')


def _json(value):
    return (json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode('utf-8')


def _ref(path):
    raw = read_regular_file(path, max_bytes=16*1024*1024)
    return {'path':str(Path(path).absolute()),'sha256':hashlib.sha256(raw).hexdigest()}


def _read_ref(ref, base=None):
    if not isinstance(ref,dict) or set(ref) != {'path','sha256'} or not re.fullmatch('[0-9a-f]{64}',ref.get('sha256','')):
        raise HarnessError('BENCH_ENVIRONMENT_BAD_REFERENCE')
    path = Path(ref['path'])
    if base is not None:
        if path.is_absolute() or path.anchor or '..' in path.parts or any(':' in p for p in path.parts):
            raise HarnessError('BENCH_ENVIRONMENT_UNSAFE_ASSET')
        path = Path(base)/path
    elif not path.is_absolute():
        raise HarnessError('BENCH_ENVIRONMENT_ABSOLUTE_REFERENCE_REQUIRED')
    raw = read_regular_file(path,max_bytes=16*1024*1024)
    if hashlib.sha256(raw).hexdigest() != ref['sha256']:
        raise HarnessError('BENCH_ENVIRONMENT_STALE_REFERENCE')
    return raw


def _save(path, raw, deadline=None):
    _check(deadline)
    with ParentDirectory(path,create=True) as parent:
        _check(deadline)
        fd = parent.open(os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_BINARY',0))
        with os.fdopen(fd,'wb') as output:
            output.write(raw);output.flush();os.fsync(output.fileno())
        parent.sync();parent.check()


def _publish(path,raw,deadline):
    # Cooperative boundary immediately before the actual publication primitive.
    # An already running filesystem operation cannot be interrupted safely.
    _check(deadline)
    return installation.atomic(path,raw)


def _identity(path):
    with ParentDirectory(Path(path)/'.binding') as parent:
        return {'parents':parent.binding,'physical':parent.physical_identity()}


def _inventory(root, deadline=None):
    rows,total = {},0
    root=Path(root)
    if not _present(root):return rows
    if not root.is_dir():raise HarnessError('BENCH_ENVIRONMENT_DIRECTORY_REQUIRED')
    for path in sorted(Path(root).rglob('*')):
        _check(deadline)
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
            raise HarnessError('BENCH_ENVIRONMENT_LINK_REFUSED')
        if stat.S_ISDIR(info.st_mode):
            continue
        raw = read_regular_file(path,max_bytes=2*1024*1024)
        total += len(raw)
        if len(rows)>=10000 or total>64*1024*1024:
            raise HarnessError('BENCH_ENVIRONMENT_INVENTORY_BOUND')
        rows[path.relative_to(root).as_posix()]={'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)}
    return rows


def _toml(values):
    lines=[]
    for key,value in values.items():
        if not re.fullmatch('[A-Za-z_][A-Za-z0-9_]*',key) or type(value) not in (str,int,bool):
            raise HarnessError('BENCH_ENVIRONMENT_UNSUPPORTED_ROLE_CONFIG')
        lines.append(key+' = '+(json.dumps(value,ensure_ascii=False) if type(value) in (str,bool) else str(value)))
    return ('\n'.join(lines)+'\n').encode('utf-8')


def _settings(policy):
    for key in ('model','provider','reasoning_effort'):
        if not isinstance(policy.get(key),str) or not policy[key] or '\x00' in policy[key]:
            raise HarnessError('BENCH_ENVIRONMENT_POLICY_INVALID')
    if type(policy.get('max_compaction_tokens')) is not int or policy['max_compaction_tokens']<=0:
        raise HarnessError('BENCH_ENVIRONMENT_POLICY_INVALID')
    return {'model':policy['model'],'model_provider':policy['provider'],
        'model_reasoning_effort':policy['reasoning_effort'],
        'model_auto_compact_token_limit':policy['max_compaction_tokens']}


class ArmHomeBuilder:
    def __init__(self,package_root,assets_root,*,tool_path=None):
        self.package_root=Path(package_root).absolute()
        self.assets_root=Path(assets_root).absolute()
        self.tool_path=tool_path if tool_path is not None else os.environ.get('PATH',os.defpath)
        self.tool_path_source='explicit_declared_path' if tool_path is not None else 'allowlisted_ambient_path_unqualified'
        if not isinstance(self.tool_path,str) or '\x00' in self.tool_path or len(self.tool_path)>65536:raise HarnessError('BENCH_ENVIRONMENT_TOOL_PATH_INVALID')

    def _profile(self,wrapped):
        profile=wrapped['profile']
        strict_yaml.validate(profile,PROFILE_V1)
        if Path(wrapped.get('assets_root',self.assets_root)).absolute()!=self.assets_root:raise HarnessError('BENCH_ENVIRONMENT_ASSET_ROOT_CHANGED')
        if digest(profile)!=wrapped['profile_digest']:
            raise HarnessError('BENCH_ENVIRONMENT_PROFILE_CHANGED')
        if 'profile_ref' in wrapped and strict_yaml.loads(_read_ref(wrapped['profile_ref'],self.assets_root))!=profile:
            raise HarnessError('BENCH_ENVIRONMENT_PROFILE_CHANGED')
        docs={}
        for name,value in wrapped['documents'].items():
            ref=profile.get(name+'_ref')
            if ref is not None:
                actual=strict_yaml.loads(_read_ref(ref,self.assets_root))
                if actual!=value:
                    raise HarnessError('BENCH_ENVIRONMENT_POLICY_CHANGED')
                docs[name]=actual
        if not {'root_policy','child_policy'}<=docs.keys():
            raise HarnessError('BENCH_ENVIRONMENT_POLICY_MISSING')
        return profile,docs

    def _package(self):
        code=self.package_root/'src/agent_harness'
        if not code.is_dir():
            code=self.package_root/'agent_harness'
        if not code.is_dir():
            raise HarnessError('BENCH_ENVIRONMENT_PACKAGE_CODE_MISSING')
        payload={}
        for name in ('policies','adapters','workflows','roles','skills','tools'):
            directory=self.package_root/name
            if directory.is_dir():
                for relative,row in _inventory(directory).items():
                    if '__pycache__' not in Path(relative).parts and not relative.endswith('.pyc'):
                        payload[name+'/'+relative]=row
        return {'build_digest':package_build_digest(code),'payload_digest':digest(payload)}

    def _ancestry(self,workspace,deadline=None):
        if workspace is None:
            return {'workspace':None,'git_root_observed':False,'ancestor_documents':[],
                'client_document_boundary':'unavailable','qualification':'unavailable'}
        workspace=Path(workspace).absolute()
        with ParentDirectory(workspace/'.binding'):
            pass
        _check(deadline)
        git_root=False
        if _present(workspace/'.git') and (workspace/'.git').is_dir():
            environment={'PATH':self.tool_path,'HOME':str(workspace),'USERPROFILE':str(workspace),
                'GIT_CONFIG_GLOBAL':os.devnull,'GIT_CONFIG_NOSYSTEM':'1','GIT_OPTIONAL_LOCKS':'0',
                'GIT_CEILING_DIRECTORIES':str(workspace.parent),'LANG':'C.UTF-8'}
            if os.name=='nt':environment['SystemRoot']=os.environ['SystemRoot']
            timeout=3
            if deadline is not None:timeout=min(timeout,max(.001,(datetime.fromisoformat(deadline.replace('Z','+00:00'))-datetime.now(timezone.utc)).total_seconds()))
            git=shutil.which('git',path=self.tool_path)
            if git is None:raise HarnessError('BENCH_ENVIRONMENT_GIT_UNAVAILABLE')
            # Pin both original spellings before comparing physical directories.
            # resolve() on just Git's spelling loses Windows aliases and could
            # hide a reparse ancestor before the no-link check.
            with ParentDirectory(workspace/'.binding') as workspace_parent:
                result=_contained_capture([str(Path(git).resolve()),'-C',str(workspace),'rev-parse','--show-toplevel'],timeout=timeout,env=environment)
                if result.returncode==0:
                    top=Path(os.fsdecode(result.stdout.removesuffix(b'\n').removesuffix(b'\r')))
                    if top.is_absolute():
                        with ParentDirectory(top/'.binding') as top_parent:
                            git_root=workspace_parent.physical_identity()==top_parent.physical_identity()
        _check(deadline)
        documents=[]
        for parent in workspace.parents:
            for name in ('AGENTS.md','AGENTS.override.md'):
                path=parent/name
                if _present(path):documents.append(_ref(path))
        return {'workspace':str(workspace),'git_root_observed':git_root,'ancestor_documents':documents,
            'client_document_boundary':'declared_git_root_not_runtime_qualified' if git_root else 'unavailable',
            'qualification':'unavailable'}

    def prepare_arm(self,row,validated_profile,trial_root,*,manifest_digest,deadline=None,workspace=None):
        started=time.perf_counter()
        _check(deadline)
        if row.get('arm') not in ARMS or not re.fullmatch('[0-9a-f]{64}',row.get('trial_id','')) or not re.fullmatch('[0-9a-f]{64}',manifest_digest):
            raise HarnessError('BENCH_ENVIRONMENT_SUBJECT_INVALID')
        profile,docs=self._profile(validated_profile)
        root_settings=_settings(docs['root_policy'])
        roles=docs['child_policy']['roles']
        if not isinstance(roles,list) or not 1<=len(roles)<=32:raise HarnessError('BENCH_ENVIRONMENT_ROLE_INVALID')
        if len({role['role'] for role in roles})!=len(roles):raise HarnessError('BENCH_ENVIRONMENT_ROLE_INVALID')
        for role in roles:
            if not re.fullmatch('[A-Za-z_][A-Za-z0-9_-]{0,63}',role['role']):raise HarnessError('BENCH_ENVIRONMENT_ROLE_INVALID')
            _settings(role)
        package=self._package()
        _check(deadline)
        trial_root=Path(trial_root).absolute()
        if workspace is not None and not Path(workspace).absolute().is_relative_to(trial_root):
            raise HarnessError('BENCH_ENVIRONMENT_WORKSPACE_ESCAPE')
        root=trial_root/'arm-environment'
        with ParentDirectory(root) as parent:
            _check(deadline)
            if parent.fd is not None:
                os.mkdir(root.name,mode=0o700,dir_fd=parent.fd)
            else:
                root.mkdir(mode=0o700)
            parent.check()
        user=root/'user';home=user/'.codex';skills=user/'.agents/skills';state=root/'harness-state'
        roots={'environment':root,'user':user,'codex_home':home,'skills_home':skills,'state_home':state}
        for directory in [user,home,skills,state,user/'.config',user/'.cache',user/'.local/share',user/'.local/state',root/'tmp',user/'AppData/Roaming',user/'AppData/Local']:
            _check(deadline)
            with ParentDirectory(directory/'.binding',create=True):
                pass
        _save(home/'config.toml',_toml(root_settings),deadline)
        full=row['arm'].startswith('harness_')
        if full:
            plans=installation.plan(self.package_root,home,skills,state,state/'machine.json')
            for plan in plans:
                _check(deadline)
                if not plan['path'].is_relative_to(root):
                    raise HarnessError('BENCH_ENVIRONMENT_PLAN_ESCAPE')
            for plan in plans:
                _publish(plan['path'],plan['after'],deadline)
        for role in docs['child_policy']['roles']:
            _check(deadline)
            name=role['role']
            if not re.fullmatch('[A-Za-z_][A-Za-z0-9_-]{0,63}',name):
                raise HarnessError('BENCH_ENVIRONMENT_ROLE_INVALID')
            path=home/'agents'/(name+'.toml')
            current=tomllib.loads(read_regular_file(path).decode('utf-8')) if path.exists() else {'name':name}
            current.update(_settings(role))
            if path.exists():_publish(path,_toml(current),deadline)
            else:_save(path,_toml(current),deadline)
        environment={'CODEX_HOME':str(home),'HOME':str(user),'USERPROFILE':str(user),
            'XDG_CONFIG_HOME':str(user/'.config'),'XDG_CACHE_HOME':str(user/'.cache'),
            'XDG_DATA_HOME':str(user/'.local/share'),'XDG_STATE_HOME':str(user/'.local/state'),
            'APPDATA':str(user/'AppData/Roaming'),'LOCALAPPDATA':str(user/'AppData/Local'),
            'TMPDIR':str(root/'tmp'),'TMP':str(root/'tmp'),'TEMP':str(root/'tmp'),
            'PATH':self.tool_path,'LANG':'C.UTF-8'}
        if os.name=='nt':environment['SystemRoot']=os.environ['SystemRoot']
        # Explicit allowlist only. No auth, plugin, MCP or user-profile variables.
        plan={'schema_version':1,'record_type':'benchmark_arm_launch_plan','environment':environment,
            'environment_digest':digest(environment),'profile_digest':validated_profile['profile_digest'],
            'tool_path_source':self.tool_path_source}
        _save(root/'launch-plan.json',_json(plan),deadline)
        immutable=_inventory(user,deadline)
        if (state/'machine.json').exists():
            immutable['@state/machine.json']={'sha256':_ref(state/'machine.json')['sha256'],'bytes':(state/'machine.json').stat().st_size}
        closed=[home/name for name in ('agents','skills','plugins','mcp')]+[user/'.agents',user/'.config']
        namespace={path.relative_to(root).as_posix():_inventory(path,deadline) for path in closed}
        definitions={name:_present(home/name) for name in ('AGENTS.md','AGENTS.override.md','hooks.json','config.toml','harness-machine.json','agent-harness-hook.py')}
        manifest={'schema_version':1,'files':immutable,'closed_namespaces':namespace,'definitions':definitions}
        _save(root/'files.json',_json(manifest),deadline)
        observation={'schema_version':1,'record_type':'benchmark_environment_prepare_observation',
            'trial_id':row['trial_id'],'manifest_digest':manifest_digest,'profile_digest':validated_profile['profile_digest'],
            'elapsed_seconds':time.perf_counter()-started,'counters':{'immutable_files_observed':len(immutable),'home_roots_created':len(roots)},
            'scope':'environment_prepare_only','effective_client_configuration_observed':False}
        _save(root/'prepare-observation.json',_json(observation),deadline)
        if self._package()!=package:
            raise HarnessError('BENCH_ENVIRONMENT_PACKAGE_CHANGED')
        _check(deadline)
        receipt={'schema_version':1,'record_type':'benchmark_arm_home_receipt','status':'prepared',
            'trial_id':row['trial_id'],'arm':row['arm'],'manifest_digest':manifest_digest,
            'profile_digest':validated_profile['profile_digest'],'profile':profile,'profile_ref':validated_profile.get('profile_ref'),
            'package':package,'package_root':str(self.package_root),'assets_root':str(self.assets_root),
            'trial_root':str(trial_root),'homes':{name:str(path) for name,path in roots.items()},
            'root_bindings':{name:_identity(path) for name,path in roots.items()},
            'file_manifest_ref':_ref(root/'files.json'),'launch_plan_ref':_ref(root/'launch-plan.json'),
            'prepare_observation_ref':_ref(root/'prepare-observation.json'),
            'ancestry':self._ancestry(workspace,deadline),
            'root_policy_ref':profile['root_policy_ref'],'child_policy_ref':profile['child_policy_ref'],
            'policy_coverage':{'root':'configured_file_projection','child':'configured_files_mapping_unqualified',
                'max_compaction_tokens':'configured_file_projection','max_tokens':'unavailable'},
            'blockers':['effective_client_configuration_unqualified','hard_max_tokens_mapping_unavailable',
                'observed_child_role_mapping_unqualified','runtime_boundary_isolation_unqualified']+(['hook_trust_and_lifecycle_unqualified'] if full else [])}
        if not receipt['ancestry']['git_root_observed']:
            receipt['blockers'].append('source_git_root_unavailable')
        strict_yaml.validate(receipt,ARM_HOME_RECEIPT)
        _save(root/'receipt.json',_json(receipt),deadline)
        self.verify_arm(receipt)
        # Include construction, package checks and verification. Publishing the
        # final timing observation itself is outside this declared timer scope.
        observation['elapsed_seconds']=time.perf_counter()-started
        observation['scope']='environment_prepare_through_verification_excluding_timing_receipt_publish'
        _publish(root/'prepare-observation.json',_json(observation),deadline)
        receipt['prepare_observation_ref']=_ref(root/'prepare-observation.json')
        _publish(root/'receipt.json',_json(receipt),deadline)
        _check(deadline)
        return receipt

    def verify_arm(self,receipt):
        strict_yaml.validate(receipt,ARM_HOME_RECEIPT)
        if receipt.get('record_type')!='benchmark_arm_home_receipt' or receipt.get('status')!='prepared':
            raise HarnessError('BENCH_ENVIRONMENT_RECEIPT_INVALID')
        root=Path(receipt['trial_root'])/'arm-environment'
        expected={'environment':str(root),'user':str(root/'user'),'codex_home':str(root/'user/.codex'),'skills_home':str(root/'user/.agents/skills'),'state_home':str(root/'harness-state')}
        if receipt['homes']!=expected or json.loads(read_regular_file(root/'receipt.json'))!=receipt:
            raise HarnessError('BENCH_ENVIRONMENT_RECEIPT_CHANGED')
        if self.package_root!=Path(receipt['package_root']) or self.assets_root!=Path(receipt['assets_root']) or self._package()!=receipt['package']:
            raise HarnessError('BENCH_ENVIRONMENT_PACKAGE_CHANGED')
        if digest(receipt['profile'])!=receipt['profile_digest']:
            raise HarnessError('BENCH_ENVIRONMENT_PROFILE_CHANGED')
        if receipt['profile_ref'] is not None and strict_yaml.loads(_read_ref(receipt['profile_ref'],self.assets_root))!=receipt['profile']:
            raise HarnessError('BENCH_ENVIRONMENT_PROFILE_CHANGED')
        for name,binding in receipt['root_bindings'].items():
            path=Path(receipt['homes'][name]);verify_parent_binding(path/'.binding',binding['parents'])
            if _identity(path)!=binding:
                raise HarnessError('BENCH_ENVIRONMENT_ROOT_CHANGED')
        for name,ref in receipt['profile'].items():
            if name.endswith('_ref'):
                _read_ref(ref,self.assets_root)
        manifest=json.loads(_read_ref(receipt['file_manifest_ref']))
        user=Path(receipt['homes']['user']);home=Path(receipt['homes']['codex_home'])
        for relative,row in manifest['files'].items():
            path=_safe_join(receipt['homes']['state_home'],relative[7:]) if relative.startswith('@state/') else _safe_join(user,relative)
            if _ref(path)['sha256']!=row['sha256']:
                raise HarnessError('BENCH_ENVIRONMENT_FILE_CHANGED')
        for relative,expected in manifest['closed_namespaces'].items():
            path=_safe_join(root,relative)
            if _inventory(path)!=expected:
                raise HarnessError('BENCH_ENVIRONMENT_NAMESPACE_CHANGED')
        if any(_present(_safe_join(home,name))!=present for name,present in manifest['definitions'].items()):
            raise HarnessError('BENCH_ENVIRONMENT_DEFINITION_CHANGED')
        plan=json.loads(_read_ref(receipt['launch_plan_ref']))
        if digest(plan['environment'])!=plan['environment_digest'] or plan['profile_digest']!=receipt['profile_digest']:
            raise HarnessError('BENCH_ENVIRONMENT_LAUNCH_CHANGED')
        if receipt['ancestry']['workspace'] is not None and self._ancestry(receipt['ancestry']['workspace'])!=receipt['ancestry']:
            raise HarnessError('BENCH_ENVIRONMENT_ANCESTRY_CHANGED')
        observation=json.loads(_read_ref(receipt['prepare_observation_ref']))
        if any(observation.get(key)!=receipt[key] for key in ('trial_id','manifest_digest','profile_digest')):
            raise HarnessError('BENCH_ENVIRONMENT_OBSERVATION_SUBJECT_CHANGED')
        return receipt

    def launch_environment(self,receipt):
        self.verify_arm(receipt)
        if not receipt['ancestry']['git_root_observed']:
            raise HarnessError('BENCH_ENVIRONMENT_PROJECT_BOUNDARY_UNAVAILABLE')
        return dict(json.loads(_read_ref(receipt['launch_plan_ref']))['environment'])

    def observe_launch(self,receipt,raw_transport,*,observation_ref):
        self.verify_arm(receipt)
        if not isinstance(observation_ref,dict):
            raise HarnessError('BENCH_ENVIRONMENT_OBSERVATION_ESCAPE')
        raw_path=observation_ref.get('path')
        if not isinstance(raw_path,str) or '\x00' in raw_path or len(raw_path)>4096:
            raise HarnessError('BENCH_ENVIRONMENT_OBSERVATION_ESCAPE')
        path=Path(raw_path)
        # Reject parent traversal before any observation read. abspath only
        # normalizes lexical spelling; read_regular_file still refuses links.
        parts=path.parts[1:] if path.anchor else path.parts
        if (any(part=='..' or part.endswith((' ','.')) or ':' in part for part in parts)
                or path.anchor and not path.is_absolute()):
            raise HarnessError('BENCH_ENVIRONMENT_OBSERVATION_ESCAPE')
        trial=Path(os.path.abspath(receipt['trial_root']))
        if not path.is_absolute():path=trial/path
        path=Path(os.path.abspath(path))
        if not path.is_relative_to(trial):
            raise HarnessError('BENCH_ENVIRONMENT_OBSERVATION_ESCAPE')
        ref={**observation_ref,'path':str(path)}
        if strict_yaml.loads(_read_ref(ref))!=raw_transport:
            raise HarnessError('BENCH_ENVIRONMENT_OBSERVATION_CHANGED')
        plan=json.loads(_read_ref(receipt['launch_plan_ref']))
        if raw_transport.get('environment_digest')!=plan['environment_digest']:
            raise HarnessError('BENCH_ENVIRONMENT_LAUNCH_MAPPING_MISMATCH')
        value={'schema_version':1,'record_type':'benchmark_arm_launch_observation',
            'trial_id':receipt['trial_id'],'manifest_digest':receipt['manifest_digest'],
            'profile_digest':receipt['profile_digest'],'environment_digest':plan['environment_digest'],
            'environment_binding':'observed_explicit_transport_argument_mapping' if raw_transport.get('process') is not None else 'declared_mapping_not_launched','process':raw_transport.get('process'),
            'status':raw_transport.get('status'),'outcome_known':raw_transport.get('outcome_known'),
            'transport_ref':ref,'arm_home_ref':_ref(Path(receipt['homes']['environment'])/'receipt.json'),
            'ancestry':receipt['ancestry'],'policy_blockers':receipt['blockers'],
            'effective_client_configuration_observed':False}
        strict_yaml.validate(value,ARM_LAUNCH_OBSERVATION)
        destination=Path(receipt['homes']['environment'])/('launch-observation-'+uuid.uuid4().hex+'.json')
        _save(destination,_json(value))
        return {'observation':value,'observation_ref':_ref(destination)}

    def measurements(self,receipt,*,run_root):
        self.verify_arm(receipt)
        from .benchmark_metrics import MetricRecorder, unavailable_measurements
        root=Path(run_root).absolute();path=Path(receipt['prepare_observation_ref']['path'])
        if not path.is_relative_to(root):
            raise HarnessError('BENCH_ENVIRONMENT_METRIC_ESCAPE')
        source={**receipt['prepare_observation_ref'],'path':path.relative_to(root).as_posix()}
        observed=json.loads(_read_ref(receipt['prepare_observation_ref']))
        result=unavailable_measurements()
        recorder=MetricRecorder(root,trial_id=receipt['trial_id'],manifest_digest=receipt['manifest_digest'],producer_id='benchmark-environment-builder')
        result['phase_seconds']['environment_prepare']=recorder.record('phase_seconds.environment_prepare',
            observed['elapsed_seconds'],coverage='observed',source_refs=[source],observation_scope='environment_prepare')
        return result
