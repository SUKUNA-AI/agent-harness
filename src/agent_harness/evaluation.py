"""Offline benchmark manifest validation and paired cohort accounting.

No model calls, downloads, or evaluator commands are executed by this module.
Assets and independent grading are supplied through explicit local receipts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import random
import statistics

from .core import HarnessError, digest
from . import benchmark_metrics
from .execution_ledger import HASH, SHORT, object_schema, safe_private
from .file_io import read_regular_file
from .strict_yaml import load, loads, dump, validate

SUITES = ("swe-bench-verified", "terminal-bench-2.1", "aider-polyglot",
          "harness-recovery", "harness-retrieval-long-task")
ARMS = {"package": ("plain_stock", "harness_full"),
        "matched": ("plain_matched", "harness_matched")}
FILE_REF = object_schema({"path": SHORT, "sha256": HASH}, ("path", "sha256"))
TASK = object_schema({"id": SHORT, "source": FILE_REF}, ("id", "source"))
SUITE = object_schema({"id": {"enum": list(SUITES)}, "revision": SHORT,
    "assets": FILE_REF, "evaluator": FILE_REF, "adapter": FILE_REF,
    "toolchain": FILE_REF, "tasks": {"type": "array", "minItems": 1, "maxItems": 10000, "items": TASK}},
    ("id", "revision", "assets", "evaluator", "adapter", "toolchain", "tasks"))
MANIFEST_SCHEMA = object_schema({"schema_version": {"const": 1}, "record_type": {"const": "benchmark_manifest"},
    "comparison": {"enum": list(ARMS)}, "seed": {"type": "integer", "minimum": 0},
    "repeats": {"type": "integer", "minimum": 1, "maximum": 20},
    "bootstrap_resamples": {"type": "integer", "minimum": 100, "maximum": 10000},
    "confidence": {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 1},
    "minimum_token_gain": {"type": "number", "minimum": 0, "maximum": 1},
    "minimum_quality_delta": {"type": "number", "minimum": -1, "maximum": 1},
    "model": SHORT, "provider": SHORT, "client_version": SHORT,
    "root_policy": FILE_REF, "child_policy": FILE_REF,
    "isolation": FILE_REF, "approved_budget": FILE_REF,
    "suites": {"type": "array", "minItems": 5, "maxItems": 5, "items": SUITE}},
    ("schema_version", "record_type", "comparison", "seed", "repeats", "bootstrap_resamples", "confidence",
     "minimum_token_gain", "minimum_quality_delta",
     "model", "provider", "client_version", "root_policy", "child_policy", "isolation", "approved_budget", "suites"))
USAGE = object_schema({"invocation_id": SHORT,
    "scope": {"const": "individual_attempt"},
    "phase": {"enum": ["initial", "planner", "worker", "integration", "reviewer", "repair", "retry"]},
    "status": {"enum": ["complete", "partial", "unavailable", "conflicting"]},
    "input_tokens": {"type": ["integer", "null"], "minimum": 0},
    "output_tokens": {"type": ["integer", "null"], "minimum": 0}},
    ("invocation_id", "scope", "phase", "status", "input_tokens", "output_tokens"))
TRIAL = object_schema({"schema_version": {"const": 1}, "manifest_digest": HASH,
    "measurement_kind": {"enum": ["synthetic", "observed"]},
    "trial_id": HASH, "outcome": {"enum": ["accepted", "failed", "cancelled", "timeout", "environment_error"]},
    "elapsed_seconds": {"type": "number", "minimum": 0},
    "usage_coverage": {"enum": ["complete", "partial", "unknown"]},
    "usage": {"type": "array", "maxItems": 10000, "items": USAGE},
    "claimed_accepted": {"type": "boolean"},
    "mandatory_nodes_skipped": {"type": "integer", "minimum": 0},
    "duplicate_writes": {"type": "integer", "minimum": 0},
    "recovery": {"enum": ["not_applicable", "succeeded", "failed", "unknown"]},
    "time_to_accepted_seconds": {"type": ["number", "null"], "minimum": 0},
    "useful_retrieval": {"type": ["boolean", "null"]},
    "stale_reuse": {"type": "integer", "minimum": 0},
    "hook_overhead_ms": {"type": ["number", "null"], "minimum": 0},
    "evaluator_receipt": FILE_REF},
    ("schema_version", "manifest_digest", "measurement_kind", "trial_id", "outcome", "elapsed_seconds", "usage_coverage", "usage",
     "claimed_accepted", "mandatory_nodes_skipped", "duplicate_writes", "recovery", "time_to_accepted_seconds",
     "useful_retrieval", "stale_reuse", "hook_overhead_ms", "evaluator_receipt"))
TRIAL_V1 = TRIAL
TRIAL_V2 = benchmark_metrics.trial_schema(TRIAL_V1)
TRIAL = {"oneOf": [TRIAL_V1, TRIAL_V2]}
GRADE = object_schema({"schema_version": {"const": 1}, "record_type": {"const": "independent_grade"},
    "manifest_digest": HASH, "trial_id": HASH, "evaluator_digest": HASH,
    "outcome": {"enum": ["accepted", "failed", "cancelled", "timeout", "environment_error"]}},
    ("schema_version", "record_type", "manifest_digest", "trial_id", "evaluator_digest", "outcome"))


# Version axes are independent. The v1 GRADE bytes above remain frozen.
from copy import deepcopy
from datetime import datetime, timezone
import os
import sys
from .core import canonical, snapshot
from .supervisor_store import atomic_write, _ordinary


def _closed(properties):
    return object_schema(properties, tuple(properties))


def _record(kind, properties, version=1):
    return _closed({'schema_version': {'const': version}, 'record_type': {'const': kind}, **properties})


def _array(items, maximum=10000):
    return {'type': 'array', 'maxItems': maximum, 'items': items}


UTC = {'type': 'string', 'format': 'utc'}
CANDIDATE_SCHEME = 'ordinary-tree-no-git-v1'
PROJECTION_FILE = _closed({'path': SHORT, 'sha256': HASH, 'bytes': {'type': 'integer', 'minimum': 0},
    'mode': {'enum': ['100644', '100755']}})
CANDIDATE_PROJECTION = _record('benchmark_candidate_projection', {'scheme': {'const': CANDIDATE_SCHEME},
    'files': _array(PROJECTION_FILE)})
PHYSICAL_CAPTURE = _closed({'root': SHORT, 'snapshot_digest': HASH, 'snapshot_scheme': {'const': 2}})
CANDIDATE_CAPTURE = _record('benchmark_candidate_capture', {'candidate_digest': HASH,
    'candidate_scheme': {'const': CANDIDATE_SCHEME}, 'projection': CANDIDATE_PROJECTION, 'physical': PHYSICAL_CAPTURE})
CANDIDATE_TRANSFER = _record('benchmark_candidate_transfer', {'declared_capture': FILE_REF,
    'destination': CANDIDATE_CAPTURE})
GRADE_SUBJECT = _closed({'subject_version': {'const': 1}, 'manifest_digest': HASH, 'task_id': SHORT, 'trial_id': HASH,
    'candidate_digest': HASH, 'candidate_scheme': {'const': CANDIDATE_SCHEME},
    'evaluator_digest': HASH, 'evaluator_artifact_digest': HASH, 'bundle_digest': HASH, 'toolchain_digest': HASH})
REQUIREMENT_RESULT = _closed({'id': SHORT, 'status': {'enum': ['passed', 'failed', 'unknown', 'not_run']}})
RUNTIME = _closed({'python': SHORT, 'executable_sha256': HASH, 'harness_build_digest': HASH})
GRADE_INPUT_V2 = _record('benchmark_grade_input', {'subject': GRADE_SUBJECT,
    'manifest_digest': HASH, 'trial_id': HASH, 'evaluator_digest': HASH,
    'candidate_capture': FILE_REF, 'evaluator': FILE_REF, 'toolchain': FILE_REF, 'runtime': RUNTIME,
    'solve_digest': HASH, 'cwd': SHORT, 'evidence_root': SHORT,
    'solve_outcome': {'enum': ['completed', 'failed', 'cancelled', 'timeout', 'environment_error', 'unknown']},
    'created_at': UTC, 'requirement_ids': _array(SHORT, 512)}, version=2)
INPUT_ARTIFACT = _closed({'role': SHORT, 'logical_path': SHORT, 'asset': FILE_REF,
    'bytes': {'type': 'integer', 'minimum': 0}, 'mode': {'enum': ['100644', '100755']}})
EVALUATOR_INPUT = _record('benchmark_evaluator_input', {'subject': GRADE_SUBJECT,
    'request': FILE_REF, 'candidate_capture': FILE_REF, 'transfer': FILE_REF,
    'materialization': FILE_REF, 'artifacts': _array(INPUT_ARTIFACT), 'created_at': UTC})
EVALUATOR_EXECUTION = _record('benchmark_evaluator_execution', {'subject': GRADE_SUBJECT,
    'evaluator_input_digest': HASH, 'evaluator_input': FILE_REF,
    'outcome': GRADE['properties']['outcome'], 'requirements': _array(REQUIREMENT_RESULT, 512), 'started_at': UTC, 'finished_at': UTC,
    'command': _array(SHORT, 256), 'returncode': {'type': ['integer', 'null']}})
EXECUTION_GRADE = _record('independent_grade', {'subject': GRADE_SUBJECT,
    'evaluator_input_digest': HASH, 'evaluator_input': FILE_REF, 'execution': FILE_REF,
    'outcome': GRADE['properties']['outcome'], 'requirements': _array(REQUIREMENT_RESULT, 512), 'started_at': UTC, 'finished_at': UTC}, version=2)
GRADE_SCHEMA = {'oneOf': [GRADE, EXECUTION_GRADE]}


# Two telemetry contracts already exist: flat legacy counters (v1) and
# source-bound metric receipts (v2). Their chain-bound counterparts are v3/v4;
# neither is an intermediate revision of the other. Preserve both shapes and
# validate v2 metric receipts after removing only the new grading envelope.
TRIAL_V3 = deepcopy(TRIAL_V1)
TRIAL_V4 = deepcopy(TRIAL_V2)
for version, schema in ((3, TRIAL_V3), (4, TRIAL_V4)):
    schema['properties'].update(schema_version={'const': version}, grade_subject=GRADE_SUBJECT, evaluator_input_digest=HASH)
    schema['required'].extend(['grade_subject', 'evaluator_input_digest'])
TRIAL = {'oneOf': [TRIAL_V1, TRIAL_V2, TRIAL_V3, TRIAL_V4]}


def _stamp():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _instant(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def _portable(name):
    path = benchmark_metrics.portable_relative_path(name)
    if path.as_posix() != name:
        raise HarnessError('BENCH_NONCANONICAL_CANDIDATE_PATH')
    return path


def candidate_capture(root):
    """Portable ordinary content and a separate physical core snapshot v2 pin.

    Includes ignored files; only .git administration is excluded. Never treats
    core.content_digest (which includes cwd) as a portable content projection.
    """
    root = safe_private(Path(root).absolute())
    def capture():
        files, total, names = [], 0, set()
        for directory, dirs, leaves in os.walk(root, followlinks=False):
            base = Path(directory)
            _ordinary(base, directory=True)
            dirs[:] = [name for name in dirs if name != '.git']
            for name in dirs: _ordinary(base/name, directory=True)
            for name in sorted(leaves):
                if name == '.git': continue
                path = base/name; metadata = _ordinary(path)
                relative = path.relative_to(root).as_posix(); _portable(relative)
                if relative.casefold() in names: raise HarnessError('BENCH_CANDIDATE_PATH_COLLISION')
                names.add(relative.casefold())
                raw = read_regular_file(path, max_bytes=64*1024*1024)
                total += len(raw)
                if len(files) >= 10000 or total > 64*1024*1024:
                    raise HarnessError('BENCH_CANDIDATE_BOUND')
                files.append({'path': relative, 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw),
                    'mode': '100755' if metadata.st_mode & 0o111 else '100644'})
        return {'schema_version': 1, 'record_type': 'benchmark_candidate_projection',
            'scheme': CANDIDATE_SCHEME, 'files': sorted(files, key=lambda row: row['path'])}
    before = capture()
    physical = snapshot({'cwd': str(root)})
    if capture() != before: raise HarnessError('BENCH_CANDIDATE_DRIFT')
    value = {'schema_version': 1, 'record_type': 'benchmark_candidate_capture',
        'candidate_digest': digest(before), 'candidate_scheme': CANDIDATE_SCHEME, 'projection': before,
        'physical': {'root': str(root), 'snapshot_digest': physical['digest'], 'snapshot_scheme': 2}}
    validate(value, CANDIDATE_CAPTURE)
    return value


def _check_capture(value):
    validate(value, CANDIDATE_CAPTURE)
    names = [row['path'] for row in value['projection']['files']]
    for name in names: _portable(name)
    if (len(names) != len(set(name.casefold() for name in names)) or names != sorted(names)
            or sum(row['bytes'] for row in value['projection']['files']) > 64*1024*1024
            or digest(value['projection']) != value['candidate_digest']):
        raise HarnessError('BENCH_CANDIDATE_PROJECTION_INTEGRITY')
    return value


def _evidence_bytes(ref, root, maximum=64*1024*1024):
    validate(ref, FILE_REF)
    raw = read_regular_file(safe_private(Path(root).absolute()/_portable(ref['path'])), max_bytes=maximum)
    if hashlib.sha256(raw).hexdigest() != ref['sha256']: raise HarnessError('BENCH_GRADE_EVIDENCE_CHANGED')
    return raw


def _evidence_record(ref, root, schema):
    return loads(_evidence_bytes(ref, root), schema)


def put_grade_evidence(root, name, value, schema=None):
    if schema is not None: validate(value, schema)
    raw = value if isinstance(value, bytes) else canonical(value)
    if schema is not None and len(raw) > 2*1024*1024:
        raise HarnessError('BENCH_GRADE_EVIDENCE_BOUND')
    path = safe_private(Path(root).absolute()/_portable(name))
    if path.exists():
        if read_regular_file(path, max_bytes=64*1024*1024) != raw:
            raise HarnessError('BENCH_GRADE_EVIDENCE_COLLISION')
    else: atomic_write(path, raw, exclusive=True)
    return {'path': name, 'sha256': hashlib.sha256(raw).hexdigest()}


def _blob(root, raw):
    return put_grade_evidence(root, 'grading-blobs/'+hashlib.sha256(raw).hexdigest(), raw)


def grading_runtime():
    from .execution_receipts import build_digest
    return {'python': sys.version, 'executable_sha256': hashlib.sha256(read_regular_file(Path(sys.executable).resolve(), max_bytes=64*1024*1024)).hexdigest(),
        'harness_build_digest': build_digest()}


def write_grade_input(path, *, evidence_root, manifest_digest, task_id, trial_id, source_root,
                      evaluator_bytes, toolchain_bytes, bundle_digest, solve_digest, solve_outcome, evaluator_digest=None, requirement_ids=()):
    """Capture actual candidate before dispatch; caller cannot supply its digest."""
    evidence_root = safe_private(Path(evidence_root).absolute()); path = Path(path).absolute()
    capture = candidate_capture(source_root)
    candidate = put_grade_evidence(evidence_root, 'grading/'+trial_id+'/candidate.json', capture, CANDIDATE_CAPTURE)
    evaluator = _blob(evidence_root, evaluator_bytes); toolchain = _blob(evidence_root, toolchain_bytes)
    runtime = grading_runtime()
    subject = {'subject_version': 1, 'manifest_digest': manifest_digest, 'task_id': task_id, 'trial_id': trial_id,
        'candidate_digest': capture['candidate_digest'], 'candidate_scheme': CANDIDATE_SCHEME,
        'evaluator_digest': evaluator_digest or evaluator['sha256'], 'evaluator_artifact_digest': evaluator['sha256'], 'bundle_digest': bundle_digest,
        'toolchain_digest': digest({'runtime': runtime, 'declared_sha256': toolchain['sha256']})}
    value = {'schema_version': 2, 'record_type': 'benchmark_grade_input', 'subject': subject,
        **{key: subject[key] for key in ('manifest_digest', 'trial_id', 'evaluator_digest')},
        'candidate_capture': candidate, 'evaluator': evaluator, 'toolchain': toolchain, 'runtime': runtime,
        'solve_digest': solve_digest, 'solve_outcome': solve_outcome, 'cwd': str(Path(source_root).absolute()),
        'evidence_root': str(evidence_root), 'created_at': _stamp(), 'requirement_ids': list(requirement_ids)}
    put_grade_evidence(evidence_root, path.relative_to(evidence_root).as_posix(), value, GRADE_INPUT_V2)
    return value


def _check_request(value, root):
    validate(value, GRADE_INPUT_V2)
    capture = _check_capture(_evidence_record(value['candidate_capture'], root, CANDIDATE_CAPTURE))
    subject = value['subject']
    if len(value['requirement_ids']) != len(set(value['requirement_ids'])):
        raise HarnessError('BENCH_GRADE_REQUIREMENT_BINDING')
    if any(value[key] != subject[key] for key in ('manifest_digest', 'trial_id', 'evaluator_digest')):
        raise HarnessError('BENCH_GRADE_INPUT_BINDING')
    if any(subject[key] != capture[key] for key in ('candidate_digest', 'candidate_scheme')):
        raise HarnessError('BENCH_GRADE_CANDIDATE_BINDING')
    _evidence_bytes(value['evaluator'], root); _evidence_bytes(value['toolchain'], root)
    if (subject['evaluator_artifact_digest'] != value['evaluator']['sha256'] or subject['toolchain_digest'] !=
            digest({'runtime': value['runtime'], 'declared_sha256': value['toolchain']['sha256']})):
        raise HarnessError('BENCH_GRADE_TOOLCHAIN_BINDING')
    if value['cwd'] != capture['physical']['root']:
        raise HarnessError('BENCH_GRADE_CANDIDATE_BINDING')
    return capture


def transfer_candidate(request, destination):
    root = request['evidence_root']; capture = _check_request(request, root)
    actual = candidate_capture(destination)
    if actual['projection'] != capture['projection'] or actual['candidate_digest'] != request['subject']['candidate_digest']:
        raise HarnessError('BENCH_CANDIDATE_TRANSFER_MISMATCH')
    transfer = {'schema_version': 1, 'record_type': 'benchmark_candidate_transfer',
        'declared_capture': request['candidate_capture'], 'destination': actual}
    return put_grade_evidence(root, 'grading/'+request['subject']['trial_id']+'/transfer.json', transfer, CANDIDATE_TRANSFER)


def begin_evaluation(grade_input, materialization, *, bundle_digest, task_id, manifest_digest, trial_id,
                     artifact_roots, transfer=None):
    raw = read_regular_file(grade_input, max_bytes=2*1024*1024)
    request = loads(raw, GRADE_INPUT_V2); root = request['evidence_root']; subject = request['subject']
    capture = _check_request(request, root)
    if (any(subject[key] != expected for key, expected in (('bundle_digest', bundle_digest), ('task_id', task_id),
            ('manifest_digest', manifest_digest), ('trial_id', trial_id))) or request['runtime'] != grading_runtime()):
        raise HarnessError('BENCH_EVALUATOR_SUBJECT_MISMATCH')
    if candidate_capture(request['cwd']) != capture: raise HarnessError('BENCH_CANDIDATE_DRIFT')
    transfer = transfer or transfer_candidate(request, request['cwd'])
    artifacts, live_inputs, total = [], [], 0
    for role, directory in artifact_roots:
        projection = candidate_capture(directory)['projection']
        for row in projection['files']:
            file_raw = read_regular_file(Path(directory)/_portable(row['path']), max_bytes=64*1024*1024)
            if hashlib.sha256(file_raw).hexdigest() != row['sha256']: raise HarnessError('BENCH_EVALUATOR_INPUT_DRIFT')
            total += len(file_raw)
            if len(artifacts) >= 10000 or total > 64*1024*1024: raise HarnessError('BENCH_EVALUATOR_INPUT_BOUND')
            live_inputs.append((str(Path(directory)/_portable(row['path'])), row['sha256'], row['mode']))
            artifacts.append({'role': role, 'logical_path': row['path'], 'asset': _blob(root, file_raw),
                'bytes': len(file_raw), 'mode': row['mode']})
    value = {'schema_version': 1, 'record_type': 'benchmark_evaluator_input', 'subject': subject,
        'request': _blob(root, raw), 'candidate_capture': request['candidate_capture'], 'transfer': transfer,
        'materialization': _blob(root, read_regular_file(materialization)), 'artifacts': artifacts, 'created_at': _stamp()}
    ref = put_grade_evidence(root, 'grading/'+trial_id+'/actual-input.json', value, EVALUATOR_INPUT)
    return {'request': request, 'request_path': str(grade_input), 'request_raw': raw, 'input': value,
        'input_ref': ref, 'input_digest': digest(value), 'started_at': _stamp(), 'live_inputs': live_inputs,
        'materialization_path': str(materialization)}


def finish_evaluation(context, *, outcome, command, returncode):
    request = context['request']; root = request['evidence_root']
    capture = _check_request(request, root)
    if (read_regular_file(context['request_path']) != context['request_raw']
            or candidate_capture(request['cwd']) != capture):
        raise HarnessError('BENCH_CANDIDATE_DRIFT')
    if read_regular_file(context['materialization_path']) != _evidence_bytes(context['input']['materialization'], root):
        raise HarnessError('BENCH_EVALUATOR_INPUT_DRIFT')
    for name, sha256, mode in context['live_inputs']:
        path = Path(name)
        if (hashlib.sha256(read_regular_file(path, max_bytes=64*1024*1024)).hexdigest() != sha256
                or ('100755' if _ordinary(path).st_mode & 0o111 else '100644') != mode):
            raise HarnessError('BENCH_EVALUATOR_INPUT_DRIFT')
    execution = {'schema_version': 1, 'record_type': 'benchmark_evaluator_execution', 'subject': request['subject'],
        'evaluator_input_digest': context['input_digest'], 'evaluator_input': context['input_ref'],
        'outcome': outcome, 'command': command, 'returncode': returncode,
        'requirements': [{'id': name, 'status': 'passed' if outcome == 'accepted' else 'failed'} for name in request['requirement_ids']],
        'started_at': context['started_at'], 'finished_at': _stamp()}
    ref = put_grade_evidence(root, 'grading/'+request['subject']['trial_id']+'/execution.json', execution, EVALUATOR_EXECUTION)
    grade = {'schema_version': 2, 'record_type': 'independent_grade', 'subject': request['subject'],
        'evaluator_input_digest': context['input_digest'], 'evaluator_input': context['input_ref'],
        'execution': ref, 'outcome': outcome, 'requirements': execution['requirements'], 'started_at': execution['started_at'], 'finished_at': execution['finished_at']}
    verify_execution_grade(grade, root)
    return grade


def verify_execution_grade(grade, root, *, expected_subject=None, expected_input_ref=None):
    """Read-only whole-chain verification; hashes bind declarations, not identity."""
    validate(grade, GRADE_SCHEMA)
    if grade['schema_version'] == 1: return 'unverified_legacy'
    subject = grade['subject']
    if expected_subject is not None and subject != expected_subject: raise HarnessError('BENCH_GRADE_BINDING_MISMATCH')
    inputs = _evidence_record(grade['evaluator_input'], root, EVALUATOR_INPUT)
    if expected_input_ref is not None:
        _evidence_bytes(expected_input_ref, root)
        if inputs['request']['sha256'] != expected_input_ref['sha256']:
            raise HarnessError('BENCH_GRADE_INPUT_BINDING')
    execution = _evidence_record(grade['execution'], root, EVALUATOR_EXECUTION)
    request = _evidence_record(inputs['request'], root, GRADE_INPUT_V2)
    _check_request(request, root)
    if (inputs['subject'] != subject or execution['subject'] != subject or request['subject'] != subject
            or digest(inputs) != grade['evaluator_input_digest']
            or execution['evaluator_input_digest'] != grade['evaluator_input_digest']
            or execution['evaluator_input'] != grade['evaluator_input']
            or inputs['candidate_capture'] != request['candidate_capture']):
        raise HarnessError('BENCH_GRADE_INPUT_BINDING')
    transfer = _evidence_record(inputs['transfer'], root, CANDIDATE_TRANSFER)
    destination = _check_capture(transfer['destination'])
    capture = _check_capture(_evidence_record(request['candidate_capture'], root, CANDIDATE_CAPTURE))
    if transfer['declared_capture'] != request['candidate_capture'] or destination['projection'] != capture['projection']:
        raise HarnessError('BENCH_CANDIDATE_TRANSFER_MISMATCH')
    materialized = loads(_evidence_bytes(inputs['materialization'], root))
    if not isinstance(materialized, dict): raise HarnessError('BENCH_GRADE_MATERIALIZATION_BINDING')
    if materialized.get('record_type') == 'materialized_task_bundle':
        from .benchmark_bundles import MATERIALIZED
    elif materialized.get('record_type') == 'benchmark_upstream_materialized':
        from .benchmark_importers import MATERIALIZED
    else: raise HarnessError('BENCH_GRADE_MATERIALIZATION_BINDING')
    validate(materialized, MATERIALIZED)
    if (materialized.get('bundle_digest', materialized.get('plan_digest')) != subject['bundle_digest']
            or any((materialized.get('plan', {}).get(key) if key == 'task_id' and 'plan' in materialized else materialized.get(key)) != subject[key] for key in ('task_id', 'trial_id', 'manifest_digest'))):
        raise HarnessError('BENCH_GRADE_MATERIALIZATION_BINDING')
    if ([row['id'] for row in grade['requirements']] != request['requirement_ids']
            or grade['outcome'] == 'accepted' and any(row['status'] != 'passed' for row in grade['requirements'])):
        raise HarnessError('BENCH_GRADE_REQUIREMENT_BINDING')
    names, total = set(), 0
    for artifact in inputs['artifacts']:
        name = (artifact['role'], artifact['logical_path'].casefold()); _portable(artifact['logical_path'])
        if name in names: raise HarnessError('BENCH_EVALUATOR_INPUT_COLLISION')
        names.add(name)
        raw = _evidence_bytes(artifact['asset'], root); total += len(raw)
        if len(raw) != artifact['bytes'] or total > 64*1024*1024: raise HarnessError('BENCH_EVALUATOR_INPUT_BOUND')
    candidate_files = [{'path': item['logical_path'], 'sha256': item['asset']['sha256'],
        'bytes': item['bytes'], 'mode': item['mode']} for item in inputs['artifacts'] if item['role'] == 'candidate']
    protected_files = {item['logical_path']: item['asset']['sha256'] for item in inputs['artifacts'] if item['role'] == 'protected'}
    if (sorted(candidate_files, key=lambda row: row['path']) != capture['projection']['files']
            or protected_files != {item['path']: item['sha256'] for item in materialized.get('protected_files', [])}):
        raise HarnessError('BENCH_GRADE_ACTUAL_INPUT_BINDING')
    if grade['outcome'] == 'accepted' and (execution['returncode'] != 0 or not execution['command']):
        raise HarnessError('BENCH_GRADE_EXECUTION_BINDING')
    if (any(grade[key] != execution[key] for key in ('outcome', 'requirements', 'started_at', 'finished_at'))
            or _instant(request['created_at']) > _instant(inputs['created_at'])
            or _instant(inputs['created_at']) > _instant(grade['started_at'])
            or _instant(grade['started_at']) > _instant(grade['finished_at'])
            or request['solve_outcome'] in ('cancelled', 'timeout', 'environment_error') and grade['outcome'] != request['solve_outcome']):
        raise HarnessError('BENCH_GRADE_EXECUTION_BINDING')
    return 'verified_subject_chain'


def grade_identity(grade):
    return grade if grade['schema_version'] == 1 else grade['subject']


def checked_ref(ref, root):
    """Resolve only ordinary local assets inside the approved bundle root."""
    try:
        path = benchmark_metrics.portable_relative_path(ref["path"])
    except ValueError as error:
        raise HarnessError("BENCH_UNSAFE_ASSET: portable relative asset refs required") from error
    target = safe_private(Path(root).absolute() / path)
    raw = read_regular_file(target, max_bytes=2 * 1024 * 1024)
    if hashlib.sha256(raw).hexdigest() != ref["sha256"]:
        raise HarnessError("BENCH_STALE_ASSET: asset digest differs")
    return raw


def validate_manifest(manifest, root):
    validate(manifest, MANIFEST_SCHEMA)
    if {suite["id"] for suite in manifest["suites"]} != set(SUITES):
        raise HarnessError("BENCH_INCOMPLETE_PORTFOLIO: exactly five distinct suites required")
    for field in ("model", "provider", "client_version"):
        if manifest[field].casefold() in {"installed", "unknown", "unavailable", "tbd", ""}:
            raise HarnessError("BENCH_UNKNOWN_IDENTITY: " + field)
    for field in ("root_policy", "child_policy", "isolation", "approved_budget"):
        checked_ref(manifest[field], root)
    for suite in manifest["suites"]:
        if suite["revision"].casefold() in {"latest", "main", "head", "tbd", "unknown", ""}:
            raise HarnessError("BENCH_UNPINNED_REVISION: immutable revision required")
        for field in ("assets", "evaluator", "adapter", "toolchain"):
            checked_ref(suite[field], root)
        identifiers = [task["id"] for task in suite["tasks"]]
        if len(set(identifiers)) != len(identifiers):
            raise HarnessError("BENCH_DUPLICATE_TASK")
        for task in suite["tasks"]:
            checked_ref(task["source"], root)
    return {"schema_version": 1, "status": "assets_verified", "manifest_digest": digest(manifest),
            "planned_trials": sum(len(suite["tasks"]) for suite in manifest["suites"]) * manifest["repeats"] * 2,
            "model_runs": "not_run", "qualification": "local_bytes_only",
            "limitations": ["asset hashes do not prove full official cohort membership",
                            "isolation and budget receipt content require separate operational qualification"]}


def schedule(manifest):
    """Counterbalance each paired task; stable IDs retain failures in denominator."""
    validate(manifest, MANIFEST_SCHEMA)
    seed = random.Random(manifest["seed"])
    fingerprint = digest(manifest)
    pairs = [(suite["id"], task["id"], repeat)
             for suite in manifest["suites"] for task in suite["tasks"]
             for repeat in range(manifest["repeats"])]
    seed.shuffle(pairs)
    trials = []
    for suite, task, repeat in pairs:
        arms = list(ARMS[manifest["comparison"]])
        if seed.getrandbits(1):
            arms.reverse()
        for arm in arms:
            row = {"suite": suite, "task_id": task, "repeat": repeat, "arm": arm}
            trials.append({**row, "trial_id": digest({"manifest_digest": fingerprint, **row})})
    return trials


def summarize(manifest, trials, root):
    """Report every planned trial, including missing/cancelled/failed observations."""
    planned = schedule(manifest)
    by_id = {row["trial_id"]: row for row in planned}
    records, invocation_ids, grade_bindings, trial_inputs = {}, set(), {}, {}
    for trial in trials:
        validate(trial, TRIAL)
        original_trial = trial
        expected_subject = trial.get('grade_subject')
        expected_input = trial.get('evaluator_input_digest')
        if trial['schema_version'] in (3, 4):
            trial = {key: value for key, value in trial.items() if key not in ('grade_subject', 'evaluator_input_digest')}
            trial['schema_version'] -= 2
        if trial["schema_version"] == 2:
            benchmark_metrics.validate_measurements({name: trial[name] for name in ("schema_version", "metrics", "phase_seconds")}, root, trial_id=trial["trial_id"], manifest_digest=trial["manifest_digest"])
        key = trial["trial_id"]
        if key not in by_id or trial["manifest_digest"] != digest(manifest):
            raise HarnessError("BENCH_TRIAL_BINDING_MISMATCH")
        if key in records:
            if trial_inputs[key] != original_trial:
                raise HarnessError("BENCH_CONFLICTING_TRIAL")
            continue
        if (trial["outcome"] != "accepted" and trial["time_to_accepted_seconds"] is not None
                or trial["time_to_accepted_seconds"] is not None
                and trial["time_to_accepted_seconds"] > trial["elapsed_seconds"]):
            raise HarnessError("BENCH_INVALID_TIME_TO_ACCEPTANCE")
        grade = loads(checked_ref(trial["evaluator_receipt"], root), GRADE_SCHEMA)
        binding_status = verify_execution_grade(grade, root, expected_subject=expected_subject)
        if grade["schema_version"] == 2 and (expected_subject is None or expected_input != grade["evaluator_input_digest"]):
            raise HarnessError("BENCH_TRIAL_GRADE_CHAIN_BINDING")
        identity = grade_identity(grade)
        suite = next(suite for suite in manifest["suites"] if suite["id"] == by_id[key]["suite"])
        if (identity["manifest_digest"] != trial["manifest_digest"] or identity["trial_id"] != key
                or identity["evaluator_digest"] != suite["evaluator"]["sha256"] or grade["outcome"] != trial["outcome"]):
            raise HarnessError("BENCH_GRADE_BINDING_MISMATCH")
        if grade['schema_version'] == 2:
            inputs = _evidence_record(grade['evaluator_input'], root, EVALUATOR_INPUT)
            request = _evidence_record(inputs['request'], root, GRADE_INPUT_V2)
            materialized = loads(_evidence_bytes(inputs['materialization'], root))
            source = next(task['source'] for task in suite['tasks'] if task['id'] == by_id[key]['task_id'])
            if (identity['task_id'] != by_id[key]['task_id'] or request['toolchain']['sha256'] != suite['toolchain']['sha256']
                    or materialized.get('bundle_ref', materialized.get('plan', {}).get('source_ref')) != source):
                raise HarnessError('BENCH_TRIAL_GRADE_CHAIN_BINDING')
        for usage in trial["usage"]:
            if usage["invocation_id"] in invocation_ids:
                raise HarnessError("BENCH_DUPLICATE_INVOCATION: overlapping coverage")
            invocation_ids.add(usage["invocation_id"])
        records[key] = trial
        grade_bindings[key] = binding_status
        trial_inputs[key] = original_trial
    arms = ARMS[manifest["comparison"]]
    output = {"schema_version": 1, "manifest_digest": digest(manifest),
              "status": "not_run" if not records else "complete" if len(records) == len(planned) else "insufficient_data",
              "measurement_kind": "synthetic" if any(row["measurement_kind"] == "synthetic" for row in records.values()) else "observed" if records else "unavailable",
              "comparison": manifest["comparison"], "suites": [],
              "grade_binding": {"verified_subject_chain": sum(value == "verified_subject_chain" for value in grade_bindings.values()),
                                "unverified_legacy": sum(value == "unverified_legacy" for value in grade_bindings.values())}}
    for suite in manifest["suites"]:
        suite_plan = [row for row in planned if row["suite"] == suite["id"]]
        summaries = {}
        for arm in arms:
            selected = [records[row["trial_id"]] for row in suite_plan
                        if row["arm"] == arm and row["trial_id"] in records]
            expected = sum(row["arm"] == arm for row in suite_plan)
            complete = len(selected) == expected and all(_complete(row) for row in selected)
            summaries[arm] = {"planned": expected, "observed": len(selected), "not_run": expected - len(selected),
                "accepted": sum(row["outcome"] == "accepted" for row in selected),
                "failed_or_cancelled": sum(row["outcome"] != "accepted" for row in selected),
                "false_acceptance": sum(row["claimed_accepted"] and row["outcome"] != "accepted" for row in selected),
                "mandatory_nodes_skipped": _metric_sum(selected, "mandatory_nodes_skipped"),
                "duplicate_writes": _metric_sum(selected, "duplicate_writes"),
                "recovery_succeeded": sum(benchmark_metrics.metric_value(row, "recovery") == "succeeded" and not benchmark_metrics.metric_unknown(row, "recovery") for row in selected),
                "recovery_failed": sum(benchmark_metrics.metric_value(row, "recovery") == "failed" and not benchmark_metrics.metric_unknown(row, "recovery") for row in selected),
                "recovery_unknown": sum(benchmark_metrics.metric_unknown(row, "recovery") or benchmark_metrics.metric_value(row, "recovery") == "unknown" for row in selected),
                "recovery_partial": sum(row["schema_version"] == 2 and row["metrics"]["recovery"]["coverage"] == "partial" for row in selected),
                "observed_model_calls": sum(len(row["usage"]) for row in selected),
                "observed_retries_repairs": sum(usage["phase"] in {"repair", "retry"} for row in selected for usage in row["usage"]),
                "useful_retrieval_true": sum(benchmark_metrics.metric_value(row, "useful_retrieval") is True for row in selected),
                "useful_retrieval_unknown": sum(benchmark_metrics.metric_unknown(row, "useful_retrieval") for row in selected),
                "stale_reuse": _metric_sum(selected, "stale_reuse"),
                "hook_overhead_ms": _metric_sum(selected, "hook_overhead_ms"),
                "hook_overhead_unknown": sum(benchmark_metrics.metric_unknown(row, "hook_overhead_ms") for row in selected),
                "time_to_accepted_seconds": [row["time_to_accepted_seconds"] for row in selected if row["outcome"] == "accepted"],
                "censored_or_unaccepted": sum(row["outcome"] != "accepted" for row in selected),
                "elapsed_seconds": sum(row["elapsed_seconds"] for row in selected),
                "usage_coverage": "complete" if complete else "partial",
                "observed_tokens": sum(value for row in selected for usage in row["usage"]
                                       for value in (usage["input_tokens"], usage["output_tokens"]) if value is not None)}
            summaries[arm].update({name + "_unknown": sum(benchmark_metrics.metric_unknown(row, name) for row in selected)
                                   for name in ("mandatory_nodes_skipped", "duplicate_writes", "stale_reuse", "manual_interventions")})
            summaries[arm]["manual_interventions"] = _metric_sum(selected, "manual_interventions")
            summaries[arm]["phase_seconds"] = {name: _phase_summary(selected, name) for name in benchmark_metrics.PHASES}
        paired, paired_report = _paired_cost_report(suite, suite_plan, records, arms, manifest["repeats"])
        interval = _interval(paired, manifest) if len(paired) >= 2 else None
        quality_delta = ((summaries[arms[1]]["accepted"] - summaries[arms[0]]["accepted"])
                         / summaries[arms[0]]["planned"])
        complete = all(row["usage_coverage"] == "complete" for row in summaries.values())
        verdict = "insufficient_data"
        if complete and interval is not None:
            verdict = ("measured_negative" if quality_delta < manifest["minimum_quality_delta"] or interval["upper"] < 0
                       else "measured_positive" if interval["lower"] >= manifest["minimum_token_gain"]
                       else "no_clear_effect")
        matched_unverified = manifest["comparison"] == "matched" and output["measurement_kind"] == "observed"
        if matched_unverified:
            # These receipt versions have no observed role-level setting map.
            # Equal policy-file hashes cannot qualify a causal effort match.
            verdict = "insufficient_data"
        output["suites"].append({"suite": suite["id"], "full_cohort": summaries,
            "quality_delta": quality_delta,
            "policy_result": verdict,
            "matched_effort_qualification": "not_verified" if matched_unverified else "not_applicable_to_observed_claim",
            "claim_scope": "matched_role_mapping_unavailable" if matched_unverified else "synthetic_accounting_only" if output["measurement_kind"] == "synthetic" else "supplied_qualified_observations_required",
            "accepted_pairs": {**paired_report, "task_clusters": len(paired),
                "median_relative_token_saving": statistics.median(paired) if paired else None,
                "selection": "accepted_in_both_arms_with_complete_usage",
                "uncertainty": "not_estimable" if interval is None else interval}})
    output["limitations"] = ["independent grading and isolation require qualified producers",
                             "no aggregate score across different suites",
                             "sample byte savings are not task token savings"]
    return output


def _metric_sum(rows, name):
    values = [benchmark_metrics.metric_value(row, name) for row in rows]
    known = [value for value in values if value is not None]
    return sum(known) if known or not rows else None


def _phase_summary(rows, name):
    values = [row["phase_seconds"][name] for row in rows if row["schema_version"] == 2]
    known = [item["value"] for item in values if item["value"] is not None]
    return {"observed_seconds": sum(known) if known else None,
            "observed_trials": sum(item["coverage"] == "observed" for item in values),
            "partial_trials": sum(item["coverage"] == "partial" for item in values),
            "unavailable_trials": len(rows) - len(values) + sum(item["coverage"] == "unavailable" for item in values)}


def _paired_cost_report(suite, planned, records, arms, repeats):
    indexed = {(row["task_id"], row["repeat"], row["arm"]): records.get(row["trial_id"])
               for row in planned}
    accepted_count = complete_count = 0
    complete_totals = [0, 0]
    selected = {arm: [] for arm in arms}
    per_task, clusters = [], []
    for task in suite["tasks"]:
        distribution, differences = [], []
        for repeat in range(repeats):
            pair = [indexed[(task["id"], repeat, arm)] for arm in arms]
            totals = [_tokens(row) if row and _complete(row) else None for row in pair]
            accepted = all(row and row["outcome"] == "accepted" for row in pair)
            complete = accepted and all(value is not None for value in totals)
            if accepted:
                accepted_count += 1
                for arm, row in zip(arms, pair):
                    selected[arm].append(row)
            if complete:
                complete_count += 1
                for index, value in enumerate(totals):
                    complete_totals[index] += value
            status = ("not_run" if any(row is None for row in pair) else
                      "not_accepted_in_both" if not accepted else
                      "incomplete_usage" if not complete else
                      "zero_baseline" if totals[0] == 0 else "estimable")
            relative = (totals[0] - totals[1]) / totals[0] if status == "estimable" else None
            if relative is not None:
                differences.append(relative)
            distribution.append({"repeat": repeat,
                "baseline_outcome": pair[0]["outcome"] if pair[0] else "not_run",
                "harness_outcome": pair[1]["outcome"] if pair[1] else "not_run",
                "baseline_tokens": totals[0], "harness_tokens": totals[1],
                "baseline_observed_tokens": _observed_tokens(pair[0]) if pair[0] else None,
                "harness_observed_tokens": _observed_tokens(pair[1]) if pair[1] else None,
                "baseline_usage_coverage": _coverage(pair[0]),
                "harness_usage_coverage": _coverage(pair[1]),
                "absolute_token_saving": totals[0] - totals[1] if complete else None,
                "relative_token_saving": relative, "relative_token_saving_status": status})
        mean = statistics.mean(differences) if differences else None
        if mean is not None:
            clusters.append(mean)
        per_task.append({"task_id": task["id"], "repeats": distribution,
                         "mean_relative_token_saving": mean})
    costs = {arm: {"observed_tokens": sum(_observed_tokens(row) for row in rows),
                   "elapsed_seconds": sum(row["elapsed_seconds"] for row in rows),
                   "usage_coverage": "unknown" if not rows or any(_coverage(row) == "unknown" for row in rows) else "complete" if all(_complete(row) for row in rows) else "partial",
                   "unknown_usage_trials": sum(_coverage(row) == "unknown" for row in rows),
                   "partial_usage_trials": sum(_coverage(row) == "partial" for row in rows)}
             for arm, rows in selected.items()}
    baseline, treatment = complete_totals
    return clusters, {"accepted_repeat_pairs": accepted_count, "complete_usage_repeat_pairs": complete_count,
        "cost_scope": "accepted_in_both_arms_including_partial_usage", "costs": costs,
        "complete_usage_costs": {"baseline_tokens": baseline, "harness_tokens": treatment,
            "absolute_token_saving": baseline - treatment,
            "relative_token_saving_of_totals": (baseline - treatment) / baseline if baseline > 0 else None,
            "relative_token_saving_status": "estimable" if baseline > 0 else "zero_baseline" if complete_count else "not_estimable"},
        "per_task": per_task}


def _observed_tokens(row):
    return sum(value for usage in row["usage"]
               for value in (usage["input_tokens"], usage["output_tokens"]) if value is not None)


def _coverage(row):
    if row is None or row["usage_coverage"] == "unknown":
        return "unknown"
    return "complete" if _complete(row) else "partial"


def _complete(row):
    return (row["usage_coverage"] == "complete" and bool(row["usage"])
            and all(usage["status"] == "complete" and type(usage["input_tokens"]) is int
                    and type(usage["output_tokens"]) is int for usage in row["usage"]))


def _tokens(row):
    return sum(usage["input_tokens"] + usage["output_tokens"] for usage in row["usage"])


def _interval(values, manifest):
    rng = random.Random(manifest["seed"])
    estimates = sorted(statistics.mean(rng.choices(values, k=len(values)))
                       for _ in range(manifest["bootstrap_resamples"]))
    alpha = (1 - manifest["confidence"]) / 2
    return {"method": "task_cluster_bootstrap_mean", "confidence": manifest["confidence"],
            "lower": estimates[int(alpha * len(estimates))],
            "upper": estimates[min(len(estimates)-1, int((1-alpha) * len(estimates)))]}


def dispatch(argv):
    if argv and argv[0] == "task-flow-experiment":
        from .task_flow_experiment import dispatch as task_flow_dispatch
        return task_flow_dispatch(argv[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("validate", "schedule", "report"))
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--assets-root", required=True)
    parser.add_argument("--trials", help="Strict YAML trial receipt array for report")
    parser.add_argument("--output", help="Exclusive Strict YAML output")
    args = parser.parse_args(argv)
    manifest = load(safe_private(args.manifest), MANIFEST_SCHEMA)
    readiness = validate_manifest(manifest, args.assets_root)
    if args.action == "report" and not args.trials:
        parser.error("report requires --trials")
    value = (readiness if args.action == "validate" else
             {**readiness, "trials": schedule(manifest)} if args.action == "schedule" else
             summarize(manifest, load(safe_private(args.trials)), args.assets_root))
    if args.output:
        dump(safe_private(args.output), value, exclusive=True)
    from .output import write_json
    write_json(value)
    return 0
