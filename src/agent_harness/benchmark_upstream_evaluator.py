"""Versioned upstream evaluator dispatch, routed by a separately qualified boundary.

This controller never runs model-generated code on an unqualified host. It
constructs exact suite commands and interprets their suite-native result files.
A boundary supplies the actual execution; no success boolean from task output
can replace those results. The default boundary is unavailable.
"""
from pathlib import Path
import difflib
import json
import math
import os
import sys
import hashlib

from . import strict_yaml
from .benchmark_bundles import GRADE_INPUT, _deadline, _hash, _relative
from .benchmark_importers import verify_upstream_materialization, _json
from .benchmark_protocol import checked_bytes
from .core import HarnessError, digest
from .file_io import read_regular_file
from .supervisor_store import atomic_write, _ordinary


class UpstreamEvaluatorBoundary:
    def run(self, command, *, cwd, deadline, timeout, mounts, resources, image):
        raise HarnessError('ENVIRONMENT_ISOLATION_UNAVAILABLE: upstream evaluator boundary unavailable')


def _patch(value, *, deadline=None):
    root=Path(value['source_root']);gold=Path(value['evaluator_root'])/'baseline'
    baseline={item['path'] for item in value['initial_files']}
    current=set()
    for index,path in enumerate(root.rglob('*')):
        if path.relative_to(root).parts[0]=='.git':continue
        if index>=10000:raise HarnessError('BENCH_UPSTREAM_INVENTORY_TOO_LARGE')
        _deadline(deadline);_ordinary(path,directory=path.is_dir())
        if path.is_file():current.add(_relative(path.relative_to(root).as_posix()))
    initial_modes={item['path']:item.get('mode','100644') for item in value['plan']['solve_files']}
    index_modes={}
    if os.name=='nt':
        from .benchmark_git import _git
        for record in _git(root,['ls-files','--stage','-z'],deadline=deadline).split('\0'):
            if record:
                fields,name=record.split('\t',1);index_modes[name]=fields.split()[0]
    algorithm=hashlib.sha256 if len(value['fresh_git']['initial_head'])==64 else hashlib.sha1
    def blob(raw):return algorithm(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
    chunks=[]
    for name in sorted(baseline|current):
        _deadline(deadline)
        before=read_regular_file(gold/name) if name in baseline else b''
        after=read_regular_file(root/name) if name in current else b''
        before_exists=name in baseline;after_exists=name in current
        old_mode=initial_modes.get(name,'100644')
        new_mode=(index_modes.get(name,old_mode if before_exists else '100644') if os.name=='nt' else
            '100755' if after_exists and _ordinary(root/name).st_mode&0o111 else '100644')
        if before_exists==after_exists and before==after and old_mode==new_mode:continue
        if b'\0' in before or b'\0' in after:
            raise HarnessError('BENCH_BINARY_PREDICTION_PATCH_UNSUPPORTED')
        try:old,new=before.decode('utf-8'),after.decode('utf-8')
        except UnicodeDecodeError as error:raise HarnessError('BENCH_BINARY_PREDICTION_PATCH_UNSUPPORTED')from error
        chunks.append('diff --git a/'+name+' b/'+name+'\n')
        if not before_exists:chunks.append('new file mode '+new_mode+'\n')
        elif not after_exists:chunks.append('deleted file mode '+old_mode+'\n')
        elif old_mode!=new_mode:chunks.extend(['old mode '+old_mode+'\n','new mode '+new_mode+'\n'])
        if before!=after or before_exists!=after_exists:
            chunks.append('index '+(blob(before) if before_exists else '0'*len(blob(b'')))+'..'+(blob(after) if after_exists else '0'*len(blob(b'')))+
                (' '+old_mode if before_exists and after_exists and old_mode==new_mode else '')+'\n')
        # Git's text records end only at LF. Unicode separators and CR are
        # ordinary bytes; str.splitlines would silently change patch hunks.
        def lf_lines(text):
            parts=text.split('\n')
            return [part+'\n' for part in parts[:-1]]+([parts[-1]] if parts[-1] else [])
        for line in difflib.unified_diff(lf_lines(old),lf_lines(new),
            fromfile='a/'+name if name in baseline else '/dev/null',tofile='b/'+name if name in current else '/dev/null'):
            chunks.append(line if line.endswith('\n') else line+'\n\\ No newline at end of file\n')
    return ''.join(chunks)


def prepare_evaluation(receipt_path, grade_input, assets_root, *, receipt_digest, deadline=None):
    """Concrete immutable evaluator inputs + native suite command; no execution."""
    _deadline(deadline)
    value=verify_upstream_materialization(receipt_path,receipt_digest=receipt_digest,deadline=deadline)
    inputs=strict_yaml.load(grade_input,GRADE_INPUT)
    if inputs['trial_id']!=value['trial_id'] or inputs['manifest_digest']!=value['manifest_digest'] or inputs['cwd']!=value['source_root']:
        raise HarnessError('BENCH_EVALUATOR_SUBJECT_MISMATCH')
    if inputs['solve_outcome']=='unknown':raise HarnessError('BENCH_UNKNOWN_SOLVE_OUTCOME')
    if inputs['solve_outcome']not in('completed','failed'):
        return {'terminal_outcome':inputs['solve_outcome'],'value':value,'inputs':inputs}
    plan=value['plan'];controller=Path(receipt_path).parent
    from .benchmark_protocol import validate_prepared_profile
    validated=validate_prepared_profile(plan['profile_ref'],assets_root)
    runtime=next(item['toolchain']['python'] for item in validated['images'] if item['image']['suite']==plan['suite'])
    output=controller/'upstream-grade'
    # Preparation must be reconciled after interruption. Never rerun an
    # upstream evaluator under a reused cached result/run identifier.
    if output.exists():raise HarnessError('BENCH_UPSTREAM_GRADE_EXISTS_OR_UNKNOWN')
    output.mkdir(mode=0o700)
    protected=Path(value['evaluator_root']);source=Path(value['source_root'])
    if plan['protocol']=='swebench-jsonl-v1':
        raw_record=_json(read_regular_file(protected/'raw-metadata'))
        dataset=output/'dataset.json';prediction=output/'predictions.jsonl'
        atomic_write(dataset,json.dumps([raw_record],allow_nan=False).encode(),exclusive=True)
        prediction_record={'instance_id':plan['task_id'],'model_name_or_path':'codex-'+value['trial_id'],
            'model_patch':_patch(value,deadline=deadline)}
        atomic_write(prediction,(json.dumps(prediction_record,allow_nan=False)+'\n').encode(),exclusive=True)
        command=[runtime,'-m','swebench.harness.run_evaluation','--dataset_name',str(dataset),
            '--predictions_path',str(prediction),'--instance_ids',plan['task_id'],'--run_id',value['trial_id'],
            '--max_workers','1','--cache_level','none']
        result_path=output/'logs'/'evaluation'/value['trial_id']/prediction_record['model_name_or_path']/plan['task_id']/'report.json'
        cwd=output
    elif plan['protocol']=='harbor-task-v1':
        # Harbor verifier paths are fixed inside the evaluator boundary. The
        # solve workspace and /tests are separate mounts, never task-selected.
        command=['/bin/bash','/tests/test.sh'];result_path=output/'reward.txt';cwd=source
    else:
        language=plan['parameters']['language']
        commands={'python':[runtime,'-m','pytest'], 'rust':['cargo','test','--offline','--','--include-ignored'],
            'go':['go','test','./...'],'java':['bash','./gradlew','test','--offline']}
        if language in ('javascript','cpp'):
            name='npm-test.sh' if language=='javascript' else 'cpp-test.sh'
            helper=next((item for item in plan['evaluator']['helpers'] if item['name']==name),None)
            if helper is None:raise HarnessError('BENCH_AIDER_TEST_HELPER_UNPINNED')
            helper_path=output/name;atomic_write(helper_path,checked_bytes(helper['artifact'],assets_root),exclusive=True)
            command=['bash',str(helper_path)]
        else:command=commands[language]
        # Independent evaluator boundary overlays original test/example files
        # onto a fresh copy of the solved workspace, not the live solve tree.
        result_path=output/'exit-code.json';cwd=output/'workspace'
        cwd.mkdir()
        # Only copied solution/build inputs enter the fresh grading workspace;
        # restore immutable original tests after the model has stopped.
        for path in source.rglob('*'):
            if path.relative_to(source).parts[0]=='.git':continue
            _deadline(deadline);_ordinary(path,directory=path.is_dir())
            if path.is_file():
                destination=cwd/path.relative_to(source)
                atomic_write(destination,read_regular_file(path),exclusive=True)
                declared=next((item.get('mode','100644') for item in plan['solve_files'] if item['path']==path.relative_to(source).as_posix()),'100644')
                os.chmod(destination,0o700 if (declared=='100755' if os.name=='nt' else _ordinary(path).st_mode&0o111) else 0o600)
        for label in ('tests','examples'):
            for path in (protected/label).rglob('*') if (protected/label).exists() else ():
                _deadline(deadline);_ordinary(path,directory=path.is_dir())
                if path.is_file():
                    raw=read_regular_file(path)
                    if language=='java' and path.suffix=='.java':
                        import re
                        raw=re.sub(r'@Disabled\([^)]*\)\s*\n','',raw.decode('utf-8')).encode()
                    destination=cwd/path.relative_to(protected/label);atomic_write(destination,raw)
                    declared=next(item.get('mode','100644') for item in plan['protected_files'] if item['path']==path.relative_to(protected).as_posix())
                    os.chmod(destination,0o700 if declared=='100755' else 0o600)
    _deadline(deadline)
    return {'value':value,'inputs':inputs,'command':command,'cwd':str(cwd),'result_path':str(result_path),
        'output_root':str(output),'protocol':plan['protocol'],
        'mounts':{'solve_result':str(source),'protected':str(protected),'output':str(output),
            'terminal_tests_target':'/tests' if plan['protocol']=='harbor-task-v1' else None,
            'terminal_tests_source':str(protected/'task/tests') if plan['protocol']=='harbor-task-v1' else None,
            'terminal_reward_target':'/logs/verifier' if plan['protocol']=='harbor-task-v1' else None},
        'timeout':plan['parameters']['verifier_timeout_seconds'],
        'dispatch_digest':digest([value['plan_digest'],inputs['solve_digest'],command,str(result_path)])}


def parse_upstream_result(request, execution):
    """Interpret real suite-native outputs, not stdout claims of acceptance."""
    protocol=request['protocol'];path=Path(request['result_path'])
    if protocol=='swebench-jsonl-v1':
        value=_json(read_regular_file(path))
        task_id=request['value']['plan']['task_id']
        if not isinstance(value,dict) or set(value)!={task_id} or not isinstance(value[task_id],dict) or type(value[task_id].get('resolved'))is not bool:
            raise HarnessError('BENCH_INVALID_SWE_EVALUATION_REPORT')
        # An infrastructure error never becomes a normal model failure/success.
        if execution['returncode']!=0:raise HarnessError('BENCH_UPSTREAM_EVALUATOR_FAILED')
        return 'accepted' if value[task_id]['resolved'] else 'failed'
    if protocol=='harbor-task-v1':
        json_path=path.with_suffix('.json')
        if json_path.exists():
            value=_json(read_regular_file(json_path))
            # Labeled/multi-dimensional rewards require an explicitly pinned
            # aggregation profile; never choose one after viewing outcomes.
            if not isinstance(value,dict) or set(value)!={'reward'}:raise HarnessError('BENCH_HARBOR_REWARD_AGGREGATION_UNSUPPORTED')
            reward=value['reward']
        else:
            reward=_json(read_regular_file(path))
        if type(reward)not in(int,float) or not math.isfinite(reward) or reward not in(0,1):raise HarnessError('BENCH_INVALID_HARBOR_REWARD')
        return 'accepted' if reward==1 and execution['returncode']==0 else 'failed'
    if type(execution.get('returncode'))is not int:raise HarnessError('BENCH_INVALID_EVALUATOR_EXIT')
    return 'accepted' if execution['returncode']==0 else 'failed'


def evaluate_upstream(receipt_path, grade_input, assets_root, *, receipt_digest, boundary=None, deadline=None):
    """Run only via an explicit evaluator boundary; default refuses before IO."""
    if boundary is None:raise HarnessError('ENVIRONMENT_ISOLATION_UNAVAILABLE')
    request=prepare_evaluation(receipt_path,grade_input,assets_root,receipt_digest=receipt_digest,deadline=deadline)
    if 'terminal_outcome'in request:outcome=request['terminal_outcome']
    else:
        from .benchmark_protocol import validate_prepared_profile
        validated=validate_prepared_profile(request['value']['plan']['profile_ref'],assets_root)
        image=next(item['image'] for item in validated['images'] if item['image']['suite']==request['value']['plan']['suite'])
        _deadline(deadline)
        timeout=request['timeout']
        if deadline is not None:
            from datetime import datetime,timezone
            timeout=min(timeout,(datetime.fromisoformat(deadline.replace('Z','+00:00'))-datetime.now(timezone.utc)).total_seconds())
        execution=boundary.run(request['command'],cwd=request['cwd'],deadline=deadline,
            timeout=timeout,mounts=request['mounts'],resources=validated['documents']['resources'],image=image)
        _deadline(deadline)
        # Revalidate protected metadata and exact receipt after evaluator IO.
        verify_upstream_materialization(receipt_path,receipt_digest=receipt_digest,deadline=deadline)
        outcome=parse_upstream_result(request,execution)
    inputs=request['inputs']
    grade={'schema_version':1,'record_type':'independent_grade','manifest_digest':inputs['manifest_digest'],
        'trial_id':inputs['trial_id'],'evaluator_digest':inputs['evaluator_digest'],'outcome':outcome}
    from . import evaluation
    strict_yaml.validate(grade,evaluation.GRADE);_deadline(deadline)
    return grade
