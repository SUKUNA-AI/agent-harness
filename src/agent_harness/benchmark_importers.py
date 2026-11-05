"""Concrete offline import/preparation of pinned upstream benchmark task formats.

No fetch, image build, model or unqualified evaluator execution occurs here.
Dataset bytes are caller-pinned; synthetic fixtures never become official runs.
"""
from pathlib import Path, PurePosixPath
import hashlib
import json
import re
import subprocess
import tomllib
import os

from . import evaluation, strict_yaml
from .benchmark_bundles import _deadline, _hash, _relative
from .benchmark_protocol import (REF, REVISION, SHORT, HASH, object_schema, checked_bytes,
    checked_document, validate_prepared_profile, EVALUATOR)
from .core import HarnessError, digest
from .file_io import read_regular_file, parent_binding, verify_parent_binding
from .supervisor_store import SupervisorStore, atomic_write, _ordinary
from .benchmark_git import FRESH_GIT

FILE = object_schema({'path':SHORT,'asset':REF,'mode':{'enum':['100644','100755']}},('path','asset'))
CHECKOUT = object_schema({'revision':REVISION,'tree':REVISION,'inventory_digest':HASH},('revision','tree','inventory_digest'))
RAW_SOURCE = object_schema({'schema_version':{'const':1},'record_type':{'const':'benchmark_upstream_source'},
    'suite':{'enum':list(evaluation.SUITES[:3])},'task_id':SHORT,'dataset_revision':REVISION,'evaluator_revision':REVISION,
    'metadata':REF,'files':{'type':'array','maxItems':10000,'items':FILE},
    'checkout':{'anyOf':[CHECKOUT,{'type':'null'}]},'language':{'enum':[None,'python','rust','go','javascript','cpp','java']}},
    ('schema_version','record_type','suite','task_id','dataset_revision','evaluator_revision','metadata','files','checkout','language'))
PARAMETERS = object_schema({'language':RAW_SOURCE['properties']['language'],'base_commit':{'type':['string','null']},
    'test_files':{'type':'array','items':SHORT},'solution_files':{'type':'array','items':SHORT},
    'agent_timeout_seconds':{'type':'number','exclusiveMinimum':0},'verifier_timeout_seconds':{'type':'number','exclusiveMinimum':0}},
    ('language','base_commit','test_files','solution_files','agent_timeout_seconds','verifier_timeout_seconds'))
PLAN = object_schema({'schema_version':{'const':1},'record_type':{'const':'benchmark_upstream_preparation'},
    'suite':RAW_SOURCE['properties']['suite'],'task_id':SHORT,'dataset_revision':REVISION,'evaluator_revision':REVISION,
    'source_ref':REF,'profile_ref':REF,'profile_digest':HASH,'cohort_selection':{'enum':['selected_full_pinned_index','custom_selection','synthetic_fixture']},
    'protocol':EVALUATOR['properties']['protocol'],'objective':{'type':'string','minLength':1,'maxLength':65536},
    'solve_files':{'type':'array','items':FILE},'protected_files':{'type':'array','items':FILE},
    'writable_paths':{'type':'array','minItems':1,'items':SHORT},'evaluator':EVALUATOR,'parameters':PARAMETERS},
    ('schema_version','record_type','suite','task_id','dataset_revision','evaluator_revision','source_ref','profile_ref','profile_digest','cohort_selection','protocol','objective','solve_files','protected_files','writable_paths','evaluator','parameters'))
BINDING = {'type':'array','items':object_schema({'path':SHORT,'device':{'type':'integer','minimum':0},'inode':{'type':'integer','minimum':0}},('path','device','inode'))}
INVENTORY = object_schema({'path':SHORT,'sha256':HASH},('path','sha256'))
MATERIALIZED = object_schema({'schema_version':{'const':1},'record_type':{'const':'benchmark_upstream_materialized'},
    'trial_id':HASH,'manifest_digest':HASH,'plan_digest':HASH,'plan':PLAN,'source_root':SHORT,'evaluator_root':SHORT,
    'source_binding':BINDING,'evaluator_binding':BINDING,'initial_files':{'type':'array','items':INVENTORY},
    'immutable_files':{'type':'array','items':INVENTORY},'protected_files':{'type':'array','items':INVENTORY},
    'fresh_git':FRESH_GIT},
    ('schema_version','record_type','trial_id','manifest_digest','plan_digest','plan','source_root','evaluator_root','source_binding','evaluator_binding','initial_files','immutable_files','protected_files','fresh_git'))


def _json(raw):
    json.loads(raw,parse_constant=lambda value:(_ for _ in ()).throw(ValueError(value)))
    return strict_yaml.loads(raw)


def _paths(files):
    result={}
    folded_paths=set()
    for item in files:
        name=_relative(item['path'])
        folded=name.casefold()
        if folded in folded_paths:
            raise HarnessError('BENCH_DUPLICATE_UPSTREAM_PATH')
        folded_paths.add(folded)
        result[name]=item['asset']
    names={name.casefold() for name in result}
    if any(str(parent).casefold() in names for name in result for parent in PurePosixPath(name).parents if str(parent)!='.'):
        raise HarnessError('BENCH_OVERLAPPING_UPSTREAM_PATH')
    return result


def capture_local_git_checkout(repository, revision, assets_root, *, prefix='checkout', deadline=None):
    """Read-only local Git capture, checking every worktree byte against HEAD."""
    from datetime import datetime,timezone
    def git(*args):
        _deadline(deadline)
        timeout=30 if deadline is None else min(30,(datetime.fromisoformat(deadline.replace('Z','+00:00'))-datetime.now(timezone.utc)).total_seconds())
        environment={key:value for key,value in os.environ.items() if not key.startswith('GIT_')}
        environment.update(GIT_CONFIG_NOSYSTEM='1',GIT_CONFIG_GLOBAL=os.devnull,GIT_CONFIG_SYSTEM=os.devnull,GIT_TERMINAL_PROMPT='0',GIT_NO_REPLACE_OBJECTS='1')
        result=subprocess.run(['git','--no-optional-locks','-c','core.fsmonitor=false',*args],cwd=repository,env=environment,capture_output=True,timeout=timeout)
        if result.returncode:
            raise HarnessError('BENCH_BASE_CHECKOUT_UNAVAILABLE')
        _deadline(deadline)
        return result.stdout
    strict_yaml.validate(revision,REVISION)
    if git('rev-parse','HEAD').decode().strip()!=revision:
        raise HarnessError('BENCH_BASE_COMMIT_MISMATCH')
    tree=git('rev-parse','HEAD^{tree}').decode().strip()
    entries=git('ls-tree','-rz','--full-tree',revision).split(b'\0')
    root=Path(assets_root).absolute();store=SupervisorStore(root)
    files=[]
    for entry in entries:
        if not entry:continue
        mode,kind,oid_name=entry.split(b' ',2);oid,name=oid_name.split(b'\t',1)
        name=_relative(name.decode('utf-8'))
        if kind!=b'blob' or mode not in (b'100644',b'100755'):
            raise HarnessError('BENCH_UNSUPPORTED_BASE_ENTRY')
        raw=read_regular_file(Path(repository)/name)
        algorithm=hashlib.sha1 if len(oid)==40 else hashlib.sha256
        if algorithm(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()!=oid.decode():
            raise HarnessError('BENCH_DIRTY_BASE_CHECKOUT')
        relative=_relative(prefix)+'/'+name
        atomic_write(store.path(relative),raw,exclusive=True)
        files.append({'path':name,'asset':{'path':relative,'sha256':_hash(raw)},'mode':mode.decode()})
        if len(files)>10000:raise HarnessError('BENCH_UPSTREAM_INVENTORY_TOO_LARGE')
    if git('rev-parse','HEAD').decode().strip()!=revision:
        raise HarnessError('BENCH_BASE_COMMIT_CHANGED')
    return files,{'revision':revision,'tree':tree,'inventory_digest':digest(files)}


def import_upstream_source(ref,assets_root,profile_ref,*,deadline=None):
    _deadline(deadline)
    source=checked_document(ref,assets_root,RAW_SOURCE)
    if not re.fullmatch(r'[A-Za-z0-9_.-]+',source['task_id']):
        raise HarnessError('BENCH_UNSAFE_UPSTREAM_TASK_ID')
    validated=validate_prepared_profile(profile_ref,assets_root)
    suite=validated['suites'][source['suite']];cohort=suite['cohort'];evaluator=suite['evaluator']
    if ({'id':source['task_id'],'source':ref} not in cohort['tasks'] or source['dataset_revision']!=cohort['dataset_revision'] or source['evaluator_revision']!=evaluator['revision']):
        raise HarnessError('BENCH_UPSTREAM_PROVENANCE_CONFLICT')
    files=_paths(source['files'])
    modes={item['path']:item.get('mode','100644') for item in source['files']}
    for asset in files.values():checked_bytes(asset,assets_root)
    metadata_raw=checked_bytes(source['metadata'],assets_root)
    protected={'raw-metadata':source['metadata']}
    protected_modes={}
    parameters={'language':source['language'],'base_commit':None,'test_files':[],'solution_files':[],
        'agent_timeout_seconds':validated['documents']['resources']['max_trial_wall_seconds'],
        'verifier_timeout_seconds':validated['documents']['resources']['max_trial_wall_seconds']}
    if source['suite']=='swe-bench-verified':
        metadata=_json(metadata_raw)
        for key in ('instance_id','repo','base_commit','problem_statement','patch','test_patch','version'):
            if type(metadata.get(key)) is not str:raise HarnessError('BENCH_INVALID_SWE_RECORD')
        if metadata['instance_id']!=source['task_id'] or source['checkout'] is None or metadata['base_commit']!=source['checkout']['revision'] or source['checkout']['inventory_digest']!=digest(source['files']):
            raise HarnessError('BENCH_SWE_BASE_BINDING_CONFLICT')
        for key in ('FAIL_TO_PASS','PASS_TO_PASS'):
            values=_json(metadata[key].encode()) if isinstance(metadata.get(key),str) else metadata.get(key)
            if not isinstance(values,list) or not all(type(value)is str for value in values):raise HarnessError('BENCH_INVALID_SWE_TEST_LIST')
        solve=files;writable=['**'];objective=metadata['problem_statement']
        parameters['base_commit']=metadata['base_commit']
        # Base inventory stays private for producing the official prediction
        # patch; gold/test_patch are exclusively in the evaluator metadata.
        protected.update({'baseline/'+name:asset for name,asset in files.items()})
        protected_modes.update({'baseline/'+name:modes[name] for name in files})
    elif source['suite']=='terminal-bench-2.1':
        configuration=tomllib.loads(metadata_raw.decode('utf-8'))
        if configuration.get('schema_version') not in ('1.0','1.1','1.2','1.3','1.4') or configuration.get('steps'):
            raise HarnessError('BENCH_UNSUPPORTED_HARBOR_TASK_VERSION_OR_STEPS')
        task_name=configuration.get('task',{}).get('name')
        if task_name is not None and (type(task_name)is not str or task_name.rsplit('/',1)[-1]!=source['task_id']):
            raise HarnessError('BENCH_HARBOR_TASK_SUBJECT_CONFLICT')
        if 'instruction.md' not in files or 'tests/test.sh' not in files:
            raise HarnessError('BENCH_INCOMPLETE_HARBOR_TASK')
        environment=configuration.get('environment',{})
        if environment.get('mcp_servers'):
            raise HarnessError('BENCH_EXTERNAL_SERVICE_REQUIRES_FROZEN_FIXTURE')
        resources=validated['documents']['resources']
        for key in ('cpus','memory_mb','storage_mb'):
            if key in environment and (type(environment[key]) not in (int,float) or environment[key]>resources[key]):
                raise HarnessError('BENCH_TASK_RESOURCE_REQUIREMENT_EXCEEDS_PROFILE')
        if environment.get('gpus',0):raise HarnessError('BENCH_GPU_PROFILE_UNAVAILABLE')
        for section,target in [('agent','agent_timeout_seconds'),('verifier','verifier_timeout_seconds')]:
            value=configuration.get(section,{}).get('timeout_sec',parameters[target])
            if type(value)not in(int,float) or value<=0:raise HarnessError('BENCH_INVALID_HARBOR_TIMEOUT')
            parameters[target]=min(value,resources['max_trial_wall_seconds'])
        prepared=next(item for item in validated['images'] if item['image']['suite']==source['suite'])
        declared=environment.get('docker_image')
        if declared is not None and declared!=prepared['image']['image']:
            raise HarnessError('BENCH_HARBOR_IMAGE_CONFLICT')
        workspace=_paths(prepared['workspace']['files'])
        modes.update({item['path']:item.get('mode','100644') for item in prepared['workspace']['files']})
        if set(workspace)&{'tests','solution'} or any(name.startswith(('tests/','solution/')) for name in workspace):
            raise HarnessError('BENCH_GOLD_INSIDE_PREPARED_WORKSPACE')
        solve={**workspace,'instruction.md':files['instruction.md']};writable=['**']
        objective=checked_bytes(files['instruction.md'],assets_root).decode('utf-8')
        protected.update({'task/'+name:asset for name,asset in files.items() if name!='instruction.md'})
        protected_modes.update({'task/'+name:modes[name] for name in files if name!='instruction.md'})
    else:
        configuration=_json(metadata_raw)
        declared=configuration.get('files',{})
        if source['language'] is None or not isinstance(declared,dict):raise HarnessError('BENCH_INVALID_AIDER_CONFIG')
        for key in ('solution','test','example'):
            values=declared.get(key,[])
            if not isinstance(values,list) or not all(type(value)is str for value in values):raise HarnessError('BENCH_INVALID_AIDER_FILE_SET')
            for name in values:
                _relative(name)
                if name not in files:raise HarnessError('BENCH_AIDER_DECLARED_FILE_MISSING')
        solutions=declared.get('solution',[]);tests=declared.get('test',[])
        if not solutions or not tests or set(solutions)&set(tests+declared.get('example',[])):
            raise HarnessError('BENCH_OVERLAPPING_AIDER_FILE_SETS')
        extensions={'python':'.py','rust':'.rs','go':'.go','javascript':'.js','cpp':'.cpp','java':'.java'}
        recognized={PurePosixPath(name).suffix for name in tests}&set(extensions.values())
        if recognized!={extensions[source['language']]}:raise HarnessError('BENCH_AIDER_LANGUAGE_TEST_CONFLICT')
        if '.docs/instructions.md' not in files:raise HarnessError('BENCH_AIDER_INSTRUCTIONS_MISSING')
        objective='\n'.join(checked_bytes(files[name],assets_root).decode('utf-8') for name in ('.docs/introduction.md','.docs/instructions.md','.docs/instructions.append.md') if name in files)
        hidden=set(tests+declared.get('example',[]))
        solve={name:asset for name,asset in files.items() if name not in hidden and not name.startswith(('.meta/','.docs/'))}
        protected.update({'tests/'+name:files[name] for name in tests})
        protected.update({'examples/'+name:files[name] for name in declared.get('example',[])})
        protected_modes.update({'tests/'+name:modes[name] for name in tests})
        protected_modes.update({'examples/'+name:modes[name] for name in declared.get('example',[])})
        writable=solutions
        parameters.update(test_files=tests,solution_files=solutions)
    plan={'schema_version':1,'record_type':'benchmark_upstream_preparation','suite':source['suite'],'task_id':source['task_id'],
        'dataset_revision':source['dataset_revision'],'evaluator_revision':source['evaluator_revision'],'source_ref':ref,'profile_ref':profile_ref,
        'profile_digest':validated['profile_digest'],'cohort_selection':cohort['selection'],'protocol':evaluator['protocol'],'objective':objective,
        'solve_files':[{'path':name,'asset':asset,'mode':modes.get(name,'100644')} for name,asset in sorted(solve.items())],
        'protected_files':[{'path':name,'asset':asset,'mode':protected_modes.get(name,'100644')} for name,asset in sorted(protected.items())],
        'writable_paths':writable,'evaluator':evaluator,'parameters':parameters}
    strict_yaml.validate(plan,PLAN);_deadline(deadline)
    return plan


def materialize_upstream_plan(plan,*,assets_root,trial_root,trial_id,manifest_digest,deadline=None):
    strict_yaml.validate(plan,PLAN)
    fresh=import_upstream_source(plan['source_ref'],assets_root,plan['profile_ref'],deadline=deadline)
    if fresh!=plan:raise HarnessError('BENCH_PREPARED_PLAN_CHANGED')
    store=SupervisorStore(trial_root)
    with store.lease('materialize pinned upstream task'):
        if any(path.name!='locks' for path in store.root.iterdir()):raise HarnessError('BENCH_MATERIALIZATION_EXISTS_OR_UNKNOWN')
        source=store.root/'source';evaluator=store.root/'evaluator'
        inventories={}
        for label,base,files in [('initial',source,plan['solve_files']),('protected',evaluator,plan['protected_files'])]:
            inventory=[]
            for item in files:
                _deadline(deadline);raw=checked_bytes(item['asset'],assets_root)
                atomic_write(base/_relative(item['path']),raw,exclusive=True)
                os.chmod(base/item['path'],0o700 if item.get('mode')=='100755' else 0o600)
                inventory.append({'path':item['path'],'sha256':_hash(raw)})
            inventories[label]=inventory
        # A prepared empty workspace still has an ordinary pinned source dir.
        source.mkdir(exist_ok=True);evaluator.mkdir(exist_ok=True)
        from .benchmark_git import initialize_fresh_git
        raw_source=checked_document(plan['source_ref'],assets_root,RAW_SOURCE)
        fresh_git=initialize_fresh_git(source,expected_tree=raw_source['checkout']['tree'] if plan['suite']=='swe-bench-verified' else None,
            executable_paths=[item['path'] for item in plan['solve_files'] if item.get('mode')=='100755'],deadline=deadline)
        value={'schema_version':1,'record_type':'benchmark_upstream_materialized','trial_id':trial_id,'manifest_digest':manifest_digest,
            'plan_digest':digest(plan),'plan':plan,'source_root':str(source),'evaluator_root':str(evaluator),
            'source_binding':parent_binding(source/'binding'),'evaluator_binding':parent_binding(evaluator/'binding'),
            'initial_files':inventories['initial'],'immutable_files':[] if '**' in plan['writable_paths'] else [item for item in inventories['initial'] if item['path']not in plan['writable_paths']],
            'protected_files':inventories['protected'],'fresh_git':fresh_git}
        store.write('upstream-materialized.yaml',value,MATERIALIZED,exclusive=True)
    return value


def verify_upstream_materialization(path,*,receipt_digest=None,initial=False,deadline=None):
    if receipt_digest is not None and _hash(read_regular_file(path))!=receipt_digest:raise HarnessError('BENCH_UPSTREAM_RECEIPT_CHANGED')
    value=strict_yaml.load(path,MATERIALIZED);root=Path(path).absolute().parent
    if value['plan_digest']!=digest(value['plan']) or value['source_root']!=str(root/'source') or value['evaluator_root']!=str(root/'evaluator'):
        raise HarnessError('BENCH_UPSTREAM_PLAN_ROOT_CONFLICT')
    for label in ('source','evaluator'):verify_parent_binding(root/label/'binding',value[label+'_binding'])
    from .benchmark_git import verify_fresh_git
    verify_fresh_git(value['fresh_git'],initial=initial,deadline=deadline)
    for base,inventory in [(root/'source',value['initial_files'] if initial else value['immutable_files']),(root/'evaluator',value['protected_files'])]:
        for item in inventory:
            _deadline(deadline)
            if _hash(read_regular_file(base/_relative(item['path'])))!=item['sha256']:raise HarnessError('BENCH_UPSTREAM_INPUT_CHANGED')
    for index,item in enumerate((root/'source').rglob('*')):
        if item.relative_to(root/'source').parts[0]=='.git':continue
        if index>=10000:raise HarnessError('BENCH_UPSTREAM_INVENTORY_TOO_LARGE')
        _deadline(deadline);_ordinary(item,directory=item.is_dir())
        if item.is_file() and '**' not in value['plan']['writable_paths'] and item.relative_to(root/'source').as_posix()not in {row['path'] for row in value['initial_files']}|set(value['plan']['writable_paths']):
            raise HarnessError('BENCH_UPSTREAM_UNDECLARED_OUTPUT')
    _deadline(deadline);return value
