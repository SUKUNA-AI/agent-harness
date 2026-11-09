"""Offline preparation over benchmark assets; never a runner or qualification.

Supplied task banks and future reports are local, unqualified declarations.
Frozen bytes establish reproducibility, not corpus authenticity or a model win.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from copy import deepcopy

from . import evaluation, strict_yaml
from .benchmark_importers import capture_local_git_checkout
from .benchmark_metrics import portable_relative_path
from .benchmark_protocol import checked_bytes as _benchmark_bytes
from .core import HarnessError, canonical, digest
from .execution_ledger import HASH, SHORT, object_schema, safe_private
from .file_io import read_regular_file
from .supervisor_store import atomic_write, _mkdir, _ordinary

ARMS = ('A', 'B-mechanical', 'B-human', 'C', 'D')
MECHANISMS = dict(zip(ARMS, ('full_prompt_tool_loop', 'preplanned_mechanical_sequence',
    'human_selected_sequence', 'supported_native_goal_continue', 'automatic_verified_flow')))
FAMILIES = {
    'cross-layer-feature': ('backend', 'ui', 'storage', 'docs', 'tests'),
    'compatibility-refactor': ('producer', 'consumer', 'legacy-api'),
    'unknown-defect': ('diagnosis', 'reproduction', 'regression'),
    'multi-deliverable': ('multiple-modules', 'documentation', 'original-coverage'),
    'interrupted-execution': ('partial-result', 'original-budgets', 'reconciliation'),
    'upstream-repair': ('upstream-api', 'downstream-revalidation', 'stale-proof'),
    'negative-review': ('reproduced-finding', 'review-verdict'),
    'indivisible-outcome': ('transaction-boundary', 'handoff-cost'),
}
REF = evaluation.FILE_REF
ID = {'type': 'string', 'pattern': '^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$'}
REVISION = {'type': 'string', 'pattern': '^[a-f0-9]{40}([a-f0-9]{24})?$'}
COUNT = {'type': ['integer', 'null'], 'minimum': 0}
SECONDS = {'type': ['number', 'null'], 'minimum': 0}
TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 65536}
STAMP = {'type': ['string', 'null'], 'format': 'utc'}
def array(kind, maximum=10000, minimum=0):
    return {'type': 'array', 'minItems': minimum, 'maxItems': maximum, 'items': kind}
def optional(kind):
    return {'anyOf': [kind, {'type': 'null'}]}
def obj(properties):
    return object_schema(properties, tuple(properties))
def record(name, properties):
    return obj({'schema_version': {'const': 1}, 'record_type': {'const': name}, **properties})

ROLE = obj({'role': ID, 'effort': SHORT})
EFFORT = obj({'root': SHORT, 'roles': array(ROLE, 32)})
REQUIREMENT = obj({'id': ID, 'text': TEXT, 'mandatory': {'type': 'boolean'}, 'tags': array(ID, 32)})
MISSION = record('task_flow_original_mission', {'task_id': ID, 'original_request': TEXT,
    'requirements': array(REQUIREMENT, 512, 1), 'invariants': array(TEXT, 128, 1)})
DAG = record('task_flow_shared_decomposition', {'task_id': ID, 'nodes': array(obj({
    'id': ID, 'requirements': array(ID, 512, 1), 'depends_on': array(ID, 256)}), 256, 1)})
EVALUATOR = record('task_flow_hidden_evaluator', {'task_id': ID, 'revision': REVISION,
    'producer_id': ID, 'requirement_ids': array(ID, 512, 1), 'files': array(REF, 256, 1)})
CONFIGURED = record('task_flow_configured_effort', {'policy_digest': HASH, 'effort': EFFORT})
OBSERVATION = record('task_flow_policy_observation', {'policy_digest': HASH,
    'coverage': {'enum': ['complete', 'partial', 'unknown']}, 'effort': optional(EFFORT),
    'method': {'enum': ['runtime_query', 'client_runtime_metadata']}, 'runtime_evidence': array(REF, 32, 1)})
POLICY = obj({'model': SHORT, 'provider': SHORT, 'client': obj({'id': SHORT, 'artifact': REF}),
    'tools': array(obj({'id': ID, 'artifact': REF}), 64, 1), 'permissions': array(ID, 64, 1),
    'budgets': obj({'max_tokens': {'type': 'integer', 'minimum': 1},
        'max_cost_usd': {'type': 'number', 'exclusiveMinimum': 0},
        'max_wall_seconds': {'type': 'number', 'exclusiveMinimum': 0}}),
    'effort': EFFORT, 'environment': REF, 'cache_regime': {'enum': ['cold', 'warm-controlled']}})
TASK = obj({'id': ID, 'family': {'enum': list(FAMILIES)},
    'source': obj({'commit': REVISION, 'tree': REVISION}), 'mission': REF,
    'public_assets': array(REF, 256), 'hidden_evaluator': REF, 'shared_decomposition': optional(REF)})
SPEC = record('task_flow_experiment', {'experiment_id': ID,
    'bank_kind': {'enum': ['supplied_long_missions', 'synthetic_fixture']}, 'bank_provenance': REF,
    'comparison': {'enum': ['mechanism', 'product']}, 'tasks': array(TASK, 128, 8),
    'policy': POLICY, 'arms': array(obj({'id': {'enum': list(ARMS)},
        'configured_effort': optional(REF)}), 5, 5),
    'seeds': array({'type': 'integer', 'minimum': 0, 'maximum': 2147483647}, 16, 1)})
REQUIREMENT_RESULT = obj({'id': ID, 'status': {'enum': ['passed', 'failed', 'not_run', 'unknown']}})
GRADE = record('task_flow_mission_evaluation', {'preparation_digest': HASH, 'trial_id': HASH,
    'candidate_digest': HASH, 'evaluator_bundle_digest': HASH, 'producer_id': ID,
    'requirements': array(REQUIREMENT_RESULT, 512, 1),
    'verdict': {'enum': ['accepted', 'rejected', 'unknown']}, 'finished_at': STAMP,
    'execution_evidence': REF})
REPORT = record('task_flow_trial_report', {'preparation_digest': HASH, 'trial_id': HASH,
    'task_id': ID, 'arm': {'enum': list(ARMS)}, 'solver_id': ID,
    'measurement_kind': {'enum': ['synthetic', 'observed']},
    'outcome': {'enum': ['completed', 'failed', 'timeout', 'cancelled', 'environment_error', 'unknown']},
    'claimed_complete': {'type': ['boolean', 'null']}, 'candidate_digest': optional(HASH),
    'observed_policy': optional(REF),
    'evaluator_receipt': optional(REF), 'started_at': STAMP, 'finished_at': STAMP,
    'manual_interventions': COUNT, 'human_minutes': SECONDS, 'human_feedback': array(REF, 128),
    'expected_invocation_ids': optional(array(ID, 10000)), 'usage': array(evaluation.USAGE),
    'usage_coverage': {'enum': ['complete', 'partial', 'unknown']},
    'phase_seconds': array(obj({'phase': {'enum': ['initial', 'planner', 'worker', 'integration', 'reviewer', 'repair', 'retry', 'checks', 'evaluator']},
        'seconds': SECONDS}), 10000)})
# Frozen v1 declarations remain readable; only v2 carries a strict subject chain.
GRADE_V2 = deepcopy(GRADE)
GRADE_V2['properties'].update(schema_version={'const': 2}, subject=evaluation.GRADE_SUBJECT,
    candidate_scheme={'const': evaluation.CANDIDATE_SCHEME}, evaluator_input_digest=HASH, finished_at=evaluation.UTC)
GRADE_V2['required'].extend(['subject', 'candidate_scheme', 'evaluator_input_digest'])
REPORT_V2 = deepcopy(REPORT)
REPORT_V2['properties'].update(schema_version={'const': 2}, candidate_digest=HASH,
    subject=evaluation.GRADE_SUBJECT, candidate_scheme={'const': evaluation.CANDIDATE_SCHEME}, evaluator_input_digest=HASH,
    started_at=evaluation.UTC, finished_at=evaluation.UTC)
REPORT_V2['required'].extend(['subject', 'candidate_scheme', 'evaluator_input_digest'])
GRADE_SCHEMA = {'oneOf': [GRADE, GRADE_V2]}
REPORT_SCHEMA = {'oneOf': [REPORT, REPORT_V2]}
SOURCE_CAPTURE = obj({'revision': REVISION, 'tree': REVISION, 'inventory_digest': HASH})
SCHEDULE_ROW = obj({'task_id': ID, 'arm': {'enum': list(ARMS)}, 'seed': {'type': 'integer', 'minimum': 0},
    'rotation': {'type': 'integer', 'minimum': 0, 'maximum': 4}, 'position': {'type': 'integer', 'minimum': 0, 'maximum': 4},
    'trial_id': HASH, 'solve_manifest': REF})
PREPARED_TASK = obj({'id': ID, 'family': {'enum': list(FAMILIES)}, 'source': SOURCE_CAPTURE,
    'mission': REF, 'requirements': array(ID, 512, 1), 'mandatory': array(ID, 512, 1),
    'evaluator': REF, 'evaluator_bundle_digest': HASH, 'evaluator_producer': ID,
    'arm_manifests': obj({arm: REF for arm in ARMS})})
QUALIFICATION = {'production': 'not_run', 'isolation': 'not_run', 'client': 'not_run', 'execution_authorized': False}
PREPARATION = record('task_flow_preparation', {'spec_digest': HASH, 'input_spec_sha256': HASH,
    'bank_kind': SPEC['properties']['bank_kind'], 'bank_authenticity': {'const': 'not_attested'},
    'comparison': SPEC['properties']['comparison'], 'policy_digest': HASH,
    'tasks': array(PREPARED_TASK, 128, 8), 'schedule': array(SCHEDULE_ROW, 10000, 1),
    'counterbalance': {'const': 'sha256_order_cyclic_latin_5_rotations_v1'}, 'qualification': {'const': QUALIFICATION},
    'files': array(obj({'path': SHORT, 'sha256': HASH, 'bytes': {'type': 'integer', 'minimum': 0},
        'mode': {'enum': ['100644', '100755']}, 'visibility': {'enum': ['solve', 'controller', 'hidden']}}), 10000),
    'preparation_digest': HASH})
SCHEMAS = (SPEC, MISSION, DAG, EVALUATOR, CONFIGURED, OBSERVATION, GRADE, GRADE_V2, REPORT, REPORT_V2, PREPARATION)
SCHEMA = {'oneOf': list(SCHEMAS)}
MAX_BYTES = 64 * 1024 * 1024


def _path(name):
    try:
        path = portable_relative_path(name)
    except ValueError as error:
        raise HarnessError('TASK_FLOW_UNSAFE_PATH') from error
    if path.as_posix() != name or any(part in ('', '.', '..') for part in name.split('/')):
        raise HarnessError('TASK_FLOW_NONCANONICAL_PATH')
    reserved = {'con', 'prn', 'aux', 'nul', *('com'+str(i) for i in range(1, 10)), *('lpt'+str(i) for i in range(1, 10))}
    if any(part.endswith(('.', ' ')) or part.split('.')[0].lower() in reserved or any(ord(c) < 32 for c in part) for part in path.parts):
        raise HarnessError('TASK_FLOW_UNSAFE_PATH')
    return path


def _unique(values, label):
    if len(values) != len(set(values)):
        raise HarnessError('TASK_FLOW_DUPLICATE_' + label)


def _document(raw, schema):
    return strict_yaml.loads(raw, schema)


def checked_bytes(ref, root):
    strict_yaml.validate(ref, REF)
    _path(ref['path'])
    return _benchmark_bytes(ref, root)


def _source(repository, declared, target, prefix):
    environment = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    environment.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
    # Bound temporary export before delegating exact byte capture to the
    # existing helper. Metadata is a budget preflight, never source identity.
    inventory = subprocess.run(['git', '--no-optional-locks', '-c', 'core.fsmonitor=false',
        '-c', 'protocol.allow=never', 'ls-tree', '-rlz', '--full-tree', declared['commit']],
        cwd=repository, env=environment, capture_output=True, timeout=30)
    if inventory.returncode or len(inventory.stdout) > 4*1024*1024:
        raise HarnessError('TASK_FLOW_SOURCE_INVENTORY_BOUND_OR_UNAVAILABLE')
    entries = [row for row in inventory.stdout.split(b'\0') if row]
    try:
        total = sum(int(row.split(b'\t', 1)[0].split()[3]) for row in entries)
    except (ValueError, IndexError) as error:
        raise HarnessError('TASK_FLOW_UNSUPPORTED_SOURCE_ENTRY') from error
    if len(entries) > 10000 or total > MAX_BYTES:
        raise HarnessError('TASK_FLOW_SOURCE_EXPORT_BOUND')
    files, captured = capture_local_git_checkout(repository, declared['commit'], target, prefix=prefix)
    if captured['tree'] != declared['tree']:
        raise HarnessError('TASK_FLOW_SOURCE_TREE_CHANGED')
    for row in files:
        if os.name != 'nt' and bool((Path(repository)/_path(row['path'])).stat().st_mode & 0o111) != (row['mode'] == '100755'):
            raise HarnessError('TASK_FLOW_SOURCE_MODE_CHANGED')
    # Never execute clean/smudge filters, even if today's worktree bytes match.
    for start in range(0, len(files), 64):
        names = [row['path'] for row in files[start:start+64]]
        result = subprocess.run(['git', '--no-optional-locks', '-c', 'core.fsmonitor=false',
            'check-attr', '-z', 'filter', 'working-tree-encoding', 'ident', '--', *names],
            cwd=repository, env=environment, capture_output=True, timeout=30)
        if result.returncode:
            raise HarnessError('TASK_FLOW_SOURCE_ATTRIBUTES_UNAVAILABLE')
        fields = result.stdout.split(b'\0')
        if any(fields[index] not in (b'unspecified', b'unset') for index in range(2, len(fields)-1, 3)):
            raise HarnessError('TASK_FLOW_SOURCE_FILTER_UNSUPPORTED')
    return files, captured


def _dag(value, mission):
    ids = [node['id'] for node in value['nodes']]; _unique(ids, 'NODE')
    allowed = {row['id'] for row in mission['requirements']}
    covered, done = set(), set()
    for node in value['nodes']:
        _unique(node['depends_on'], 'DEPENDENCY')
        if not set(node['depends_on']) <= set(ids) or not set(node['requirements']) <= allowed:
            raise HarnessError('TASK_FLOW_DECOMPOSITION_BINDING')
        covered.update(node['requirements'])
    while True:
        ready = {node['id'] for node in value['nodes'] if set(node['depends_on']) <= done}
        if ready <= done:
            break
        done.update(ready)
    if len(done) != len(ids) or not {r['id'] for r in mission['requirements'] if r['mandatory']} <= covered:
        raise HarnessError('TASK_FLOW_DECOMPOSITION_CYCLE_OR_OMISSION')


def _schedule(spec, tasks):
    rows = []
    for seed in spec['seeds']:
        base = sorted(ARMS, key=lambda arm: digest({'seed': seed, 'arm': arm}))
        for task in tasks:
            for rotation in range(5):
                for position, arm in enumerate(base[rotation:]+base[:rotation]):
                    row = {'task_id': task['id'], 'arm': arm, 'seed': seed, 'rotation': rotation, 'position': position}
                    rows.append({**row, 'trial_id': digest({'spec_digest': digest(spec), **row}), 'solve_manifest': task['arm_manifests'][arm]})
    if len(rows) > 10000: raise HarnessError('TASK_FLOW_SCHEDULE_BOUND')
    return rows


class _MaterialInventory:
    """Staged final bytes, with immutable portable paths and metadata."""
    def __init__(self):
        self.files = {}
        self.paths = {}
        self.total_bytes = 0

    def put(self, name, raw, audience, mode='100644'):
        name = _path(name).as_posix()
        key = name.casefold()
        value = (raw, mode, audience)
        if key in self.paths:
            prior_name = self.paths[key]
            prior = self.files[prior_name]
            if prior[2] != audience:
                raise HarnessError('TASK_FLOW_HIDDEN_EVALUATOR_LEAK_OR_OVERLAP: VISIBILITY_CONFLICT')
            if prior_name != name or prior != value:
                raise HarnessError('TASK_FLOW_OUTPUT_COLLISION')
        else:
            self.paths[key] = name
            self.files[name] = value
            self.total_bytes += len(raw)
        if len(self.files) > 10000 or self.total_bytes > MAX_BYTES:
            raise HarnessError('TASK_FLOW_PREPARATION_BOUND')
        return {'path': name, 'sha256': hashlib.sha256(raw).hexdigest()}


class _InputInventory:
    """Classify original references, including explicitly shared public inputs."""
    def __init__(self):
        self.refs = {}

    def add(self, ref, audience):
        name = _path(ref['path']).as_posix()
        key = name.casefold()
        prior = self.refs.get(key)
        if prior is not None:
            if prior[1] != audience:
                raise HarnessError('TASK_FLOW_HIDDEN_EVALUATOR_LEAK_OR_OVERLAP: VISIBILITY_CONFLICT')
            if prior[0] != ref:
                raise HarnessError('TASK_FLOW_INPUT_REF_CONFLICT')
        self.refs[key] = (ref, audience)


def _final_visibility(material, spec):
    """Rebuild visibility from frozen references and final bytes, not row claims.

    This exact-byte guard is cooperative validation, not semantic leakage
    detection, a signature, or proof of runtime mount isolation.
    """
    inputs = _InputInventory()
    public_hashes, hidden_hashes = set(), set()
    staged = _MaterialInventory()
    for name, (raw, mode, audience) in material.items():
        staged.put(name, raw, audience, mode)
        expected = {'solve': 'solve', 'controller': 'controller', 'evaluator-private': 'hidden'}.get(name.split('/')[0])
        if expected is None or audience != expected:
            raise HarnessError('TASK_FLOW_VISIBILITY_METADATA_CONFLICT')
        if audience == 'solve': public_hashes.add(hashlib.sha256(raw).hexdigest())
        if audience == 'hidden': hidden_hashes.add(hashlib.sha256(raw).hexdigest())

    def asset(ref, audience):
        inputs.add(ref, audience)
        name = ('evaluator-private' if audience == 'hidden' else 'controller')+'/inputs/'+ref['sha256']
        if name not in material or hashlib.sha256(material[name][0]).hexdigest() != ref['sha256']:
            raise HarnessError('TASK_FLOW_FROZEN_INPUT_BINDING')
        if audience == 'solve': public_hashes.add(ref['sha256'])
        return material[name][0]

    asset(spec['bank_provenance'], 'controller')
    for ref in [spec['policy']['client']['artifact'], spec['policy']['environment'],
                *[tool['artifact'] for tool in spec['policy']['tools']],
                *[arm['configured_effort'] for arm in spec['arms'] if arm['configured_effort']]]:
        asset(ref, 'solve')
    for task in spec['tasks']:
        asset(task['mission'], 'solve')
        if task['shared_decomposition']: asset(task['shared_decomposition'], 'solve')
        for ref in task['public_assets']: asset(ref, 'solve')
        evaluator = _document(asset(task['hidden_evaluator'], 'hidden'), EVALUATOR)
        for ref in evaluator['files']: asset(ref, 'hidden')
    if public_hashes & hidden_hashes:
        raise HarnessError('TASK_FLOW_HIDDEN_EVALUATOR_LEAK_OR_OVERLAP')


def _read_material(root, rows, *, complete):
    """Read the actual complete inventory before trusting/publishing its receipt."""
    _unique([row['path'] for row in rows], 'FROZEN_FILE')
    expected = {row['path'] for row in rows} | ({'preparation.json'} if complete else set())
    actual, directories = set(), set()
    for path in root.rglob('*'):
        _ordinary(path, directory=path.is_dir())
        if path.is_file(): actual.add(path.relative_to(root).as_posix())
        if path.is_dir(): directories.add(path.relative_to(root).as_posix())
        if len(actual) > 10001: raise HarnessError('TASK_FLOW_PREPARATION_BOUND')
    if actual != expected: raise HarnessError('TASK_FLOW_PREPARATION_INVENTORY')
    expected_directories = {parent.as_posix() for name in expected for parent in Path(name).parents if parent != Path('.')}
    if directories != expected_directories: raise HarnessError('TASK_FLOW_PREPARATION_DIRECTORY_INVENTORY')
    staged = _MaterialInventory()
    for row in rows:
        path = root/_path(row['path']); raw = read_regular_file(path, max_bytes=MAX_BYTES)
        if len(raw) != row['bytes'] or hashlib.sha256(raw).hexdigest() != row['sha256']:
            raise HarnessError('TASK_FLOW_PREPARATION_BYTES_CHANGED')
        if os.name != 'nt' and bool(path.stat().st_mode & 0o111) != (row['mode'] == '100755'):
            raise HarnessError('TASK_FLOW_PREPARATION_MODE_CHANGED')
        staged.put(row['path'], raw, row['visibility'], row['mode'])
    return staged.files


def prepare(spec_path, *, assets_root, source_roots, output_dir):
    """Freeze supplied local inputs; retries revalidate inputs and exact outputs.

    Partial directories are retained and refused. No existing file is replaced.
    The controller must withhold controller/evaluator-private from solve mounts;
    this preparation does not establish that runtime isolation actually exists.
    """
    raw_spec = read_regular_file(safe_private(spec_path), max_bytes=2*1024*1024)
    spec = _document(raw_spec, SPEC)
    output = safe_private(Path(output_dir).absolute()); assets_root = safe_private(Path(assets_root).absolute())
    ids = [task['id'] for task in spec['tasks']]; _unique(ids, 'TASK')
    _unique([name.casefold() for name in ids], 'PORTABLE_TASK')
    for name in ids: _path(name)
    if set(source_roots) != set(ids) or {task['family'] for task in spec['tasks']} != set(FAMILIES):
        raise HarnessError('TASK_FLOW_INCOMPLETE_SUPPLIED_BANK')
    if {arm['id'] for arm in spec['arms']} != set(ARMS):
        raise HarnessError('TASK_FLOW_ARMS_MUST_BE_DISTINCT')
    _unique(spec['seeds'], 'SEED'); _unique(spec['policy']['permissions'], 'PERMISSION')
    _unique([tool['id'] for tool in spec['policy']['tools']], 'TOOL')
    _unique([role['role'] for role in spec['policy']['effort']['roles']], 'ROLE')
    for value in [spec['policy']['model'], spec['policy']['provider'], spec['policy']['client']['id'],
                  spec['policy']['effort']['root'], *[r['effort'] for r in spec['policy']['effort']['roles']]]:
        if value.strip().lower() in ('', 'unknown', 'unavailable', 'default', 'installed'):
            raise HarnessError('TASK_FLOW_REQUESTED_IDENTITY_UNAVAILABLE')
    for source in source_roots.values():
        source = safe_private(Path(source).absolute())
        if output == source or output.is_relative_to(source) or source.is_relative_to(output):
            raise HarnessError('TASK_FLOW_OUTPUT_SOURCE_OVERLAP')
    if output == assets_root or output.is_relative_to(assets_root) or assets_root.is_relative_to(output):
        raise HarnessError('TASK_FLOW_OUTPUT_ASSET_OVERLAP')
    _mkdir(output.parent)
    staged = _MaterialInventory()
    material, inputs = staged.files, _InputInventory()
    put = staged.put
    def asset(ref, audience):
        inputs.add(ref, audience)
        raw = checked_bytes(ref, assets_root)
        put(('evaluator-private' if audience == 'hidden' else 'controller')+'/inputs/'+ref['sha256'],
            raw, 'hidden' if audience == 'hidden' else 'controller')
        return raw
    asset(spec['bank_provenance'], 'controller')
    for ref in [spec['policy']['client']['artifact'], spec['policy']['environment'], *[t['artifact'] for t in spec['policy']['tools']]]:
        asset(ref, 'solve')
    configured = {}
    for arm in spec['arms']:
        ref = arm['configured_effort']
        value = _document(asset(ref, 'solve'), CONFIGURED) if ref else None
        if value and (value['policy_digest'] != digest(spec['policy']) or value['effort'] != spec['policy']['effort']):
            raise HarnessError('TASK_FLOW_CONFIGURED_POLICY_MISMATCH')
        configured[arm['id']] = {'status': 'declared_unqualified' if ref else 'unavailable',
                                  'value': value['effort'] if value else None, 'evidence': ref}
    tasks = []
    with tempfile.TemporaryDirectory(prefix='task-flow-capture-', dir=output.parent) as temporary:
        capture_root = Path(temporary)
        for task in sorted(spec['tasks'], key=lambda row: row['id']):
            name = task['id']; mission_raw = asset(task['mission'], 'solve'); mission = _document(mission_raw, MISSION)
            evaluator_raw = asset(task['hidden_evaluator'], 'hidden'); evaluator = _document(evaluator_raw, EVALUATOR)
            requirement_ids = [row['id'] for row in mission['requirements']]; _unique(requirement_ids, 'REQUIREMENT')
            _unique(evaluator['requirement_ids'], 'EVALUATOR_REQUIREMENT')
            if mission['task_id'] != name or evaluator['task_id'] != name or set(evaluator['requirement_ids']) != set(requirement_ids):
                raise HarnessError('TASK_FLOW_ORIGINAL_REQUIREMENT_BINDING')
            tags = {tag for row in mission['requirements'] if row['mandatory'] for tag in row['tags']}
            if not set(FAMILIES[task['family']]) <= tags:
                raise HarnessError('TASK_FLOW_MISSING_FAMILY_REQUIREMENTS')
            evaluator_files = []
            for ref in evaluator['files']:
                asset(ref, 'hidden')
                evaluator_files.append({'path': 'evaluator-private/inputs/'+ref['sha256'], 'sha256': ref['sha256']})
            mission_ref = put('solve/'+name+'/mission.json', canonical(mission), 'solve')
            shared = task['shared_decomposition']
            if (spec['comparison'] == 'mechanism') != (shared is not None):
                raise HarnessError('TASK_FLOW_COMPARISON_INFORMATION_MISMATCH')
            shared_ref = None
            if shared:
                raw = asset(shared, 'solve'); dag = _document(raw, DAG)
                if dag['task_id'] != name:
                    raise HarnessError('TASK_FLOW_DECOMPOSITION_BINDING')
                _dag(dag, mission)
                shared_ref = put('solve/'+name+'/decomposition.json', canonical(dag), 'solve')
            public = []
            for ref in task['public_assets']:
                raw = asset(ref, 'solve')
                public.append(put('solve/'+name+'/assets/'+ref['sha256'], raw, 'solve'))
            files, source = _source(source_roots[name], task['source'], capture_root, 'solve/'+name+'/source')
            for item in files:
                ref = item['asset']; raw = read_regular_file(capture_root/_path(ref['path']))
                put(ref['path'], raw, 'solve', item['mode'])
            evaluator_ref = put('evaluator-private/'+name+'/manifest.json', canonical({**evaluator, 'files': evaluator_files}), 'hidden')
            bundle_digest = digest({'manifest': task['hidden_evaluator'], 'files': evaluator['files']})
            arm_refs = {}
            for arm in ARMS:
                manifest = {'schema_version': 1, 'record_type': 'task_flow_solve_preparation', 'task_id': name,
                    'arm': arm, 'mechanism': MECHANISMS[arm], 'comparison': spec['comparison'], 'source': source, 'source_files': files,
                    'mission': mission_ref, 'public_assets': public, 'shared_decomposition': shared_ref,
                    'requested_policy': spec['policy'], 'effort': {'requested': spec['policy']['effort'],
                        'configured': configured[arm], 'observed': {'status': 'not_run', 'value': None}},
                    'capability': {'status': 'not_run', 'launch_allowed': False}}
                arm_refs[arm] = put('solve/'+name+'/arms/'+arm+'.json', canonical(manifest), 'solve')
            tasks.append({'id': name, 'family': task['family'], 'source': source, 'mission': mission_ref,
                'requirements': requirement_ids, 'mandatory': [r['id'] for r in mission['requirements'] if r['mandatory']],
                'evaluator': evaluator_ref, 'evaluator_bundle_digest': bundle_digest,
                'evaluator_producer': evaluator['producer_id'], 'arm_manifests': arm_refs})
        put('controller/spec.original', raw_spec, 'controller')
        put('controller/spec.json', canonical(spec), 'controller')
        _final_visibility(material, spec)
        schedule = _schedule(spec, tasks)
        receipt = {'schema_version': 1, 'record_type': 'task_flow_preparation', 'spec_digest': digest(spec),
            'input_spec_sha256': hashlib.sha256(raw_spec).hexdigest(), 'bank_kind': spec['bank_kind'],
            'bank_authenticity': 'not_attested', 'comparison': spec['comparison'], 'policy_digest': digest(spec['policy']),
            'tasks': tasks, 'schedule': schedule, 'counterbalance': 'sha256_order_cyclic_latin_5_rotations_v1',
            'qualification': QUALIFICATION,
            'files': [{'path': name, 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw), 'mode': mode,
                       'visibility': audience} for name, (raw, mode, audience) in sorted(material.items())]}
        receipt['preparation_digest'] = digest(receipt)
        strict_yaml.validate(receipt, PREPARATION)
        receipt_raw = canonical(receipt)
        if len(receipt_raw) > 2*1024*1024:
            raise HarnessError('TASK_FLOW_PREPARATION_BOUND')
        if output.exists():
            if not (output/'preparation.json').exists():
                raise HarnessError('TASK_FLOW_PARTIAL_PREPARATION: preserve incomplete output and use a fresh directory')
            existing = verify(output)
            if existing != receipt:
                raise HarnessError('TASK_FLOW_PREPARATION_COLLISION')
            return existing
        # Claim exclusively; a killed publication leaves a visible partial
        # directory that retries refuse. Existing user directories are preserved.
        output.mkdir(mode=0o700)
        for name, (raw, mode, audience) in material.items():
            target = output/_path(name); atomic_write(target, raw, exclusive=True)
            if mode == '100755' and os.name != 'nt': target.chmod(0o700)
        # Recheck inputs after writes before publishing a complete receipt.
        if read_regular_file(safe_private(spec_path), max_bytes=2*1024*1024) != raw_spec:
            raise HarnessError('TASK_FLOW_SPEC_DRIFT')
        for ref, audience in inputs.refs.values(): checked_bytes(ref, assets_root)
        with tempfile.TemporaryDirectory(prefix='task-flow-recheck-', dir=output.parent) as recheck:
            for task in spec['tasks']:
                _, source = _source(source_roots[task['id']], task['source'], Path(recheck), 'solve/'+task['id']+'/source')
                if source != next(row['source'] for row in tasks if row['id'] == task['id']):
                    raise HarnessError('TASK_FLOW_SOURCE_DRIFT')
        written = _read_material(output, receipt['files'], complete=False)
        _final_visibility(written, spec)
        atomic_write(output/'preparation.json', receipt_raw, exclusive=True)
        return verify(output)


def verify(preparation_dir, *, expected_digest=None):
    """Check exact frozen local files; never promote qualification or run tests."""
    root = safe_private(Path(preparation_dir).absolute())
    receipt = _document(read_regular_file(root/'preparation.json', max_bytes=2*1024*1024), PREPARATION)
    content = dict(receipt); supplied = content.pop('preparation_digest')
    if digest(content) != supplied or receipt['record_type'] != 'task_flow_preparation':
        raise HarnessError('TASK_FLOW_PREPARATION_INTEGRITY')
    if expected_digest is not None and supplied != expected_digest: raise HarnessError('TASK_FLOW_EXPECTED_DIGEST_CHANGED')
    _unique([row['path'] for row in receipt['files']], 'FROZEN_FILE')
    _unique([row['id'] for row in receipt['tasks']], 'FROZEN_TASK')
    _unique([row['trial_id'] for row in receipt['schedule']], 'TRIAL')
    material = _read_material(root, receipt['files'], complete=True)
    spec = _document(read_regular_file(root/'controller/spec.json'), SPEC)
    _final_visibility(material, spec)
    if receipt['spec_digest'] != digest(spec) or receipt['policy_digest'] != digest(spec['policy']) or receipt['schedule'] != _schedule(spec, receipt['tasks']):
        raise HarnessError('TASK_FLOW_FROZEN_SPEC_BINDING')
    return receipt


def _instant(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')) if value is not None else None


def _mission_chain(prepared, task, grade, reports_root, preparation_dir):
    pipeline = _document(checked_bytes(grade['execution_evidence'], reports_root), evaluation.GRADE_SCHEMA)
    identity = evaluation.grade_identity(pipeline)
    if (identity['manifest_digest'] != prepared['preparation_digest'] or identity['trial_id'] != grade['trial_id']
            or identity['evaluator_digest'] != task['evaluator_bundle_digest']):
        raise HarnessError('TASK_FLOW_PIPELINE_GRADE_BINDING')
    status = evaluation.verify_execution_grade(pipeline, reports_root)
    if grade['schema_version'] == 1: return pipeline, 'unverified_legacy'
    subject = grade['subject']
    expected = {'manifest_digest': prepared['preparation_digest'], 'task_id': task['id'], 'trial_id': grade['trial_id'],
        'candidate_digest': grade['candidate_digest'], 'candidate_scheme': grade['candidate_scheme'],
        'evaluator_digest': task['evaluator_bundle_digest']}
    if (pipeline['schema_version'] != 2 or any(subject[key] != value for key, value in expected.items())
            or pipeline['subject'] != subject or grade['evaluator_input_digest'] != pipeline['evaluator_input_digest']
            or grade['finished_at'] != pipeline['finished_at']
            or grade['requirements'] != pipeline['requirements']
            or {row['id'] for row in pipeline['requirements']} != set(task['requirements'])):
        raise HarnessError('TASK_FLOW_GRADE_SUBJECT_CHAIN_BINDING')
    actual_inputs = evaluation._evidence_record(pipeline['evaluator_input'], reports_root, evaluation.EVALUATOR_INPUT)
    evaluator = _document(checked_bytes(task['evaluator'], preparation_dir), EVALUATOR)
    actual_hidden = {row['asset']['sha256'] for row in actual_inputs['artifacts'] if row['role'] == 'protected'}
    if not {ref['sha256'] for ref in evaluator['files']} <= actual_hidden:
        raise HarnessError('TASK_FLOW_EVALUATOR_INPUT_BINDING')
    return pipeline, status


def write_mission_grade(path, *, preparation_dir, trial_id, execution_evidence, reports_root):
    """Create the mission wrapper from the actual validated execution chain."""
    prepared = verify(preparation_dir)
    row = next((row for row in prepared['schedule'] if row['trial_id'] == trial_id), None)
    if row is None: raise HarnessError('TASK_FLOW_REPORT_BINDING')
    task = next(task for task in prepared['tasks'] if task['id'] == row['task_id'])
    pipeline = _document(checked_bytes(execution_evidence, reports_root), evaluation.EXECUTION_GRADE)
    evaluation.verify_execution_grade(pipeline, reports_root)
    subject = pipeline['subject']
    accepted = pipeline['outcome'] == 'accepted'
    value = {'schema_version': 2, 'record_type': 'task_flow_mission_evaluation',
        'preparation_digest': prepared['preparation_digest'], 'trial_id': trial_id,
        'candidate_digest': subject['candidate_digest'], 'candidate_scheme': subject['candidate_scheme'], 'subject': subject,
        'evaluator_input_digest': pipeline['evaluator_input_digest'], 'evaluator_bundle_digest': task['evaluator_bundle_digest'],
        'producer_id': task['evaluator_producer'], 'requirements': pipeline['requirements'],
        'verdict': 'accepted' if accepted else 'rejected', 'finished_at': pipeline['finished_at'], 'execution_evidence': execution_evidence}
    _mission_chain(prepared, task, value, reports_root, preparation_dir)
    return evaluation.put_grade_evidence(reports_root, Path(path).absolute().relative_to(Path(reports_root).absolute()).as_posix(), value, GRADE_V2)


def write_trial_report(path, *, preparation_dir, evaluator_receipt, reports_root, report):
    """Bind a supplied observation to a validated wrapper; never echo its candidate."""
    prepared = verify(preparation_dir)
    grade = _document(checked_bytes(evaluator_receipt, reports_root), GRADE_V2)
    row = next((row for row in prepared['schedule'] if row['trial_id'] == grade['trial_id']), None)
    if row is None: raise HarnessError('TASK_FLOW_REPORT_BINDING')
    task = next(task for task in prepared['tasks'] if task['id'] == row['task_id'])
    _mission_chain(prepared, task, grade, reports_root, preparation_dir)
    value = {**report, 'schema_version': 2, 'preparation_digest': prepared['preparation_digest'],
        'trial_id': row['trial_id'], 'task_id': row['task_id'], 'arm': row['arm'], 'evaluator_receipt': evaluator_receipt,
        **{key: grade[key] for key in ('candidate_digest', 'candidate_scheme', 'subject', 'evaluator_input_digest')}}
    aggregate(preparation_dir, [value], reports_root=reports_root)
    return evaluation.put_grade_evidence(reports_root, Path(path).absolute().relative_to(Path(reports_root).absolute()).as_posix(), value, REPORT_V2)


def aggregate(preparation_dir, reports, *, reports_root):
    """Aggregate future supplied reports; missing instrumentation stays unknown.

    Independent grade bytes are subject-bound declarations, not authentication of
    their producer or production execution. Phase durations are never summed
    into wall time. Planner/reviewer/repair invocation rows share one denominator.
    """
    prepared = verify(preparation_dir); planned = {row['trial_id']: row for row in prepared['schedule']}
    spec_file = next(row for row in prepared['files'] if row['path'] == 'controller/spec.json')
    policy = _document(checked_bytes({key: spec_file[key] for key in ('path', 'sha256')}, preparation_dir), SPEC)['policy']
    tasks = {row['id']: row for row in prepared['tasks']}; observed, invocations = {}, set()
    for index, report in enumerate(reports):
        if index >= 10000: raise HarnessError('TASK_FLOW_REPORT_BOUND')
        strict_yaml.validate(report, REPORT_SCHEMA)
        key = report['trial_id']; plan = planned.get(key)
        if not plan or report['preparation_digest'] != prepared['preparation_digest'] or any(report[name] != plan[name] for name in ('task_id', 'arm')):
            raise HarnessError('TASK_FLOW_REPORT_BINDING')
        if key in observed:
            if observed[key]['report'] != report: raise HarnessError('TASK_FLOW_REPORT_COLLISION')
            continue
        task = tasks[plan['task_id']]; grade = None; binding_status = 'unverified_legacy'
        policy_observation = None
        if report['observed_policy'] is not None:
            policy_observation = _document(checked_bytes(report['observed_policy'], reports_root), OBSERVATION)
            for ref in policy_observation['runtime_evidence']: checked_bytes(ref, reports_root)
            if policy_observation['coverage'] == 'complete' and policy_observation['effort'] is None:
                raise HarnessError('TASK_FLOW_POLICY_OBSERVATION_INCOMPLETE')
        if report['evaluator_receipt'] is not None:
            grade = _document(checked_bytes(report['evaluator_receipt'], reports_root), GRADE_SCHEMA)
            if any(grade[name] != report[name] for name in ('preparation_digest', 'trial_id', 'candidate_digest')) or grade['evaluator_bundle_digest'] != task['evaluator_bundle_digest'] or grade['producer_id'] != task['evaluator_producer'] or grade['producer_id'] == report['solver_id']:
                raise HarnessError('TASK_FLOW_INDEPENDENT_GRADE_BINDING')
            pipeline_grade, binding_status = _mission_chain(prepared, task, grade, reports_root, preparation_dir)
            if grade['schema_version'] == 2:
                if (report['schema_version'] != 2 or report['subject'] != grade['subject']
                        or report['candidate_scheme'] != grade['candidate_scheme']
                        or report['evaluator_input_digest'] != grade['evaluator_input_digest']):
                    raise HarnessError('TASK_FLOW_REPORT_GRADE_CHAIN_BINDING')
            _unique([row['id'] for row in grade['requirements']], 'GRADE_REQUIREMENT')
            if {row['id'] for row in grade['requirements']} != set(task['requirements']):
                raise HarnessError('TASK_FLOW_GRADE_REQUIREMENT_OMISSION')
        results = {row['id']: row['status'] for row in grade['requirements']} if grade else {}
        declared_verified = bool(grade and grade['verdict'] == 'accepted' and all(results[r] == 'passed' for r in task['mandatory']))
        verified = declared_verified and binding_status == 'verified_subject_chain'
        if grade and grade['verdict'] == 'accepted' and not declared_verified:
            raise HarnessError('TASK_FLOW_FALSE_EVALUATOR_ACCEPTANCE')
        if declared_verified and pipeline_grade['outcome'] != 'accepted': raise HarnessError('TASK_FLOW_PIPELINE_GRADE_CONFLICT')
        if grade and grade['verdict'] == 'rejected' and pipeline_grade['outcome'] == 'accepted':
            raise HarnessError('TASK_FLOW_PIPELINE_GRADE_CONFLICT')
        if verified and report['outcome'] != 'completed': raise HarnessError('TASK_FLOW_OUTCOME_CONFLICT')
        start, finish = _instant(report['started_at']), _instant(report['finished_at'])
        verified_at = _instant(grade['finished_at']) if grade and verified else None
        if verified and (start is None or finish is None or _instant(pipeline_grade['started_at']) < start or _instant(pipeline_grade['finished_at']) > finish):
            raise HarnessError('TASK_FLOW_INVALID_TIMELINE')
        if start and finish and finish < start or verified_at and (start and verified_at < start or finish and verified_at > finish):
            raise HarnessError('TASK_FLOW_INVALID_TIMELINE')
        expected = report['expected_invocation_ids']; ids = [row['invocation_id'] for row in report['usage']]
        _unique(ids, 'USAGE_INVOCATION')
        if expected is not None:
            _unique(expected, 'EXPECTED_INVOCATION')
            if not set(ids) <= set(expected): raise HarnessError('TASK_FLOW_UNPLANNED_USAGE')
        if invocations & set(ids): raise HarnessError('TASK_FLOW_OVERLAPPING_USAGE')
        invocations.update(ids)
        for usage in report['usage']:
            if usage['status'] == 'unavailable' and (usage['input_tokens'] is not None or usage['output_tokens'] is not None):
                raise HarnessError('TASK_FLOW_UNAVAILABLE_USAGE_HAS_VALUES')
        usage_complete = bool(expected) and set(ids) == set(expected) and all(row['status'] == 'complete' and row['input_tokens'] is not None and row['output_tokens'] is not None for row in report['usage'])
        if report['usage_coverage'] == 'complete' and not usage_complete:
            raise HarnessError('TASK_FLOW_FALSE_USAGE_COVERAGE')
        for ref in report['human_feedback']: checked_bytes(ref, reports_root)
        tokens = [value for row in report['usage'] if row['status'] != 'conflicting' for value in (row['input_tokens'], row['output_tokens']) if value is not None]
        observed[key] = {'report': report, 'verified': verified,
            'grade_binding': binding_status, 'observed_policy': policy_observation,
            'grade_unknown': grade is None or grade['verdict'] == 'unknown' or binding_status != 'verified_subject_chain',
            'false_completion': None if binding_status != 'verified_subject_chain' and declared_verified else False if report['claimed_complete'] is False else (not verified) if report['claimed_complete'] is True and grade and grade['verdict'] != 'unknown' else None,
            'mandatory_passed': sum(results.get(r) == 'passed' for r in task['mandatory']),
            'mandatory_failed': sum(results.get(r) == 'failed' for r in task['mandatory']),
            'requirement_unknown': sum(results.get(r, 'unknown') in ('unknown', 'not_run') for r in task['mandatory']),
            'tokens': sum(tokens) if tokens else None, 'usage_complete': usage_complete and report['usage_coverage'] == 'complete',
            'wall_seconds': (finish-start).total_seconds() if start and finish else None,
            'time_to_verified_seconds': (verified_at-start).total_seconds() if start and verified_at else None}
    arms = {}
    for arm in ARMS:
        selected = [observed[row['trial_id']] for row in planned.values() if row['arm'] == arm and row['trial_id'] in observed]
        expected_rows = [row for row in planned.values() if row['arm'] == arm]
        missing = len(expected_rows)-len(selected)
        known_tokens = [row['tokens'] for row in selected if row['tokens'] is not None]
        interventions = [row['report']['manual_interventions'] for row in selected if row['report']['manual_interventions'] is not None]
        arms[arm] = {'planned_trials': len(expected_rows), 'reported_trials': len(selected), 'not_run': missing,
            'reported_full_mission_verified': sum(row['verified'] for row in selected),
            'unverified_legacy_grades': sum(row['grade_binding'] == 'unverified_legacy' and row['report']['evaluator_receipt'] is not None for row in selected),
            'full_mission_unknown': missing+sum(row['grade_unknown'] for row in selected),
            'mandatory_requirements_planned': sum(len(tasks[row['task_id']]['mandatory']) for row in expected_rows),
            'mandatory_requirements_passed': sum(row['mandatory_passed'] for row in selected),
            'mandatory_requirements_failed': sum(row['mandatory_failed'] for row in selected),
            'mandatory_requirements_unknown': sum(row['requirement_unknown'] for row in selected)+sum(len(tasks[row['task_id']]['mandatory']) for row in expected_rows if row['trial_id'] not in observed),
            'false_completions': sum(row['false_completion'] is True for row in selected),
            'false_completion_unknown': missing+sum(row['false_completion'] is None for row in selected),
            'manual_interventions_observed': sum(interventions) if interventions else None,
            'manual_interventions_unknown': missing+sum(row['report']['manual_interventions'] is None for row in selected),
            'observed_tokens': sum(known_tokens) if known_tokens else None,
            'usage_coverage': 'complete' if not missing and all(row['usage_complete'] for row in selected) else 'partial' if known_tokens else 'unknown',
            'usage_unknown_trials': missing+sum(not row['usage_complete'] for row in selected),
            'reported_wall_seconds': [row['wall_seconds'] for row in selected],
            'time_to_verified_seconds': [row['time_to_verified_seconds'] for row in selected],
            'phase_seconds': [phase for row in selected for phase in row['report']['phase_seconds']],
            'human_minutes': [row['report']['human_minutes'] for row in selected],
            'human_feedback_count': sum(len(row['report']['human_feedback']) for row in selected)}
        arms[arm]['requested_effort'] = policy['effort']
        arms[arm]['observed_effort'] = [row['observed_policy'] for row in selected]
        arms[arm]['policy_observation_unknown_trials'] = missing+sum(not row['observed_policy'] or row['observed_policy']['coverage'] != 'complete' for row in selected)
        arms[arm]['policy_mismatch_trials'] = sum(bool(row['observed_policy']) and (row['observed_policy']['policy_digest'] != prepared['policy_digest'] or row['observed_policy']['effort'] is not None and row['observed_policy']['effort'] != policy['effort']) for row in selected)
    return {'schema_version': 1, 'record_type': 'task_flow_report_summary',
        'preparation_digest': prepared['preparation_digest'], 'status': 'not_run' if not observed else 'supplied_reports_unqualified',
        'measurement_kind': 'synthetic' if any(row['report']['measurement_kind'] == 'synthetic' for row in observed.values()) else 'observed_declaration' if observed else 'unavailable',
        'production_qualified': False, 'winner': None, 'arms': arms}


def dispatch(argv):
    """Integration point: root CLI forwards task-flow-experiment arguments here."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    command = commands.add_parser('prepare'); command.add_argument('--spec', required=True)
    command.add_argument('--assets-root', required=True); command.add_argument('--output', required=True)
    command.add_argument('--source-root', action='append', default=[], metavar='TASK_ID=PATH')
    command = commands.add_parser('verify'); command.add_argument('--preparation', required=True)
    command = commands.add_parser('aggregate'); command.add_argument('--preparation', required=True)
    command.add_argument('--reports', required=True, help='Strict JSONL trial reports')
    command.add_argument('--reports-root', required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == 'prepare':
            sources = {}
            for item in args.source_root:
                name, separator, path = item.partition('=')
                if not separator or name in sources: raise HarnessError('TASK_FLOW_SOURCE_ARGUMENT')
                sources[name] = path
            value = prepare(args.spec, assets_root=args.assets_root, source_roots=sources, output_dir=args.output)
            value = {'preparation_digest': value['preparation_digest'], 'status': 'prepared_not_run', 'trials': len(value['schedule']), 'qualification': value['qualification']}
        elif args.action == 'verify':
            receipt = verify(args.preparation)
            value = {'status': 'prepared_bytes_verified', 'preparation_digest': receipt['preparation_digest'], 'qualification': receipt['qualification']}
        else:
            raw = read_regular_file(safe_private(args.reports), max_bytes=MAX_BYTES)
            from .benchmark_bundles import _json
            reports = [_json(line) for line in raw.split(b'\n') if line]
            if len(reports) > 10000: raise HarnessError('TASK_FLOW_REPORT_BOUND')
            value = aggregate(args.preparation, reports, reports_root=args.reports_root)
        from .output import write_json
        write_json(value); return 0
    except (OSError, ValueError, HarnessError) as error:
        from .output import write_json
        write_json({'status': 'error', 'error': str(error)}); return 2


if __name__ == '__main__':
    raise SystemExit(dispatch(__import__('sys').argv[1:]))
