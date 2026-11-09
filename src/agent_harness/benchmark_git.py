"""Fresh task Git roots with controller-owned physical/base-tree bindings.

Project AGENTS files remain intact. This prepares the documented Git boundary;
it does not qualify a real client's ambient instruction discovery semantics.
"""
from pathlib import Path
from datetime import datetime,timezone
import os
import subprocess

from .core import HarnessError
from .execution_ledger import SHORT,object_schema
from .file_io import parent_binding,verify_parent_binding
from .supervisor_store import _ordinary

_REVISION={'type':'string','pattern':'^[a-f0-9]{40}([a-f0-9]{24})?$'}
_BINDING={'type':'array','items':object_schema({'path':SHORT,'device':{'type':'integer','minimum':0},'inode':{'type':'integer','minimum':0}},('path','device','inode'))}
FRESH_GIT=object_schema({'schema_version':{'const':1},'record_type':{'const':'benchmark_fresh_git'},'root':SHORT,
    'initial_head':_REVISION,'initial_tree':_REVISION,'git_binding':_BINDING,'root_binding':_BINDING},
    ('schema_version','record_type','root','initial_head','initial_tree','git_binding','root_binding'))

def _deadline(deadline):
    if deadline is not None and datetime.fromisoformat(deadline.replace('Z','+00:00'))<=datetime.now(timezone.utc):
        raise HarnessError('BENCH_DEADLINE_EXHAUSTED')


def _git(root,args,*,deadline=None):
    _deadline(deadline)
    environment={key:value for key,value in os.environ.items() if not key.startswith('GIT_')}
    environment.update(GIT_CONFIG_NOSYSTEM='1',GIT_CONFIG_GLOBAL=os.devnull,GIT_CONFIG_SYSTEM=os.devnull,
        GIT_TERMINAL_PROMPT='0',GIT_AUTHOR_NAME='Benchmark preparation',GIT_AUTHOR_EMAIL='benchmark@localhost',
        GIT_COMMITTER_NAME='Benchmark preparation',GIT_COMMITTER_EMAIL='benchmark@localhost',GIT_NO_REPLACE_OBJECTS='1')
    # Git environment variables can otherwise redirect discovery/index/config.
    for key in list(environment):
        if key.startswith(('GIT_CONFIG_KEY_','GIT_CONFIG_VALUE_')) or key in {'GIT_CONFIG_COUNT','GIT_DIR','GIT_COMMON_DIR','GIT_WORK_TREE','GIT_INDEX_FILE','GIT_OBJECT_DIRECTORY','GIT_ALTERNATE_OBJECT_DIRECTORIES','GIT_CONFIG_PARAMETERS'}:
            environment.pop(key,None)
    timeout=30 if deadline is None else min(30,(datetime.fromisoformat(deadline.replace('Z','+00:00'))-datetime.now(timezone.utc)).total_seconds())
    result=subprocess.run(['git','--no-optional-locks','-c','core.hooksPath='+str(Path(root).parent/'git-empty-hooks'),
        '-c','core.autocrlf=false','-c','core.safecrlf=false','-c','core.fsmonitor=false','-c','protocol.allow=never',*args],cwd=root,env=environment,capture_output=True,timeout=timeout)
    _deadline(deadline)
    if result.returncode:raise HarnessError('BENCH_FRESH_GIT_PREPARATION_FAILED')
    return result.stdout.decode('utf-8').strip()


def initialize_fresh_git(root,*,expected_tree=None,executable_paths=(),deadline=None):
    root=Path(root).absolute();_ordinary(root,directory=True)
    if (root/'.git').exists() or (root/'.git').is_symlink():raise HarnessError('BENCH_GIT_ROOT_ALREADY_EXISTS')
    template=root.parent/'git-empty-template';hooks=root.parent/'git-empty-hooks'
    template.mkdir(mode=0o700,exist_ok=True);hooks.mkdir(mode=0o700,exist_ok=True)
    if any(template.iterdir()) or any(hooks.iterdir()):raise HarnessError('BENCH_GIT_TEMPLATE_OR_HOOK_NOT_EMPTY')
    algorithm='sha256' if expected_tree is not None and len(expected_tree)==64 else 'sha1'
    _git(root,['init','--template='+str(template),'--object-format='+algorithm],deadline=deadline)
    _git(root,['add','--all','--force'],deadline=deadline)
    for start in range(0,len(executable_paths),50):
        _git(root,['update-index','--chmod=+x','--',*executable_paths[start:start+50]],deadline=deadline)
    tree=_git(root,['write-tree'],deadline=deadline)
    if expected_tree is not None and tree!=expected_tree:raise HarnessError('BENCH_FRESH_GIT_BASE_TREE_CONFLICT')
    _git(root,['commit','--allow-empty','-m','Pinned benchmark initial workspace'],deadline=deadline)
    head=_git(root,['rev-parse','HEAD'],deadline=deadline)
    return {'schema_version':1,'record_type':'benchmark_fresh_git','root':str(root),'initial_head':head,'initial_tree':tree,
        'git_binding':parent_binding(root/'.git'/'binding'),'root_binding':parent_binding(root/'binding')}


def verify_fresh_git(value,*,initial=False,deadline=None):
    root=Path(value['root']);_ordinary(root,directory=True);_ordinary(root/'.git',directory=True)
    verify_parent_binding(root/'binding',value['root_binding'])
    verify_parent_binding(root/'.git'/'binding',value['git_binding'])
    # A literal root check prevents an enclosing user repository from being
    # mistaken for the trial root. No project AGENTS content is disabled.
    actual=Path(_git(root,['rev-parse','--show-toplevel'],deadline=deadline))
    if actual.resolve()!=root.resolve():raise HarnessError('BENCH_AMBIENT_PROJECT_POLICY_ISOLATION_UNAVAILABLE')
    if _git(root,['rev-parse',value['initial_head']+'^{tree}'],deadline=deadline)!=value['initial_tree']:
        raise HarnessError('BENCH_FRESH_GIT_BASE_TREE_CONFLICT')
    if initial and (_git(root,['rev-parse','HEAD'],deadline=deadline)!=value['initial_head'] or _git(root,['write-tree'],deadline=deadline)!=value['initial_tree']):
        raise HarnessError('BENCH_FRESH_GIT_INITIAL_BINDING_CHANGED')
    _deadline(deadline);return value
