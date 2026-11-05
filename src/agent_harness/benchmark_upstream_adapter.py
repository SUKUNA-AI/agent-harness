"""Concrete prepared upstream suite adapter for the existing benchmark runner.

Raw imports, prepared image/workspace pins, per-arm client environment receipts
and independent evaluator dispatch are consumed together. The observed registry
stays closed. Defaults refuse before any model/evaluator execution.
"""
from pathlib import Path
import json
import sys

from . import strict_yaml
from .benchmark_runner import CodexBenchmarkAdapter
from .benchmark_bundles import TaskBundleBoundary, _deadline, _hash
from .benchmark_protocol import validate_prepared_profile
from .benchmark_importers import import_upstream_source, materialize_upstream_plan, verify_upstream_materialization
from .benchmark_upstream_evaluator import evaluate_upstream
from .codex_execution import CodexAdapter
from .core import HarnessError,digest
from .execution_receipts import build_digest
from .file_io import read_regular_file


class UpstreamBenchmarkAdapter(CodexBenchmarkAdapter):
    def __init__(self,profile_ref,assets_root,*,boundary=None,arm_builder=None,evaluator_boundary=None):
        self.profile_ref=profile_ref;self.assets_root=Path(assets_root).absolute()
        self.boundary=boundary or TaskBundleBoundary();self.arm_builder=arm_builder;self.evaluator_boundary=evaluator_boundary

    def identity(self):
        value=self.boundary.identity()
        validated=validate_prepared_profile(self.profile_ref,self.assets_root)
        for key in('model','provider','client_version'):
            if value[key]!=validated['documents']['root_policy'][key]:raise HarnessError('BENCH_PROFILE_CLIENT_CONFLICT')
        return {**value,'adapter_id':'upstream-prepared-v1','build_digest':digest([build_digest(),value['build_digest'],validated['profile_digest']])}

    def control_argv(self,challenge,*,deadline=None):
        return self.boundary.control_argv(challenge,deadline=deadline)

    def _plan(self,row,manifest,assets_root,*,deadline=None):
        if Path(assets_root).absolute()!=self.assets_root:raise HarnessError('BENCH_UPSTREAM_ASSETS_ROOT_CONFLICT')
        validate_prepared_profile(self.profile_ref,self.assets_root,manifest)
        suite=next(item for item in manifest['suites'] if item['id']==row['suite'])
        source=next(item['source'] for item in suite['tasks'] if item['id']==row['task_id'])
        plan=import_upstream_source(source,self.assets_root,self.profile_ref,deadline=deadline)
        if plan['task_id']!=row['task_id'] or plan['suite']!=row['suite']:
            raise HarnessError('BENCH_PREPARED_PLAN_SUBJECT_CONFLICT')
        return plan

    def estimate_trial_tokens(self,row,manifest,assets_root):
        plan=self._plan(row,manifest,assets_root)
        return self.boundary.estimate_trial_tokens(row,plan)

    def prepare(self,row,manifest,assets_root,trial_root,*,deadline=None):
        _deadline(deadline);self.boundary.require_available(deadline=deadline)
        if self.arm_builder is None:raise HarnessError('BENCH_ARM_ENVIRONMENT_BUILDER_UNAVAILABLE')
        plan=self._plan(row,manifest,assets_root,deadline=deadline)
        value=materialize_upstream_plan(plan,assets_root=self.assets_root,trial_root=trial_root,
            trial_id=row['trial_id'],manifest_digest=digest(manifest),deadline=deadline)
        validated=validate_prepared_profile(self.profile_ref,self.assets_root,manifest)
        arm=self.arm_builder.prepare_arm(row,validated,trial_root,manifest_digest=digest(manifest),deadline=deadline,workspace=value['source_root'])
        self.arm_builder.verify_arm(arm)
        root=Path(trial_root)
        arm_path=root/'upstream-arm-receipt.yaml';strict_yaml.dump(arm_path,arm,exclusive=True)
        binding={'upstream_receipt_sha256':_hash(read_regular_file(root/'upstream-materialized.yaml')),
            'arm_receipt_sha256':_hash(read_regular_file(arm_path))}
        resources=validated['documents']['resources']
        task={'schema_version':1,'task':{'id':row['task_id'],'objective':plan['objective'],'allowed_write_scopes':plan['writable_paths']},
            'mode':'native','memory':True,'ledger':'inform','budgets':{'max_attempts':2,
            'max_wall_seconds':max(1,int(min(plan['parameters']['agent_timeout_seconds'],resources['max_trial_wall_seconds']))),
            'turn_timeout_seconds':max(1,int(min(plan['parameters']['agent_timeout_seconds'],resources['max_trial_wall_seconds'])))},
            # Submission is a structural gate only. Native upstream tests are
            # private and determine independent benchmark acceptance later.
            'acceptance':[{'id':'submitted-for-independent-upstream-evaluation','kind':'command','required':True,
                'argv':[sys.executable,'-c','pass',json.dumps(binding,sort_keys=True)],'timeout':5}]}
        contract=root/'task.yaml';strict_yaml.dump(contract,task,exclusive=True)
        _deadline(deadline)
        return {'schema_version':1,'trial_id':row['trial_id'],'cwd':value['source_root'],'contract_path':str(contract),
            'root_policy_digest':manifest['root_policy']['sha256'],'child_policy_digest':manifest['child_policy']['sha256'],
            'token_upper_bound':self.boundary.estimate_trial_tokens(row,plan)}

    def _verify(self,prepared,*,deadline=None):
        _deadline(deadline);root=Path(prepared['contract_path']).parent
        contract=strict_yaml.load(prepared['contract_path']);binding=json.loads(contract['acceptance'][0]['argv'][-1])
        value=verify_upstream_materialization(root/'upstream-materialized.yaml',receipt_digest=binding['upstream_receipt_sha256'],deadline=deadline)
        if value['trial_id']!=prepared['trial_id'] or value['source_root']!=prepared['cwd']:raise HarnessError('BENCH_UPSTREAM_PREPARED_SUBJECT_CONFLICT')
        if _hash(read_regular_file(root/'upstream-arm-receipt.yaml'))!=binding['arm_receipt_sha256']:raise HarnessError('BENCH_ARM_RECEIPT_CHANGED')
        if self.arm_builder is None:raise HarnessError('BENCH_ARM_ENVIRONMENT_BUILDER_UNAVAILABLE')
        arm=strict_yaml.load(root/'upstream-arm-receipt.yaml');self.arm_builder.verify_arm(arm)
        if arm['trial_id']!=prepared['trial_id'] or arm['profile_digest']!=value['plan']['profile_digest'] or arm['manifest_digest']!=value['manifest_digest']:
            raise HarnessError('BENCH_ARM_RECEIPT_SUBJECT_CONFLICT')
        fresh=import_upstream_source(value['plan']['source_ref'],self.assets_root,self.profile_ref,deadline=deadline)
        if fresh!=value['plan']:raise HarnessError('BENCH_PREPARED_PLAN_CHANGED')
        _deadline(deadline);return value,arm,binding

    def codex(self,prepared,*,deadline=None):
        value,arm,binding=self._verify(prepared,deadline=deadline)
        backend=self.boundary.codex(prepared,deadline=deadline)
        if not isinstance(backend,CodexAdapter):raise HarnessError('BENCH_CODEX_ADAPTER_REQUIRED')
        environment=self.arm_builder.launch_environment(arm)
        # An explicit transport API must bind capabilities, exec and resume.
        # Never alter process-global os.environ during concurrent trials.
        if not hasattr(backend,'with_environment'):raise HarnessError('BENCH_CLIENT_ENVIRONMENT_API_UNAVAILABLE')
        return backend.with_environment(environment)

    def independent_grade(self,row,prepared,grade_input,*,deadline=None):
        value,arm,binding=self._verify(prepared,deadline=deadline)
        if self.evaluator_boundary is None:raise HarnessError('BENCH_UPSTREAM_EVALUATOR_BOUNDARY_UNAVAILABLE')
        return evaluate_upstream(Path(prepared['contract_path']).parent/'upstream-materialized.yaml',grade_input,
            self.assets_root,receipt_digest=binding['upstream_receipt_sha256'],boundary=self.evaluator_boundary,deadline=deadline)

    def measurements(self,row,prepared,result,*,deadline=None):
        self._verify(prepared,deadline=deadline)
        try:
            return self.boundary.measurements(row,prepared,result,deadline=deadline)
        except HarnessError as error:
            if str(error).split(':',1)[0]!='BENCH_MEASUREMENTS_UNAVAILABLE':raise
            from .benchmark_metrics import unavailable_measurements
            return unavailable_measurements()


class PortfolioBenchmarkAdapter(CodexBenchmarkAdapter):
    """Route all five frozen suites without changing their trial/usage engine."""
    def __init__(self,upstream,local=None):
        from .benchmark_bundles import LocalTaskBundleAdapter
        self.upstream=upstream;self.local=local or LocalTaskBundleAdapter(upstream.boundary,fresh_git=True)

    def _adapter(self,row=None,prepared=None):
        return self.upstream if (row is not None and row['suite']in('swe-bench-verified','terminal-bench-2.1','aider-polyglot')) or (prepared is not None and (Path(prepared['contract_path']).parent/'upstream-materialized.yaml').exists()) else self.local

    def identity(self):
        upstream=self.upstream.identity();local=self.local.identity()
        if any(upstream[key]!=local[key] for key in('model','provider','client_version','measurement_kind','boundary_digest')):
            raise HarnessError('BENCH_PORTFOLIO_TRANSPORT_CONFLICT')
        return {**upstream,'adapter_id':'prepared-portfolio-v1','build_digest':digest([upstream['build_digest'],local['build_digest']])}

    def control_argv(self,challenge,*,deadline=None):
        return self.upstream.control_argv(challenge,deadline=deadline)

    def estimate_trial_tokens(self,row,manifest,assets_root):
        return self._adapter(row).estimate_trial_tokens(row,manifest,assets_root)

    def prepare(self,row,manifest,assets_root,trial_root,*,deadline=None):
        adapter=self._adapter(row)
        if adapter is self.upstream:return adapter.prepare(row,manifest,assets_root,trial_root,deadline=deadline)
        if self.upstream.arm_builder is None:raise HarnessError('BENCH_ARM_ENVIRONMENT_BUILDER_UNAVAILABLE')
        validated=validate_prepared_profile(self.upstream.profile_ref,assets_root,manifest)
        prepared=adapter.prepare(row,manifest,assets_root,trial_root,deadline=deadline)
        arm=self.upstream.arm_builder.prepare_arm(row,validated,trial_root,manifest_digest=digest(manifest),deadline=deadline,workspace=prepared['cwd'])
        self.upstream.arm_builder.verify_arm(arm)
        path=Path(trial_root)/'portfolio-arm-receipt.yaml';strict_yaml.dump(path,arm,exclusive=True)
        contract=strict_yaml.load(prepared['contract_path'])
        for check in contract['acceptance']:
            check['argv'].insert(-2,json.dumps({'arm_receipt_sha256':_hash(read_regular_file(path))},sort_keys=True))
        strict_yaml.dump(prepared['contract_path'],contract)
        return prepared

    def _local_arm(self,prepared):
        root=Path(prepared['contract_path']).parent
        contract=strict_yaml.load(prepared['contract_path']);binding=json.loads(contract['acceptance'][0]['argv'][-3])
        if _hash(read_regular_file(root/'portfolio-arm-receipt.yaml'))!=binding['arm_receipt_sha256']:raise HarnessError('BENCH_ARM_RECEIPT_CHANGED')
        arm=strict_yaml.load(root/'portfolio-arm-receipt.yaml');self.upstream.arm_builder.verify_arm(arm)
        if arm['trial_id']!=prepared['trial_id']:raise HarnessError('BENCH_ARM_RECEIPT_SUBJECT_CONFLICT')
        return arm

    def codex(self,prepared,*,deadline=None):
        adapter=self._adapter(prepared=prepared)
        if adapter is self.upstream:return adapter.codex(prepared,deadline=deadline)
        backend=adapter.codex(prepared,deadline=deadline)
        environment=self.upstream.arm_builder.launch_environment(self._local_arm(prepared))
        if not hasattr(backend,'with_environment'):raise HarnessError('BENCH_CLIENT_ENVIRONMENT_API_UNAVAILABLE')
        return backend.with_environment(environment)

    def independent_grade(self,row,prepared,grade_input,*,deadline=None):
        adapter=self._adapter(row)
        if adapter is self.upstream:return adapter.independent_grade(row,prepared,grade_input,deadline=deadline)
        self._local_arm(prepared)
        from .runner import _contained_capture
        from . import evaluation
        from datetime import datetime,timezone
        _deadline(deadline)
        argv=adapter.evaluator_argv(row,prepared,grade_input,deadline=deadline)
        _deadline(deadline)
        remaining=30 if deadline is None else min(30,(datetime.fromisoformat(deadline.replace('Z','+00:00'))-datetime.now(timezone.utc)).total_seconds())
        result=_contained_capture(argv,timeout=remaining)
        _deadline(deadline)
        if result.returncode:raise HarnessError('BENCH_UNKNOWN_GRADE_OUTCOME')
        return strict_yaml.loads(result.stdout,evaluation.GRADE)

    def measurements(self,row,prepared,result,*,deadline=None):
        self._local_arm(prepared) if self._adapter(row)is self.local else None
        return self._adapter(row).measurements(row,prepared,result,deadline=deadline)
