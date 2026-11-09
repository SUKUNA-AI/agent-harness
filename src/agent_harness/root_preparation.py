"""Journal-owned Supervisor root preparation and pre-intent reservations.

These records are controller operations, not execution receipts or model usage.
The ordinary core CLI and historical state/receipt schemas remain unchanged.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import ExitStack, contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

from .core import HarnessError, Run, canonical, digest, now, snapshot, file_hash
from .execution_ledger import object_schema, sanitized
from .failure_policy import PreparationFailure, PREPARATION_TARGETS, PREPARATION_CONTEXTS
from .file_io import ParentDirectory, read_regular_file, same_directory
from .strict_yaml import validate, MAX_BYTES
from .supervisor_store import atomic_write
from .agent_lifecycle import LEASE_SCHEMA

HASH = {"type": "string", "pattern": "^[a-f0-9]{64}$"}
TEXT = {"type": "string", "maxLength": 16384}
NULL_TEXT = {**TEXT, "type": ["string", "null"]}
NULL_HASH = {**HASH, "type": ["string", "null"]}
INTEGER = {"type": "integer", "minimum": 0}
STAMP = {"type": "array", "minItems": 7, "maxItems": 7, "items": INTEGER}
REF = object_schema({"path": TEXT, "sha256": HASH}, ("path", "sha256"))
BINDING_FIELDS = dict(run_id=TEXT, task_id=TEXT, mode={"enum": ["managed", "native"]},
    contract_digest=HASH, contract_path=TEXT, mission_digest=NULL_HASH,
    mission_generation={"type": ["integer", "null"], "minimum": 1},
    plan_digest=NULL_HASH, quality_assessment_ref=NULL_TEXT, source_cwd=TEXT, cwd=TEXT,
    source_snapshot_digest=HASH, deadline=TEXT, budgets_digest=HASH,
    max_attempts={"type": "integer", "minimum": 1}, attempts_before=INTEGER,
    core_path=TEXT, core_generation={"type": "integer", "minimum": 1},
    prior_core_state_digest=NULL_HASH, prior_core_contract_digest=NULL_HASH,
    failure_digest=NULL_HASH, purpose={"enum": ["initial", "repair", "revalidation"]}, harness_build_digest=HASH)
BINDING_SCHEMA = object_schema(BINDING_FIELDS, tuple(BINDING_FIELDS))
OPAQUE = {"type": "object", "additionalProperties": True}
DIRECTORY_IDENTITY = {"oneOf": [object_schema(dict(device=INTEGER, inode=INTEGER), ('device','inode')),
    object_schema(dict(identity_kind={"const":"windows-file-id-info-v1"},
        device={"type":"string","pattern":"^[a-f0-9]{16}$"}, inode={"type":"string","pattern":"^[a-f0-9]{32}$"}),
        ('identity_kind','device','inode'))]}
MATERIAL_FIELDS = dict(schema_version={"const": 1}, operation_id=HASH, binding=BINDING_SCHEMA,
    created_at={"type":"string","format":"utc"}, ownership_token={"type": "string", "pattern": "^[a-f0-9]{32}$"},
    parent_identity=DIRECTORY_IDENTITY, contract=OPAQUE, baseline=OPAQUE,
    prior_state={**OPAQUE, "type": ["object", "null"]}, expected_state=OPAQUE,
    origin_baseline_digest=NULL_HASH, publication_prior_ref=REF)
MATERIAL_SCHEMA = object_schema(MATERIAL_FIELDS, tuple(MATERIAL_FIELDS))
MATERIAL_HEADER_FIELDS = {k: v for k, v in MATERIAL_FIELDS.items()
    if k not in {'contract', 'baseline', 'prior_state', 'expected_state'}}
MATERIAL_HEADER_SCHEMA = object_schema(MATERIAL_HEADER_FIELDS, tuple(MATERIAL_HEADER_FIELDS))
WIRE_MATERIAL_FIELDS = {k: v for k, v in MATERIAL_FIELDS.items()
    if k not in {'contract', 'baseline', 'prior_state', 'expected_state'}}
WIRE_MATERIAL_FIELDS.update(contract_ref=REF, baseline_ref=REF, expected_state_ref=REF,
    prior_state_ref={**REF, "type": ["object", "null"]})
WIRE_MATERIAL_SCHEMA = object_schema(WIRE_MATERIAL_FIELDS, tuple(WIRE_MATERIAL_FIELDS))
from .native import MAX_RESPONSE_BYTES as ARTIFACT_BYTES


def _schema(kind, fields):
    props = dict(schema_version={"const": 1}, kind={"const": kind}, **fields)
    return object_schema(props, tuple(props))


EVENT_SCHEMAS = {
    "root_core_preparation_intent": _schema("root_core_preparation_intent", dict(operation_id=HASH, material_ref=REF)),
    "root_core_preparation_files": _schema("root_core_preparation_files", dict(operation_id=HASH, manifest_ref=REF)),
    "root_core_preparation_commit": _schema("root_core_preparation_commit", dict(operation_id=HASH,
        intent_digest=HASH, core_path=TEXT, core_generation=INTEGER, state_digest=HASH)),
    "root_model_reservation": _schema("root_model_reservation", dict(reservation_id=HASH,
        binding=BINDING_SCHEMA, invocation_id={"type": "string", "pattern": "^[a-f0-9]{32}$"},
        attempt={"type": "integer", "minimum": 1}, previous_attempts_used=INTEGER,
        preparation_id=NULL_HASH, repair={"type": "boolean"}, thread_id=NULL_TEXT, prompt_digest=HASH)),
    "root_model_reservation_bound": _schema("root_model_reservation_bound", dict(reservation_id=HASH,
        binding_index={"type": "integer", "minimum": 1}, slot_name={"type": "string", "pattern": "^slot-[0-4]\\.yaml$"},
        lease=LEASE_SCHEMA, slot_identity=STAMP)),
    "root_model_reservation_release_authorized": _schema("root_model_reservation_release_authorized", dict(
        reservation_id=HASH, binding_index={"type": "integer", "minimum": 1}, slot_lease_digest=HASH,
        slot_identity=STAMP, absence_head=object_schema(dict(sequence=INTEGER, hash=HASH), ('sequence', 'hash')),
        writer_token={"type": "string", "pattern": "^[a-f0-9]{32}$"})),
    "root_model_reservation_released": _schema("root_model_reservation_released", dict(reservation_id=HASH,
        binding_index={"type": "integer", "minimum": 1}, slot_lease_digest=HASH, authorization_digest=HASH,
        observation={"enum": ["exact", "reused"]})),
    "root_preparation_failure": _schema("root_preparation_failure", dict(failure_id=HASH,
        run_id=TEXT, task_id=TEXT, contract_digest=HASH, operation_context={"enum": sorted(PREPARATION_CONTEXTS)},
        origin_state=TEXT, reason_code={"enum": sorted(PREPARATION_TARGETS)}, target={"enum": sorted(set(PREPARATION_TARGETS.values()))},
        exception_type=TEXT, detail={"type": "string", "maxLength": 2048}, diagnostic_digest=HASH, diagnostic_ref=REF,
        sanitized={"type": "boolean"}, proof={**OPAQUE, "type": ["object", "null"]},
        preparation_id=NULL_HASH, reservation_id=NULL_HASH)),
    "root_preparation_failure_reconciled": _schema("root_preparation_failure_reconciled", dict(failure_id=HASH,
        contract_digest=HASH, preparation_id=NULL_HASH, binding_digest=HASH,
        explicit_reopen={"type": "boolean"}, hypothesis_digest=NULL_HASH)),
}


def fail(code, context, detail, **kw):
    raise PreparationFailure(code, context, detail, **kw)


def _lease(supervisor):
    from .core import process_identity, identity_alive
    lease = supervisor.store._lease
    if (lease is None or lease['identity'] != process_identity(os.getpid()) or not identity_alive(lease['identity'])
            or supervisor.store.read('locks/supervisor.yaml') != lease):
        fail('INTEGRITY', 'reservation_recovery', 'Current exact Supervisor writer lease required')
    return lease


def events(supervisor, *, writing=False):
    if writing:
        _lease(supervisor)
    # events() always opens and validates the complete current journal chain.
    records = supervisor.store.events()
    seen = {}; positions = {}; rows = []
    def key(row):
        return (row['kind'], row.get('operation_id', row.get('failure_id', row.get('reservation_id'))), row.get('binding_index'))
    for event in records:
        row = event['payload']
        kind = row.get('kind', '')
        if isinstance(kind,str) and kind.startswith(('root_core_preparation_', 'root_model_reservation', 'root_preparation_failure')):
            if kind not in EVENT_SCHEMAS:
                fail('INTEGRITY', 'reservation_recovery', 'Unknown root operation kind/version')
            validate(row, EVENT_SCHEMAS[kind])
            if kind == 'root_model_reservation_bound':
                from .agent_lifecycle import LEASE_SCHEMA
                validate(row['lease'], LEASE_SCHEMA)
            identity = key(row)
            if identity in seen:
                if seen[identity] != row:
                    fail('INTEGRITY', 'reservation_recovery', 'Root operation identity conflict')
                continue  # Exact duplicate facts never allocate/account twice.
            seen[identity] = row; positions[identity] = event['sequence']
        rows.append(row)
    def parent(kind, identity, index=None):
        value = seen.get((kind,identity,index))
        if value is None:
            fail('INTEGRITY','reservation_recovery','Root operation lacks its exact causal parent')
        return value
    for row in seen.values():
        kind = row['kind']
        if kind in {'root_core_preparation_files', 'root_core_preparation_commit'}:
            intent = parent('root_core_preparation_intent',row['operation_id'])
            if positions[key(intent)] >= positions[key(row)]:
                fail('INTEGRITY','core_prepare','Core publication precedes its durable preparation intent')
            if kind == 'root_core_preparation_files':
                continue
            if row['intent_digest'] != digest(intent):
                fail('INTEGRITY','core_prepare','Core commit differs from its durable preparation intent')
            files=parent('root_core_preparation_files',row['operation_id'])
            if positions[key(files)] >= positions[key(row)]:
                fail('INTEGRITY','core_prepare','Core commit precedes its prepared-file authority')
        elif kind in {'root_model_reservation_bound','root_model_reservation_release_authorized','root_model_reservation_released'}:
            reservation = parent('root_model_reservation',row['reservation_id'])
            if positions[key(reservation)] >= positions[key(row)]:
                fail('INTEGRITY','reservation_recovery','Reservation fact precedes its allocation')
            if kind != 'root_model_reservation_bound':
                bound = parent('root_model_reservation_bound',row['reservation_id'],row['binding_index'])
                if row['slot_lease_digest'] != digest(bound['lease']) or positions[key(bound)] >= positions[key(row)]:
                    fail('INTEGRITY','reservation_recovery','Cancellation differs from exact slot binding')
                if kind == 'root_model_reservation_release_authorized':
                    head=row['absence_head'];number=head['sequence']
                    if (row['slot_identity'] != bound['slot_identity'] or not positions[key(bound)] <= number < positions[key(row)]
                            or number > len(records) or records[number-1]['hash'] != head['hash']):
                        fail('INTEGRITY','reservation_recovery','Cancellation absence head is not the exact bound journal prefix')
                else:
                    authorization=parent('root_model_reservation_release_authorized',row['reservation_id'],row['binding_index'])
                    if row['authorization_digest'] != digest(authorization) or positions[key(authorization)] >= positions[key(row)]:
                        fail('INTEGRITY','reservation_recovery','Cancellation acknowledgement lacks prior authority')
        elif kind == 'root_preparation_failure':
            body={k:v for k,v in row.items() if k not in {'schema_version','kind','failure_id'}}
            if row['failure_id'] != digest(body) or row['target'] != PREPARATION_TARGETS[row['reason_code']]:
                fail('INTEGRITY','failure_publication','Typed causal failure identity/target mismatch')
        elif kind == 'root_preparation_failure_reconciled':
            failure=parent('root_preparation_failure',row['failure_id'])
            if (failure['contract_digest'] != row['contract_digest'] or positions[key(failure)] >= positions[key(row)]
                    or row['explicit_reopen'] != (row['hypothesis_digest'] is not None)
                    or row['explicit_reopen'] and failure['reason_code'] != 'NO_PROGRESS'):
                fail('INTEGRITY','failure_publication','Preparation reconciliation changed causal authority')
    return rows


def publish(supervisor, kind, **fields):
    row = dict(schema_version=1, kind=kind, **fields)
    validate(row, EVENT_SCHEMAS[kind])
    rows = events(supervisor, writing=True)
    identity = row.get('operation_id', row.get('failure_id', row.get('reservation_id')))
    old = [r for r in rows if r.get('kind') == kind and
        r.get('operation_id', r.get('failure_id', r.get('reservation_id'))) == identity and r.get('binding_index') == row.get('binding_index')]
    if old:
        if old != [row]:
            fail('INTEGRITY', 'reservation_recovery', 'Root operation publication conflict')
        return old[0]
    supervisor.store.append_event(row, format_version=2)
    return row


def _artifact(supervisor, value):
    wire = {key: item for key, item in value.items() if key not in {'contract', 'baseline', 'prior_state', 'expected_state'}}
    for key in ('contract', 'baseline', 'prior_state', 'expected_state'):
        if value[key] is None:
            wire[key + '_ref'] = None
            continue
        data = canonical(value[key]) + b'\n'
        if len(data) > ARTIFACT_BYTES:
            fail('ENVIRONMENT', 'core_prepare', 'Root preparation blob exceeds explicit100MiB storage capacity')
        checksum = hashlib.sha256(data).hexdigest()
        relative = 'context/root-preparation-blobs/' + checksum + '.json'; target = supervisor.store.path(relative)
        if target.exists():
            if read_regular_file(target, max_bytes=ARTIFACT_BYTES) != data:
                fail('INTEGRITY', 'core_prepare', 'Root preparation immutable blob conflict')
        else:
            atomic_write(target, data, exclusive=True)
        wire[key + '_ref'] = dict(path=relative, sha256=checksum)
    validate(wire, WIRE_MATERIAL_SCHEMA)
    raw = canonical(wire) + b'\n'
    if len(raw) > MAX_BYTES:
        fail('AMBIGUOUS', 'core_prepare', 'Preparation material exceeds bounded storage')
    sha = hashlib.sha256(raw).hexdigest()
    relative = 'context/root-preparation-' + sha + '.json'
    path = supervisor.store.path(relative)
    if path.exists():
        if read_regular_file(path) != raw:
            fail('INTEGRITY', 'core_prepare', 'Immutable root preparation material changed')
    else:
        atomic_write(path, raw, exclusive=True)
    return dict(path=relative, sha256=sha)


def _read_payload(supervisor, ref):
    validate(ref, REF)
    if ref['path'] != 'context/root-preparation-' + ref['sha256'] + '.json':
        fail('INTEGRITY', 'core_prepare', 'Preparation material is outside addressed storage')
    raw = read_regular_file(supervisor.store.path(ref['path']))
    if hashlib.sha256(raw).hexdigest() != ref['sha256']:
        fail('INTEGRITY', 'core_prepare', 'Preparation material digest changed')
    wire = json.loads(raw); validate(wire, WIRE_MATERIAL_SCHEMA)
    value = {key: item for key, item in wire.items() if key not in {'contract_ref', 'baseline_ref', 'prior_state_ref', 'expected_state_ref'}}
    for key in ('contract', 'baseline', 'prior_state', 'expected_state'):
        blob = wire[key + '_ref']
        if blob is None:
            value[key] = None
            continue
        if blob['path'] != 'context/root-preparation-blobs/' + blob['sha256'] + '.json':
            fail('INTEGRITY', 'core_prepare', 'Root preparation blob path binding conflict')
        data = read_regular_file(supervisor.store.path(blob['path']), max_bytes=ARTIFACT_BYTES)
        if hashlib.sha256(data).hexdigest() != blob['sha256']:
            fail('INTEGRITY', 'core_prepare', 'Root preparation blob digest changed')
        value[key] = json.loads(data)
    _validate_material(value)
    if value['operation_id'] != digest(value['binding']):
        fail('INTEGRITY', 'core_prepare', 'Preparation operation differs from exact subject')
    return value


def _read(supervisor, ref):
    try:
        return _read_payload(supervisor, ref)
    except PreparationFailure:
        raise
    except (OSError, ValueError, KeyError, TypeError, HarnessError) as error:
        raise PreparationFailure('INTEGRITY', 'core_prepare', 'Required immutable preparation material/blob invalid or missing') from error


def _validate_material(value):
    from .core import validate_contract, verify_snapshot
    if type(value) is not dict or set(value) != set(MATERIAL_FIELDS):
        fail('INTEGRITY', 'core_prepare', 'Root preparation material shape changed')
    validate({key: value[key] for key in MATERIAL_HEADER_FIELDS}, MATERIAL_HEADER_SCHEMA)
    # The core JSON snapshots have their own validators/capacity. Feeding a
    # many-file baseline through YAML's100000-node limit would narrow them.
    validate_contract(value['contract']); verify_snapshot(value['baseline'])
    prior, expected = value['prior_state'], value['expected_state']
    if prior is not None and type(prior) is not dict or type(expected) is not dict:
        fail('INTEGRITY', 'core_prepare', 'Prepared core state has invalid type')
    required = {'schema_version','run_id','task_id','current_attempt','contract_digest','attempts','status'}
    if not required <= set(expected) or expected['schema_version'] != 1 or type(expected['attempts']) is not list:
        fail('INTEGRITY', 'core_prepare', 'Prepared core state lacks required legacy fields')
    if (expected['contract_digest'] != digest(value['contract']) or expected['run_id'] != value['binding']['run_id']
            or expected['task_id'] != value['binding']['task_id']
            or (digest(prior) if prior is not None else None) != value['binding']['prior_core_state_digest']):
        fail('INTEGRITY', 'core_prepare', 'Prepared legacy state/contract digest differs from subject')


def _directory(path):
    with ParentDirectory(Path(path) / '.root-identity') as parent:
        return parent.physical_identity()


def binding(supervisor, purpose, *, attempts_before=None, core_path=None, generation=None, include_hypothesis=True):
    from .execution_receipts import build_digest
    from .work_contract import legacy_contract
    state = supervisor.state; contract = supervisor.contract
    core = Run(state['core_run']) if state['core_run'] else None
    try:
        before = core.state() if core else None
        plan = supervisor.store.read(state['plan_path'], 'agent-plan.schema.json') if state['plan_path'] else None
    except (OSError,ValueError,HarnessError) as error:
        raise PreparationFailure('INTEGRITY','core_prepare','Required current core/plan binding unavailable') from error
    number = generation if generation is not None else len(before['attempts']) if before else 1
    target = core_path or state['core_run'] or str(supervisor.path / ('verification' if contract.get('mission', {}).get('generation', 1) == 1
        else 'verification-mission-' + str(contract['mission']['generation'])))
    source = snapshot(legacy_contract(contract, cwd=state['cwd'], source_cwd=state['source_cwd'],
        run_id=state['run_id'], attempt=1, deadline=state['deadline']))
    mission = contract.get('mission')
    hypothesis = [r for r in events(supervisor) if r.get('kind') == 'root_preparation_failure_reconciled'
        and r['contract_digest'] == state['contract_digest'] and r['explicit_reopen']] if include_hypothesis else []
    failure_digest = digest(state['current_failure']) if state['current_failure'] else None
    if hypothesis:
        failure_digest = digest([failure_digest, digest(hypothesis[-1])])
    value = dict(run_id=state['run_id'], task_id=state['task_id'], mode=contract['mode'],
        contract_digest=state['contract_digest'], contract_path=state.get('contract_path', 'contract.yaml'),
        mission_digest=digest(mission) if mission else None, mission_generation=mission['generation'] if mission else None,
        plan_digest=digest(plan) if plan else None, quality_assessment_ref=state.get('plan_quality', {}).get('assessment_ref'),
        source_cwd=state['source_cwd'], cwd=state['cwd'], source_snapshot_digest=source['digest'],
        deadline=state['deadline'], budgets_digest=digest(contract['budgets']), max_attempts=state['max_attempts'],
        attempts_before=state['attempts_used'] if attempts_before is None else attempts_before,
        core_path=target, core_generation=number, prior_core_state_digest=digest(before) if before else None,
        prior_core_contract_digest=before['contract_digest'] if before else None,
        failure_digest=failure_digest,
        purpose=purpose, harness_build_digest=build_digest())
    validate(value, BINDING_SCHEMA)
    return value, source


def _subject_current(supervisor, frozen, *, core=False):
    current, _ = binding(supervisor, frozen['purpose'], attempts_before=frozen['attempts_before'],
        core_path=frozen['core_path'], generation=frozen['core_generation'])
    ignored = {'prior_core_state_digest', 'prior_core_contract_digest'} if core else set()
    if any(current[k] != frozen[k] for k in frozen if k not in ignored):
        fail('INTEGRITY', 'core_prepare' if core else 'reservation_recovery', 'Prepared root subject/input changed; no retargeting')
    if core:
        counts = {frozen['attempts_before']}
        counts.update(row['attempt'] for row in events(supervisor) if row.get('kind') == 'root_model_reservation'
            and row['preparation_id'] == digest(frozen) and row['previous_attempts_used'] == frozen['attempts_before'])
        if supervisor.state['attempts_used'] not in counts:
            fail('INTEGRITY','core_prepare','Prepared root counter changed without its exact reservation')


def admissible(supervisor, *, pending=False, context='core_prepare'):
    from .agent_lifecycle import guard_invocations
    from . import plan_quality, telemetry
    guard_invocations(supervisor)
    if (supervisor.path / 'cancel.yaml').exists():
        fail('CANCELLED', context, 'Persisted cancellation prevents preparation/dispatch')
    if supervisor.remaining() <= 0:
        fail('DEADLINE', context, 'Original wall deadline expired')
    if not pending and supervisor.state['attempts_used'] >= supervisor.state['max_attempts']:
        fail('ATTEMPT_BUDGET', context, 'Original root reservation budget exhausted')
    limit = supervisor.contract['budgets'].get('max_tokens')
    metrics = supervisor.state['metrics']
    if limit is not None:
        if not telemetry.budget_usage_known(metrics):
            fail('USAGE_UNVERIFIED', context, 'Configured token budget has unknown usage coverage')
        if sum(metrics[k] or 0 for k in ('input_tokens', 'output_tokens')) >= limit:
            fail('TOKEN_BUDGET', context, 'Observed original token budget exhausted')
    if plan_quality.enabled(supervisor):
        errors = plan_quality.evidence_errors(supervisor)
        if errors:
            fail('INTEGRITY', context, 'PLAN_QUALITY: ' + '; '.join(errors))


class CorePreparation:
    """In-process authority; every use revalidates the current writer/journal."""
    def __init__(self, supervisor, intent):
        self.supervisor, self.intent = supervisor, intent
        self.material = _read(supervisor, intent['material_ref'])

    def validate(self):
        if self.intent not in events(self.supervisor, writing=True):
            fail('INTEGRITY', 'core_prepare', 'Core preparation lacks authoritative intent')
        if _read(self.supervisor, self.intent['material_ref']) != self.material:
            fail('INTEGRITY', 'core_prepare', 'Core preparation immutable material conflict')
        _subject_current(self.supervisor, self.material['binding'], core=True)
        return self.material


def _owned_directory(path, material):
    """Unmarked or replaced directories are evidence, never deletion targets."""
    path = Path(path)
    with ParentDirectory(path) as parent:
        parent.check()
        if not path.exists():
            if parent.fd is not None:
                os.mkdir(path.name, mode=0o700, dir_fd=parent.fd)
            else:
                path.mkdir(mode=0o700)
            parent.sync(); parent.check()
            marker = path / '.supervisor-root-owner.json'
            with ParentDirectory(marker) as owner:
                fd = owner.open(os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                with os.fdopen(fd, 'wb') as output:
                    info = os.fstat(output.fileno())
                    value = dict(schema_version=1, operation_id=material['operation_id'],
                        token=material['ownership_token'], directory_identity=owner.physical_identity(),
                        file_identity=[info.st_dev, info.st_ino])
                    output.write(canonical(value) + b'\n'); output.flush(); os.fsync(output.fileno())
                owner.sync(); owner.check()
        _check_owned_directory(path, material)


def _check_owned_directory(path, material):
    marker = Path(path) / '.supervisor-root-owner.json'
    try:
        with ParentDirectory(marker) as owner:
            value = json.loads(read_regular_file(marker)); info = marker.lstat()
            expected = dict(schema_version=1, operation_id=material['operation_id'],
                token=material['ownership_token'], directory_identity=owner.physical_identity(),
                file_identity=[info.st_dev, info.st_ino])
            if canonical(value) != canonical(expected):
                fail('INTEGRITY', 'core_prepare', 'Preparation target ownership/physical identity changed')
    except (OSError, ValueError) as error:
        raise PreparationFailure('INTEGRITY', 'core_prepare', 'Unmarked or invalid preparation target; preserve for reconciliation') from error


FILE_IDENTITY = object_schema(dict(device=INTEGER,inode={'type':'integer','minimum':1}),('device','inode'))
PARENT_ROW = object_schema(dict(path=TEXT,identity=DIRECTORY_IDENTITY),('path','identity'))
PUBLICATION_FILE = object_schema(dict(path=TEXT,sha256=HASH,staged_path=TEXT,identity=FILE_IDENTITY,
    prior_identity={**FILE_IDENTITY,'type':['object','null']},parents={'type':'array','items':PARENT_ROW}),
    ('path','sha256','staged_path','identity','prior_identity','parents'))


def _blob(supervisor, value):
    data=canonical(value)+b'\n'
    if len(data)>ARTIFACT_BYTES:
        fail('ENVIRONMENT','core_prepare','Prepared publication artifact exceeds100MiB capacity')
    checksum=hashlib.sha256(data).hexdigest()
    ref=dict(path='context/root-preparation-blobs/'+checksum+'.json',sha256=checksum)
    path=supervisor.store.path(ref['path'])
    if path.exists():
        if read_regular_file(path,max_bytes=ARTIFACT_BYTES)!=data:
            fail('INTEGRITY','core_prepare','Prepared publication artifact conflicts')
    else:atomic_write(path,data,exclusive=True)
    return ref


def _blob_value(supervisor, ref):
    validate(ref,REF)
    if ref['path']!='context/root-preparation-blobs/'+ref['sha256']+'.json':
        fail('INTEGRITY','core_prepare','Prepared publication reference escaped addressed storage')
    try:raw=read_regular_file(supervisor.store.path(ref['path']),max_bytes=ARTIFACT_BYTES)
    except (OSError,ValueError) as error:
        raise PreparationFailure('INTEGRITY','core_prepare','Prepared publication artifact missing or invalid') from error
    if hashlib.sha256(raw).hexdigest()!=ref['sha256']:
        fail('INTEGRITY','core_prepare','Prepared publication artifact digest changed')
    try:return json.loads(raw)
    except ValueError as error:
        raise PreparationFailure('INTEGRITY','core_prepare','Prepared publication artifact JSON invalid') from error


def _relative(value):
    if not isinstance(value,str) or not value or '\\' in value or Path(value).is_absolute() or any(p in {'','.','..'} for p in value.split('/')):
        fail('INTEGRITY','core_prepare','Unsafe prepared publication path')
    return value


def _file_identity(path):
    try:return _opened_file_identity(path)
    except (OSError,ValueError) as error:
        raise PreparationFailure('INTEGRITY','core_prepare','Prepared physical file missing or invalid') from error


def _opened_file_identity(path):
    with ParentDirectory(path) as parent:
        fd=parent.open(os.O_RDONLY)
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                fail('INTEGRITY','core_prepare','Prepared output is not an ordinary file')
            if not info.st_ino:
                fail('ENVIRONMENT','core_prepare','Prepared output physical identity capability unavailable')
            value=dict(device=info.st_dev,inode=info.st_ino)
            observed=Path(path).lstat()
            if value!=dict(device=observed.st_dev,inode=observed.st_ino):
                fail('INTEGRITY','core_prepare','Prepared output changed while opening')
            parent.check();return value
        finally:os.close(fd)


def _output_values(material):
    contract=material['contract'];attempt='attempts/'+contract['attempt_id']+'/'
    values={'contracts/'+digest(contract)+'.json':canonical(contract)+b'\n',
        attempt+'baseline.json':canonical(material['baseline'])+b'\n',
        'state.json':canonical(material['expected_state'])+b'\n'}
    if material['prior_state'] is None:values['origin-baseline.json']=canonical(material['baseline'])+b'\n'
    for shared in contract.get('scope',{}).get('shared_files',[]):
        relative=_relative(shared['path']);source=Path(contract['cwd'])/relative
        if source.exists():
            raw=read_regular_file(source,max_bytes=ARTIFACT_BYTES)
            values[attempt+'baseline-files/'+relative]=raw
            if material['prior_state'] is None:values['origin-baseline-files/'+relative]=raw
    return values


def _output_parents(values):
    return sorted({'' if str(p)=='.' else p.as_posix() for name in values for p in Path(name).parents},
        key=lambda p:(len(Path(p).parts),p))


def _prior_publication(supervisor, material):
    core=Path(material['binding']['core_path']);directories=[]
    for relative in _output_parents(_output_values(material)):
        path=core/relative
        if path.exists():directories.append(dict(path=relative,identity=_directory(path)))
    return _blob(supervisor,dict(schema_version=1,operation_id=material['operation_id'],
        prior_state_identity=_file_identity(core/'state.json') if material['prior_state'] is not None else None,
        existing_directories=directories))


def _publication_prior(authority):
    material=authority.material;value=_blob_value(authority.supervisor,material['publication_prior_ref'])
    if type(value) is not dict or set(value)!={'schema_version','operation_id','prior_state_identity','existing_directories'} or type(value['schema_version']) is not int or value['schema_version']!=1 or value['operation_id']!=material['operation_id']:
        fail('INTEGRITY','core_prepare','Prepared publication prior identity changed')
    validate(value['prior_state_identity'],{**FILE_IDENTITY,'type':['object','null']})
    if (value['prior_state_identity'] is None)!=(material['prior_state'] is None) or type(value['existing_directories']) is not list:
        fail('INTEGRITY','core_prepare','Prepared publication prior state binding changed')
    seen=set();allowed=set(_output_parents(_output_values(material)))
    for row in value['existing_directories']:
        validate(row,PARENT_ROW)
        if row['path'] not in allowed or row['path'] in seen:
            fail('INTEGRITY','core_prepare','Prepared prior directory binding conflicts')
        seen.add(row['path'])
    return value


def _check_parent_rows(core, rows):
    for row in rows:
        try:identity=_directory(core/row['path'])
        except (OSError,ValueError) as error:
            raise PreparationFailure('INTEGRITY','core_prepare','Prepared output ancestor unavailable') from error
        if identity!=row['identity']:
            fail('INTEGRITY','core_prepare','Prepared output ancestor physical identity changed')


def _owned_baseline_directory(core, relative, material):
    # Arbitrary shared-file names remain available beneath baseline-files;
    # directory ownership metadata lives in a separate controller namespace.
    control=core/('.root-directory-markers-'+material['operation_id'])
    _owned_directory(control,material)
    path=core/relative;marker=control/(digest(relative)+'.json')
    with ParentDirectory(path) as parent:
        if not path.exists():
            if parent.fd is not None:os.mkdir(path.name,mode=0o700,dir_fd=parent.fd)
            else:path.mkdir(mode=0o700)
            parent.sync();parent.check()
            with ParentDirectory(marker) as owner:
                fd=owner.open(os.O_WRONLY|os.O_CREAT|os.O_EXCL)
                with os.fdopen(fd,'wb') as stream:
                    info=os.fstat(stream.fileno())
                    value=dict(schema_version=1,operation_id=material['operation_id'],token=material['ownership_token'],
                        directory_identity=_directory(path),file_identity=[info.st_dev,info.st_ino])
                    stream.write(canonical(value)+b'\n');stream.flush();os.fsync(stream.fileno())
                owner.sync();owner.check()
        try:
            value=json.loads(read_regular_file(marker));info=marker.lstat()
        except (OSError,ValueError) as error:
            raise PreparationFailure('INTEGRITY','core_prepare','Unmarked shared baseline directory; preserve for reconciliation') from error
        expected=dict(schema_version=1,operation_id=material['operation_id'],token=material['ownership_token'],
            directory_identity=_directory(path),file_identity=[info.st_dev,info.st_ino])
        if canonical(value)!=canonical(expected):
            fail('INTEGRITY','core_prepare','Shared baseline directory ownership changed')
        parent.check()


def _publication_parents(authority, values, prior):
    core=Path(authority.material['binding']['core_path']);existing={r['path']:r for r in prior['existing_directories']}
    _check_parent_rows(core,prior['existing_directories'])
    for relative in _output_parents(values):
        if relative not in existing:
            parts=Path(relative).parts
            if relative in {'attempts','contracts'} or parts and (parts[0]=='origin-baseline-files' or len(parts)>2 and parts[0]=='attempts' and parts[2]=='baseline-files'):
                _owned_baseline_directory(core,relative,authority.material)
            else:_owned_directory(core/relative,authority.material)
    return [dict(path=p,identity=_directory(core/p)) for p in _output_parents(values)]


def _file_manifest(authority, values, *, prepare=False):
    material=authority.material;core=Path(material['binding']['core_path']);prior=_publication_prior(authority)
    rows=[r for r in events(authority.supervisor) if r.get('kind')=='root_core_preparation_files' and r['operation_id']==material['operation_id']]
    if not rows:
        if not prepare:fail('INTEGRITY','core_prepare','Prepared core lacks durable physical file publication authority')
        parents=_publication_parents(authority,values,prior)
        stage=core/'attempts'/material['contract']['attempt_id']/'.root-file-stages';_owned_directory(stage,material)
        files=[]
        for relative,raw in sorted(values.items()):
            if len(raw)>ARTIFACT_BYTES:fail('ENVIRONMENT','core_prepare','Prepared core output exceeds100MiB capacity')
            destination=core/relative
            previous=prior['prior_state_identity'] if relative=='state.json' else None
            if destination.exists() and (previous is None or _file_identity(destination)!=previous):
                fail('INTEGRITY','core_prepare','Unpublished core destination exists without physical ownership')
            path=stage/(uuid.uuid4().hex+'.stage')
            atomic_write(path,raw,exclusive=True)
            files.append(dict(path=relative,sha256=hashlib.sha256(raw).hexdigest(),
                staged_path=path.relative_to(core).as_posix(),identity=_file_identity(path),prior_identity=previous,
                parents=[p for p in parents if p['path'] in {'' if str(v)=='.' else v.as_posix() for v in Path(relative).parents}]))
        value=dict(schema_version=1,operation_id=material['operation_id'],files=files)
        ref=_blob(authority.supervisor,value);authority.validate();_check_parent_rows(core,parents)
        rows=[publish(authority.supervisor,'root_core_preparation_files',operation_id=material['operation_id'],manifest_ref=ref)]
    value=_blob_value(authority.supervisor,rows[0]['manifest_ref'])
    if type(value) is not dict or set(value)!={'schema_version','operation_id','files'} or type(value['schema_version']) is not int or value['schema_version']!=1 or value['operation_id']!=material['operation_id'] or type(value['files']) is not list:
        fail('INTEGRITY','core_prepare','Prepared file manifest shape/operation changed')
    if len(value['files'])!=len(values):fail('INTEGRITY','core_prepare','Prepared file manifest omits outputs')
    seen=set();stage_prefix='attempts/'+material['contract']['attempt_id']+'/.root-file-stages/'
    for row in value['files']:
        validate(row,PUBLICATION_FILE);relative=_relative(row['path']);_relative(row['staged_path'])
        expected_parents={'' if str(v)=='.' else v.as_posix() for v in Path(relative).parents}
        if (relative not in values or relative in seen or row['sha256']!=hashlib.sha256(values[relative]).hexdigest()
                or not row['staged_path'].startswith(stage_prefix) or '/' in row['staged_path'][len(stage_prefix):]
                or len(row['parents'])!=len(expected_parents) or {r['path'] for r in row['parents']}!=expected_parents
                or row['prior_identity']!=(prior['prior_state_identity'] if relative=='state.json' else None)):
            fail('INTEGRITY','core_prepare','Prepared file manifest differs from exact output binding')
        seen.add(relative)
    return value


def _check_file(path, row):
    if _file_identity(path)!=row['identity']:
        fail('INTEGRITY','core_prepare','Prepared file physical identity changed; preserve foreign file')
    raw=read_regular_file(path,max_bytes=ARTIFACT_BYTES)
    if hashlib.sha256(raw).hexdigest()!=row['sha256'] or _file_identity(path)!=row['identity']:
        fail('INTEGRITY','core_prepare','Prepared file content/physical identity changed')


def _publish_prepared_file(authority, row):
    material=authority.material;core=Path(material['binding']['core_path']);destination=core/row['path'];stage=core/row['staged_path']
    _check_parent_rows(core,row['parents']);_check_owned_directory(stage.parent,material);_check_file(stage,row)
    if destination.exists() and _file_identity(destination)==row['identity']:
        _check_file(destination,row);return
    if destination.exists():
        if row['prior_identity'] is None or _file_identity(destination)!=row['prior_identity'] or json.loads(read_regular_file(destination,max_bytes=ARTIFACT_BYTES))!=material['prior_state']:
            fail('INTEGRITY','core_prepare','Prepared destination is foreign; no overwrite')
    elif row['prior_identity'] is not None:
        fail('INTEGRITY','core_prepare','Prior core state disappeared before replacement')
    with ParentDirectory(destination) as target, ParentDirectory(stage) as source:
        leaf=destination.name if row['prior_identity'] is None else '.root-publish-'+digest([material['operation_id'],row['path']])
        alias=destination.parent/leaf
        if alias.exists():_check_file(alias,row)
        else:
            try:
                if target.fd is not None and source.fd is not None:
                    os.link(stage.name,leaf,src_dir_fd=source.fd,dst_dir_fd=target.fd,follow_symlinks=False)
                else:os.link(stage,alias,follow_symlinks=False)
            except PermissionError as error:
                raise PreparationFailure('APPROVAL','core_prepare','Prepared publication requires file-link authority') from error
            except OSError as error:
                if error.errno in {errno.EXDEV,errno.ENOSYS,errno.ENOTSUP,errno.EOPNOTSUPP}:
                    raise PreparationFailure('ENVIRONMENT','core_prepare','Required same-filesystem file-link capability unavailable') from error
                raise
            target.sync()
        _check_file(alias,row);source.check();target.check();_check_parent_rows(core,row['parents'])
        if row['prior_identity'] is not None:
            if _file_identity(destination)!=row['prior_identity']:
                fail('INTEGRITY','core_prepare','Prior state inode changed at publication boundary')
            target.replace(leaf)
        _check_file(destination,row);target.sync();target.check()


def _check_prepared_files(authority, values=None):
    values=_output_values(authority.material) if values is None else values
    manifest=_file_manifest(authority,values)
    core=Path(authority.material['binding']['core_path'])
    for row in manifest['files']:
        _check_parent_rows(core,row['parents'])
        _check_owned_directory((core/row['staged_path']).parent,authority.material)
        _check_file(core/row['staged_path'],row);_check_file(core/row['path'],row)
    return manifest


def prepare_core(run, authority):
    """Only the Supervisor protocol can allocate verification generations."""
    from .core import lock, validate_contract, verify_snapshot, read_json, atomic_json
    if type(authority) is not CorePreparation:
        fail('INTEGRITY', 'core_prepare', 'Core preparation requires exact Supervisor authority')
    material = authority.validate(); subject = material['binding']; contract = material['contract']
    validate_contract(contract); verify_snapshot(material['baseline'])
    if (not same_directory(run.path, subject['core_path']) or digest(contract) != material['expected_state']['contract_digest']
            or contract['attempt_id'] != 'attempt-' + str(subject['core_generation'])
            or contract['run_id'] != subject['run_id'] or contract['task_id'] != subject['task_id']
            or contract['budget']['deadline'] != subject['deadline'] or contract['budget']['max_attempts'] != subject['max_attempts']):
        fail('INTEGRITY', 'core_prepare', 'Prepared core contract/subject mismatch')
    initial = material['prior_state'] is None
    physical_parent = run.path.parent if initial else run.path / 'attempts'
    if _directory(physical_parent) != material['parent_identity']:
        fail('INTEGRITY', 'core_prepare', 'Preparation parent physical identity changed')
    lock_path, lock_name = run._workspace_lease(contract)
    with lock(lock_path, purpose=lock_name):
        authority.validate()
        if snapshot(contract)['digest'] != subject['source_snapshot_digest']:
            fail('INTEGRITY', 'core_prepare', 'Input changed before core preparation')
        if initial:
            _owned_directory(run.path, material)
        with lock(run.path / '.state.lock', purpose='Supervisor exact verification preparation'):
            observed = run.state() if run.state_path.exists() else None
            if observed not in (material['prior_state'], material['expected_state']):
                fail('INTEGRITY', 'core_prepare', 'Actual core state differs from prepared pre/post state')
            if not initial:
                if file_hash(run.path / 'origin-baseline.json') != material['origin_baseline_digest']:
                    fail('INTEGRITY', 'core_prepare', 'Original core baseline changed')
                run._require_reconciled(material['prior_state'])
            run._check_base(contract, material['baseline'])
            values=_output_values(material)
            manifest=_file_manifest(authority,values,prepare=True)
            for row in sorted(manifest['files'],key=lambda row:row['path']=='state.json'):
                _publish_prepared_file(authority,row)
            if snapshot(contract)['digest'] != subject['source_snapshot_digest']:
                fail('INTEGRITY', 'core_prepare', 'Input changed during core preparation')
            authority.validate();_check_prepared_files(authority,values)
            return run.state()


def _materials(supervisor):
    rows = events(supervisor)
    intents = [r for r in rows if r.get('kind') == 'root_core_preparation_intent']
    result = []
    for intent in intents:
        material = _read(supervisor, intent['material_ref'])
        if material['operation_id'] != intent['operation_id']:
            fail('INTEGRITY', 'core_prepare', 'Preparation identity/material conflict')
        commits = [r for r in rows if r.get('kind') == 'root_core_preparation_commit' and r['operation_id'] == intent['operation_id']]
        expected = dict(schema_version=1, kind='root_core_preparation_commit', operation_id=intent['operation_id'],
            intent_digest=digest(intent), core_path=material['binding']['core_path'],
            core_generation=material['binding']['core_generation'], state_digest=digest(material['expected_state']))
        if commits and commits != [expected]:
            fail('INTEGRITY', 'core_prepare', 'Preparation commit differs from exact frozen material')
        result.append((intent, material, bool(commits)))
    return result


def _unused(supervisor, material):
    rows = events(supervisor)
    ids = {r['invocation_id'] for r in rows if r.get('kind') == 'root_model_reservation'
        and r['preparation_id'] == material['operation_id']}
    return not any(r.get('kind') == 'execution_receipt' and r['receipt']['record_type'] == 'invocation_intent'
        and r['receipt']['invocation_id'] in ids for r in rows)


def unused_core(supervisor):
    for intent, material, committed in reversed(_materials(supervisor)):
        if material['binding']['contract_digest'] != supervisor.state['contract_digest'] or not _unused(supervisor, material):
            continue
        if material['binding']['mode']=='managed' and material['prior_state'] is None:
            # This core anchors origin before DAG workers. It is not a root
            # dispatch preparation and must not bypass DAG recovery/admission.
            continue
        run = Run(material['binding']['core_path'])
        if (run.path / 'attempts' / material['contract']['attempt_id'] / 'candidate.json').exists():
            continue
        _subject_current(supervisor, material['binding'], core=True)
        return material
    return None


class ManagedInputProof:
    """Current complete integrated DAG, rather than a historical bootstrap."""
    def __init__(self, supervisor):
        self.supervisor=supervisor
        self.source_digest=None

    def validate(self):
        from .agent_lifecycle import Worktrees, git
        from . import verified_flow, plan_quality
        supervisor=self.supervisor;_lease(supervisor)
        if supervisor.contract['mode']!='managed':
            fail('INTEGRITY','invocation_boundary','Managed current-input authority required')
        if plan_quality.enabled(supervisor):
            errors=plan_quality.evidence_errors(supervisor)
            if errors:fail('INTEGRITY','invocation_boundary','PLAN_QUALITY: '+'; '.join(errors))
        plan=supervisor.store.read(supervisor.state['plan_path'],'agent-plan.schema.json')
        nodes={node['id'] for node in plan['tasks']}
        records=supervisor.receipts().records()
        integrations=[row for row in records if row['record_type']=='integration' and row['plan_digest']==digest(plan)]
        if not nodes <= {row['node_id'] for row in integrations}:
            fail('INTEGRITY','invocation_boundary','Initial root requires the complete integrated DAG')
        staging=supervisor.store.read('integration.yaml')
        trees=Worktrees(supervisor.state['source_cwd'],supervisor.path/'worktrees',create=False)
        checkout=trees.validate(staging,expected_id='integration',expected_role='integrator',integrated=True)
        if not same_directory(checkout,supervisor.state['cwd']) or not same_directory(checkout,supervisor.state['integration_worktree']):
            fail('INTEGRITY','invocation_boundary','Root candidate differs from owned integration checkout')
        latest=integrations[-1]['payload']
        if trees.tree(staging)!=latest['result_tree'] or git(checkout,'rev-parse','HEAD').decode().strip()!=latest['result_head']:
            fail('INTEGRITY','invocation_boundary','Integrated root tree/head changed outside its exact publication')
        if verified_flow.enabled(supervisor):
            agents={name:supervisor.store.read(supervisor.agent_path(name),'agent-state.schema.json') for name in nodes}
            frontier=verified_flow.refresh_frontier(supervisor,plan,agents,names=nodes,verify_only=True)
            if set(frontier)!=nodes:
                fail('INTEGRITY','invocation_boundary','Current complete node frontier required before initial root')
        current,_=binding(supervisor,'initial')
        if self.source_digest is not None and self.source_digest!=current['source_snapshot_digest']:
            fail('INTEGRITY','invocation_boundary','Integrated input changed during preparation')
        self.source_digest=current['source_snapshot_digest']
        return current


def prepare(supervisor, purpose, *, current_input=None):
    """Return/recover one exact unused preparation, otherwise allocate once."""
    from .work_contract import legacy_contract
    _lease(supervisor)
    existing = _materials(supervisor)
    chosen = None
    if current_input is not None:
        if type(current_input) is not ManagedInputProof or current_input.supervisor is not supervisor:
            fail('INTEGRITY','core_prepare','New integrated generation requires exact current-input authority')
        current_input.validate()
    for intent, material, committed in reversed(existing) if current_input is None else ():
        if material['binding']['contract_digest'] != supervisor.state['contract_digest']:
            continue
        if material['binding']['purpose'] != purpose:
            continue
        if _unused(supervisor, material):
            run = Run(material['binding']['core_path'])
            candidate = run.path / 'attempts' / material['contract']['attempt_id'] / 'candidate.json'
            if not candidate.exists():
                _subject_current(supervisor, material['binding'], core=True)
                chosen = (intent, material)
            break
    if chosen is None:
        admissible(supervisor)
        before_run = Run(supervisor.state['core_run']) if supervisor.state['core_run'] else None
        before = before_run.state() if before_run else None
        number = len(before['attempts']) + 1 if before else 1
        subject, baseline = binding(supervisor, purpose, generation=number)
        contract = legacy_contract(supervisor.contract, cwd=subject['cwd'], source_cwd=subject['source_cwd'],
            run_id=subject['run_id'], attempt=number, deadline=subject['deadline'])
        operation = digest(subject); timestamp = now()
        if before:
            after = deepcopy(before)
            after.update(contract_digest=digest(contract), current_attempt=contract['attempt_id'], status='planned',
                accepted=None, attempt_reason='supervisor-root:' + operation, updated_at=timestamp)
            after['attempts'].append(contract['attempt_id'])
        else:
            after = dict(schema_version=1, run_id=contract['run_id'], task_id=contract['task_id'], workflow=contract['workflow'],
                goal=contract['goal'], current_attempt=contract['attempt_id'], contract_digest=digest(contract),
                attempts=[contract['attempt_id']], status='planned', created_at=timestamp, updated_at=timestamp,
                checkpoints=0, owners={}, operations={}, accepted=None, unknown_telemetry=['account_cost', 'tokens'])
        parent = Path(subject['core_path']) / 'attempts' if before else Path(subject['core_path']).parent
        material = dict(schema_version=1, operation_id=operation, binding=subject, created_at=timestamp,
            ownership_token=uuid.uuid4().hex, parent_identity=_directory(parent), contract=contract, baseline=baseline,
            prior_state=before, expected_state=after,
            origin_baseline_digest=file_hash(before_run.path / 'origin-baseline.json') if before else None)
        material['publication_prior_ref']=_prior_publication(supervisor,material)
        _validate_material(material)
        ref = _artifact(supervisor, material)
        intent = publish(supervisor, 'root_core_preparation_intent', operation_id=operation, material_ref=ref)
        chosen = intent, material
    intent, material = chosen
    authority = CorePreparation(supervisor, intent)
    run = Run(material['binding']['core_path'])
    allocating = not run.state_path.exists() or run.state() != material['expected_state']
    admissible(supervisor, pending=not allocating or pending_reservation(supervisor) is not None)
    actual = run.prepare_supervisor_attempt(authority)
    _check_prepared_files(authority)
    publish(supervisor, 'root_core_preparation_commit', operation_id=material['operation_id'],
        intent_digest=digest(intent), core_path=material['binding']['core_path'], core_generation=material['binding']['core_generation'],
        state_digest=digest(actual))
    # Preserve the frozen spelling in historical intents and their commit facts.
    # Physical comparison above admits aliases without rewriting signed inputs.
    supervisor.state['core_run'] = material['binding']['core_path']
    for criterion in supervisor.state['acceptance']:
        criterion.update(status='pending', evidence_id=None)
    supervisor._save('exact Supervisor core preparation committed: ' + material['operation_id'])
    return run, material['operation_id']


def current_preparation(supervisor, preparation_id):
    matches=[(intent,material,committed) for intent,material,committed in _materials(supervisor)
        if material['operation_id']==preparation_id]
    if len(matches)!=1 or not matches[0][2]:
        fail('INTEGRITY','invocation_boundary','Exact committed current root preparation required')
    intent,material,_=matches[0]
    CorePreparation(supervisor,intent).validate()
    run=Run(material['binding']['core_path'])
    if run.state()!=material['expected_state']:
        fail('INTEGRITY','invocation_boundary','Prepared root core state changed before invocation')
    _check_owned_directory(run.path/'attempts'/material['contract']['attempt_id'],material)
    _check_prepared_files(CorePreparation(supervisor,intent))
    return material


def prepare_dispatch(supervisor, *, repair):
    pending=pending_reservation(supervisor)
    if supervisor.contract['mode']=='managed' and not repair:
        proof=ManagedInputProof(supervisor);proof.validate()
    else:
        proof=None
    if pending is not None:
        material=current_preparation(supervisor,pending['preparation_id'])
        supervisor._root_preparation_id=material['operation_id']
        return Run(material['binding']['core_path'])
    for intent,material,committed in reversed(_materials(supervisor)):
        if material['binding']['contract_digest']!=supervisor.state['contract_digest'] or not committed or not _unused(supervisor,material):
            continue
        if material['binding']['mode']=='managed' and material['prior_state'] is None:
            break  # Historical origin bootstrap; allocate a new current stage.
        current_preparation(supervisor,material['operation_id'])
        supervisor._root_preparation_id=material['operation_id']
        return Run(material['binding']['core_path'])
    if proof is not None:
        run,supervisor._root_preparation_id=prepare(supervisor,'initial',current_input=proof)
        return run
    fail('INTEGRITY','invocation_boundary','No exact unused root preparation; explicit reconciliation required')


CAUSE_ROW = object_schema(dict(exception_type=TEXT, detail={"type": "string", "maxLength": 8192},
    diagnostic_digest=HASH, sanitized={"type": "boolean"}, truncated={"type": "boolean"}),
    ('exception_type', 'detail', 'diagnostic_digest', 'sanitized', 'truncated'))
DIAGNOSTIC_SCHEMA = object_schema(dict(schema_version={"const": 1}, purpose={"const": "root_preparation_failure"},
    reason_code={"enum": sorted(PREPARATION_TARGETS)}, operation_context={"enum": sorted(PREPARATION_CONTEXTS)},
    origin_state=TEXT, exception_type=TEXT, causes={"type": "array", "maxItems": 8, "items": CAUSE_ROW},
    omitted_causes=INTEGER), ('schema_version', 'purpose', 'reason_code', 'operation_context',
    'origin_state', 'exception_type', 'causes', 'omitted_causes'))


def no_progress_proof(supervisor, proof):
    expected = {'failure_signature', 'source_snapshot_digest', 'history_digest'}
    if type(proof) is not dict or set(proof) != expected:
        return False
    history = supervisor.state['failure_history']
    if len(history) < 2 or not supervisor.state['current_failure']:
        return False
    current, _ = binding(supervisor, 'repair')
    return (proof['history_digest'] == digest(history) and proof['source_snapshot_digest'] == current['source_snapshot_digest']
        and proof['failure_signature'] == supervisor.state['current_failure']['signature']
        and all(row['signature'] == proof['failure_signature'] and row['snapshot_id'] == proof['source_snapshot_digest']
            for row in history[-2:]))


def _diagnostic(supervisor, error, code, context, origin):
    from .runner import redact
    # Command exceptions render cmd/argv in __str__, and wrappers may copy it.
    # A bounded sensitive chain therefore withholds all rendered messages.
    probe=error;scanned=set();structured_context=False
    while probe is not None and id(probe) not in scanned and len(scanned)<8:
        scanned.add(id(probe))
        if any(key in vars(probe) for key in ('cmd','argv','command','env','environment','prompt','transcript')):
            structured_context=True
        probe=probe.__cause__ or probe.__context__
    if probe is not None:structured_context=True
    rows = []; seen = set(); current = error
    while current is not None and id(current) not in seen and len(rows) < 8:
        seen.add(id(current))
        try:
            raw = str(current)
        except Exception:
            raw = '<diagnostic string unavailable>'
        sha = hashlib.sha256(raw.encode('utf-8', errors='replace')).hexdigest()
        try:
            # No traceback, command arguments, environment or prompt objects.
            if structured_context:raise ValueError('Structured command/context rendering withheld')
            detail = redact(raw)
            if '\n' in detail or len(detail) > 8192:
                # Multiline dumps are not an allowed causal message.
                detail = '<multiline/oversize diagnostic withheld; see digest>'
            sanitized(detail)
            safe = True
        except Exception:
            detail = '<diagnostic withheld by privacy policy>'
            safe = False
        rows.append(dict(exception_type=type(current).__name__, detail=detail,
            diagnostic_digest=sha, sanitized=safe, truncated=detail != raw))
        current = current.__cause__ or current.__context__
    packet = dict(schema_version=1, purpose='root_preparation_failure', reason_code=code,
        operation_context=context, origin_state=origin, exception_type=type(error).__name__,
        causes=rows, omitted_causes=int(current is not None))
    validate(packet, DIAGNOSTIC_SCHEMA)
    raw = canonical(packet) + b'\n'; sha = hashlib.sha256(raw).hexdigest()
    relative = 'diagnostics/root-preparation-' + sha + '.json'; path = supervisor.store.path(relative)
    if path.exists():
        if read_regular_file(path, max_bytes=ARTIFACT_BYTES) != raw:
            fail('INTEGRITY', 'failure_publication', 'Causal artifact immutable conflict')
    else:
        atomic_write(path, raw, exclusive=True)
    return dict(path=relative, sha256=sha), packet


def _check_diagnostic(supervisor, row):
    ref = row['diagnostic_ref']
    if ref['path'] != 'diagnostics/root-preparation-' + ref['sha256'] + '.json':
        fail('INTEGRITY', 'failure_publication', 'Causal artifact path binding conflict')
    raw = read_regular_file(supervisor.store.path(ref['path']))
    if hashlib.sha256(raw).hexdigest() != ref['sha256']:
        fail('INTEGRITY', 'failure_publication', 'Causal artifact digest changed')
    packet = json.loads(raw); validate(packet, DIAGNOSTIC_SCHEMA)
    if any(packet[key] != row[key] for key in ('reason_code', 'operation_context', 'origin_state', 'exception_type')):
        fail('INTEGRITY', 'failure_publication', 'Causal artifact subject conflict')
    return packet


def active_failure(supervisor):
    rows = events(supervisor)
    reconciled = {r['failure_id'] for r in rows if r.get('kind') == 'root_preparation_failure_reconciled'}
    for row in reversed(rows):
        if row.get('kind') == 'root_preparation_failure' and row['contract_digest'] == supervisor.state['contract_digest'] and row['failure_id'] not in reconciled:
            if row['run_id'] != supervisor.state['run_id'] or row['task_id'] != supervisor.state['task_id']:
                fail('INTEGRITY', 'failure_publication', 'Causal failure belongs to another run/task')
            _check_diagnostic(supervisor, row)
            return row
    return None


def handle_failure(supervisor, error, *, context='core_prepare'):
    """Publish causal authority before attempting any state projection."""
    typed = error if type(error) is PreparationFailure else PreparationFailure('INTERNAL', context,
        'Unhandled preparation error: ' + type(error).__name__)
    code = typed.reason_code
    if code == 'NO_PROGRESS' and not no_progress_proof(supervisor, typed.proof):
        code = 'INTEGRITY'
    origin = (getattr(supervisor, '_root_execution_origin', supervisor.state['status'])
        if context in {'root_preflight', 'invocation_boundary', 'core_bootstrap'} else supervisor.state['status'])
    ref, packet = _diagnostic(supervisor, error, code, typed.context, origin)
    proof = typed.proof if code == 'NO_PROGRESS' else None
    row = dict(run_id=supervisor.state['run_id'], task_id=supervisor.state['task_id'],
        contract_digest=supervisor.state['contract_digest'], operation_context=typed.context,
        origin_state=origin, reason_code=code, target=PREPARATION_TARGETS[code],
        exception_type=type(error).__name__, detail=code + ' during ' + typed.context + ': ' + type(error).__name__,
        diagnostic_digest=packet['causes'][0]['diagnostic_digest'], diagnostic_ref=ref,
        sanitized=all(r['sanitized'] for r in packet['causes']), proof=proof,
        preparation_id=getattr(supervisor, '_root_preparation_id', None),
        reservation_id=getattr(supervisor, '_root_reservation_id', None))
    row['failure_id'] = digest(row)
    value = publish(supervisor, 'root_preparation_failure', **row)
    try:
        if code == 'CANCELLED':
            supervisor.transition('CANCEL_REQUESTED', value['detail'])
        supervisor._preparation_no_progress_proof = proof
        supervisor.transition(value['target'], value['detail'] + '; causal artifact: ' + ref['path'])
    except Exception:
        # The authoritative packet already exists. A secondary projection
        # failure must not replace its cause or enable another writer.
        pass
    finally:
        supervisor._preparation_no_progress_proof = None
    return value


def reconcile_failure(supervisor, preparation_id=None, *, new_attempt=None):
    row = active_failure(supervisor)
    if row:
        subject, _ = binding(supervisor, 'repair', include_hypothesis=False)
        publish(supervisor, 'root_preparation_failure_reconciled', failure_id=row['failure_id'],
            contract_digest=supervisor.state['contract_digest'], preparation_id=preparation_id,
            binding_digest=digest(subject), explicit_reopen=new_attempt is not None and row['reason_code']=='NO_PROGRESS',
            hypothesis_digest=hashlib.sha256(new_attempt.encode()).hexdigest()
                if new_attempt is not None and row['reason_code']=='NO_PROGRESS' else None)


def explicit_reopen_current(supervisor):
    subject, _ = binding(supervisor, 'repair', include_hypothesis=False)
    return any(r.get('kind') == 'root_preparation_failure_reconciled' and r['explicit_reopen']
        and r['contract_digest'] == supervisor.state['contract_digest'] and r['binding_digest'] == digest(subject)
        for r in events(supervisor))


def decorate_status(supervisor, result):
    row = active_failure(supervisor)
    if row:
        result['preparation_failure'] = {key: row[key] for key in ('reason_code', 'operation_context',
            'origin_state', 'exception_type', 'diagnostic_ref', 'failure_id')}
        if supervisor.state['status'] in {'REPAIRING', 'EXECUTING', 'PLANNING', 'PREFLIGHT', 'FREEZING', 'VERIFYING'}:
            result.update(status=row['target'], reason=row['detail'], current_outcome='recovery_required',
                current_phase='preparation:'+row['operation_context'],
                next_allowed_action='explicit recovery of the recorded preparation cause')
    roots = reservations(supervisor)
    receipts = supervisor.receipts().records()
    root_intents = [r for r in receipts if r['record_type']=='invocation_intent' and r['node_id'] in {'@root','@integration'}]
    try:
        generation=len(Run(supervisor.state['core_run']).state()['attempts']) if supervisor.state['core_run'] else None
    except (OSError,ValueError,KeyError,HarnessError):
        generation=None  # Missing observation is not generation zero.
    result['root_execution'] = dict(reserved_attempts=supervisor.state['attempts_used'],
        journal_reservations=len(roots), invocation_intents=len(root_intents),
        verification_generation=generation)
    return result


def reservations(supervisor):
    rows = events(supervisor)
    values = [r for r in rows if r.get('kind') == 'root_model_reservation']
    attempts = {}; invocations = set()
    for row in values:
        subject = row['binding']
        if (row['reservation_id'] != digest([subject, row['attempt']])
                or row['previous_attempts_used'] != subject['attempts_before']
                or row['attempt'] != row['previous_attempts_used'] + 1 or row['attempt'] > subject['max_attempts']
                or subject['run_id'] != supervisor.state['run_id'] or subject['task_id'] != supervisor.state['task_id']
                or row['attempt'] in attempts or row['invocation_id'] in invocations):
            fail('INTEGRITY', 'reservation_recovery', 'Root reservation counter/subject/identity conflict')
        attempts[row['attempt']] = row; invocations.add(row['invocation_id'])
        bound = [r for r in rows if r.get('kind') == 'root_model_reservation_bound' and r['reservation_id'] == row['reservation_id']]
        for index, item in enumerate(bound, 1):
            if (item['binding_index'] != index or item['lease']['run_id'] != subject['run_id']
                    or item['lease']['task_id'] != subject['task_id'] or item['lease']['status'] != 'reserved'
                    or item['lease']['child_identity'] is not None or item['lease'].get('tree_cleanup_required', False)):
                fail('INTEGRITY', 'reservation_recovery', 'Root reservation slot binding conflict')
            if index > 1 and not any(r.get('kind') == 'root_model_reservation_released'
                    and r['reservation_id'] == row['reservation_id'] and r['binding_index'] == index - 1 for r in rows):
                fail('INTEGRITY', 'reservation_recovery', 'Root rebind lacks prior cancellation acknowledgement')
    return values


def pending_reservation(supervisor):
    rows = events(supervisor); values = reservations(supervisor)
    invoked = {r['receipt']['invocation_id'] for r in rows if r.get('kind') == 'execution_receipt'
        and r['receipt']['record_type'] == 'invocation_intent'}
    pending = [r for r in values if r['binding']['contract_digest'] == supervisor.state['contract_digest'] and r['invocation_id'] not in invoked]
    if len(pending) > 1:
        fail('INTEGRITY', 'reservation_recovery', 'Multiple unused root reservations require reconciliation')
    return pending[0] if pending else None


def reservation_bindings(supervisor, reservation):
    return [r for r in events(supervisor) if r.get('kind') == 'root_model_reservation_bound'
        and r['reservation_id'] == reservation['reservation_id']]


class RootReservationProof:
    """Fresh absence authority under the current run writer, never a PID test."""
    def __init__(self, supervisor, reservation, bound):
        self.supervisor, self.reservation, self.bound = supervisor, reservation, bound
        self.writer_token = _lease(supervisor)['token']

    def validate(self):
        supervisor = self.supervisor
        if _lease(supervisor)['token'] != self.writer_token:
            fail('INTEGRITY', 'reservation_recovery', 'Reservation proof writer lease changed')
        rows = events(supervisor, writing=True)
        if self.reservation not in reservations(supervisor) or self.bound not in reservation_bindings(supervisor, self.reservation):
            fail('INTEGRITY', 'reservation_recovery', 'Reservation proof lacks exact authoritative records')
        if reservation_bindings(supervisor, self.reservation)[-1] != self.bound:
            fail('INTEGRITY', 'reservation_recovery', 'Reservation proof is not the current slot binding')
        _subject_current(supervisor, self.reservation['binding'])
        if self.reservation['preparation_id'] is not None:
            prepared = [(intent,material,committed) for intent,material,committed in _materials(supervisor)
                if material['operation_id'] == self.reservation['preparation_id']]
            if len(prepared) != 1 or not prepared[0][2]:
                fail('INTEGRITY','reservation_recovery','Reserved root lacks its exact committed core preparation')
            intent,material,_ = prepared[0]
            CorePreparation(supervisor,intent).validate()
            run = Run(material['binding']['core_path'])
            if run.state() != material['expected_state']:
                fail('INTEGRITY','reservation_recovery','Reserved core changed after preparation')
            _check_owned_directory(run.path/'attempts'/material['contract']['attempt_id'],material)
            _check_prepared_files(CorePreparation(supervisor,intent))
        if supervisor.state['attempts_used'] not in (self.reservation['previous_attempts_used'], self.reservation['attempt']):
            fail('INTEGRITY', 'reservation_recovery', 'Root reserved counter cannot be reconciled')
        accounting = supervisor.receipts().reconcile_usage()
        for intent in accounting['intents'].values():
            if (intent['invocation_id'] == self.reservation['invocation_id'] or
                    intent['node_id'] in {'@root', '@integration'} and intent['attempt_id'] == 'attempt-' + str(self.reservation['attempt'])):
                fail('INTEGRITY', 'reservation_recovery', 'Invocation intent exists; dispatch remains unresolved')
        return rows

    def authorization(self):
        rows = self.validate()
        matches = [r for r in rows if r.get('kind') == 'root_model_reservation_release_authorized'
            and r['reservation_id'] == self.reservation['reservation_id'] and r['binding_index'] == self.bound['binding_index']]
        if not matches:
            return None
        if len(matches) != 1:
            fail('INTEGRITY', 'reservation_recovery', 'Duplicate release authorization')
        value = matches[0]
        if value['slot_lease_digest'] != digest(self.bound['lease']) or value['slot_identity'] != self.bound['slot_identity']:
            fail('INTEGRITY', 'reservation_recovery', 'Release authorization slot identity conflict')
        records = self.supervisor.store.events(); head = value['absence_head']; number = head['sequence']
        if number > len(records) or (records[number - 1]['hash'] if number else '0' * 64) != head['hash']:
            fail('INTEGRITY', 'reservation_recovery', 'Release authorization journal prefix changed')
        return value

    def authorize(self, row, stamp):
        value = self.authorization()
        if value is not None:
            return value
        if row != self.bound['lease'] or list(stamp) != self.bound['slot_identity']:
            fail('INTEGRITY', 'reservation_recovery', 'Reserved slot physical record changed before authorization')
        if row['status'] != 'reserved' or row['child_identity'] is not None or row.get('tree_cleanup_required', False):
            fail('INTEGRITY', 'reservation_recovery', 'Reservation slot is active or cleanup uncertain')
        records = self.supervisor.store.events()
        head = dict(sequence=len(records), hash=records[-1]['hash'] if records else '0' * 64)
        return publish(self.supervisor, 'root_model_reservation_release_authorized',
            reservation_id=self.reservation['reservation_id'], binding_index=self.bound['binding_index'],
            slot_lease_digest=digest(row), slot_identity=list(stamp), absence_head=head, writer_token=self.writer_token)

    def acknowledge(self, authorization, observation):
        if self.authorization() != authorization:
            fail('INTEGRITY', 'reservation_recovery', 'Release acknowledgement lost exact authority')
        return publish(self.supervisor, 'root_model_reservation_released', reservation_id=self.reservation['reservation_id'],
            binding_index=self.bound['binding_index'], slot_lease_digest=digest(self.bound['lease']),
            authorization_digest=digest(authorization), observation=observation)


def reconcile_pending(supervisor):
    from .agent_lifecycle import GlobalSlots
    reservation = pending_reservation(supervisor)
    if reservation is None:
        return None
    _lease(supervisor); _subject_current(supervisor, reservation['binding'])
    if supervisor.state['attempts_used'] not in (reservation['previous_attempts_used'], reservation['attempt']):
        fail('INTEGRITY', 'reservation_recovery', 'Counter differs from exact reserved before/after values')
    bindings = reservation_bindings(supervisor, reservation)
    if not bindings:
        fail('INTEGRITY', 'reservation_recovery', 'Unused reservation has no proven slot binding')
    bound = bindings[-1]
    released = [r for r in events(supervisor) if r.get('kind') == 'root_model_reservation_released'
        and r['reservation_id'] == reservation['reservation_id'] and r['binding_index'] == bound['binding_index']]
    if not released:
        GlobalSlots(supervisor.state_dir, create=False).reconcile_root_reservation(RootReservationProof(supervisor, reservation, bound))
    else:
        proof = RootReservationProof(supervisor, reservation, bound)
        authorization = proof.authorization()
        if authorization is None or released[0]['authorization_digest'] != digest(authorization):
            fail('INTEGRITY', 'reservation_recovery', 'Cancellation acknowledgement lacks exact authority')
    if supervisor.state['attempts_used'] != reservation['attempt']:
        supervisor.state['attempts_used'] = reservation['attempt']
        supervisor._save('recover one exact pre-intent root reservation without another allocation')
    supervisor._root_reservation_id = reservation['reservation_id']
    supervisor._root_preparation_id = reservation['preparation_id']
    return reservation


def reserve(supervisor, prompt, slot, *, repair):
    if supervisor.contract['mode']=='managed' and not repair:
        ManagedInputProof(supervisor).validate()
    if getattr(supervisor,'_root_preparation_id',None) is not None:
        current_preparation(supervisor,supervisor._root_preparation_id)
    reservation = pending_reservation(supervisor)
    sha = hashlib.sha256(prompt.encode('utf-8')).hexdigest()
    if reservation:
        _subject_current(supervisor, reservation['binding'])
        if (reservation['prompt_digest'] != sha or reservation['repair'] != repair
                or reservation['thread_id'] != (supervisor.state['thread_id'] if repair else None)):
            fail('INTEGRITY', 'invocation_boundary', 'Reserved prompt/context/session changed; no silent retargeting')
        admissible(supervisor, pending=True, context='invocation_boundary')
    else:
        admissible(supervisor, context='invocation_boundary')
        subject, _ = binding(supervisor, 'repair' if repair else 'initial')
        attempt = supervisor.state['attempts_used'] + 1
        reservation = publish(supervisor, 'root_model_reservation', reservation_id=digest([subject, attempt]),
            binding=subject, invocation_id=supervisor._pending_invocation_id, attempt=attempt,
            previous_attempts_used=supervisor.state['attempts_used'], preparation_id=getattr(supervisor, '_root_preparation_id', None),
            repair=repair, thread_id=supervisor.state['thread_id'] if repair else None, prompt_digest=sha)
    bindings = reservation_bindings(supervisor, reservation)
    if bindings and not any(r.get('kind') == 'root_model_reservation_released'
            and r['reservation_id'] == reservation['reservation_id'] and r['binding_index'] == len(bindings) for r in events(supervisor)):
        fail('INTEGRITY', 'invocation_boundary', 'Unused root lease must be reconciled before rebinding')
    stamp = slot.store._journal_stamp(slot.store.path(slot.name))
    if slot.store.read(slot.name) != slot.value:
        fail('INTEGRITY', 'invocation_boundary', 'Root slot changed before reservation binding')
    bound = publish(supervisor, 'root_model_reservation_bound', reservation_id=reservation['reservation_id'],
        binding_index=len(bindings) + 1, slot_name=slot.name, lease=deepcopy(slot.value), slot_identity=list(stamp))
    supervisor._root_reservation_id = reservation['reservation_id']
    supervisor.state['attempts_used'] = reservation['attempt']
    supervisor._save('model attempt reserved at validated dispatch boundary')
    return reservation, bound


def assert_dispatch(supervisor, reservation, bound):
    rows = events(supervisor, writing=True)
    if reservation not in reservations(supervisor) or bound != reservation_bindings(supervisor, reservation)[-1]:
        fail('INTEGRITY', 'invocation_boundary', 'Root invocation lost current reservation binding')
    if any(r.get('kind') == 'root_model_reservation_release_authorized' and r['reservation_id'] == reservation['reservation_id']
            and r['binding_index'] == bound['binding_index'] for r in rows):
        fail('INTEGRITY', 'invocation_boundary', 'Cancelled root binding cannot dispatch')
    _subject_current(supervisor, reservation['binding'])
    if supervisor.contract['mode']=='managed' and not reservation['repair']:
        ManagedInputProof(supervisor).validate()
    if reservation['preparation_id'] is not None:
        current_preparation(supervisor,reservation['preparation_id'])
    if supervisor.state['attempts_used'] != reservation['attempt']:
        fail('INTEGRITY', 'invocation_boundary', 'Invocation reservation counter is not exact')


@contextmanager
def workspace_acquisition(supervisor, run, slot):
    """Known local no-dispatch while the exact newly acquired guard is live.

    Only the workspace __enter__ is covered. Exceptions from the invocation
    body, BaseException crashes and any contradictory receipt/row retain the
    lease. This does not reclaim a historical unbound slot after restart.
    """
    token=_lease(supervisor)['token'];original=deepcopy(slot.value)
    stamp=slot.store._journal_stamp(slot.store.path(slot.name))
    with ExitStack() as stack:
        try:stack.enter_context(run.workspace_lock())
        except Exception as error:
            try:
                if _lease(supervisor)['token']!=token:
                    fail('INTEGRITY','invocation_boundary','Workspace admission writer changed')
                events(supervisor,writing=True)
                records=supervisor.receipts().records()
                if any(r['record_type']=='invocation_intent' and
                        (r['invocation_id']==supervisor._pending_invocation_id or
                         r['payload']['owner_id']==original['lease_id'] or
                         r['node_id'] in {'@root','@integration'} and r['attempt_id']=='attempt-'+str(supervisor._pending_attempt_number))
                        for r in records):
                    fail('INTEGRITY','invocation_boundary','Workspace failure has an invocation intent; no local release')
                if (slot.value!=original or slot.store.read(slot.name,LEASE_SCHEMA)!=original
                        or slot.store._journal_stamp(slot.store.path(slot.name))!=stamp
                        or original['status']!='reserved' or original['child_identity'] is not None
                        or original.get('tree_cleanup_required',False)):
                    fail('INTEGRITY','invocation_boundary','Workspace failure lost exact unused live lease')
                slot.complete()
            except Exception as proof_error:
                proof_error.__cause__=error
                raise PreparationFailure('INTEGRITY','invocation_boundary',
                    'Workspace failure cannot qualify local no-dispatch; preserve unresolved lease') from proof_error
            raise
        yield


class PreflightAdapter:
    """Attach operation context to executable failures without parsing text."""
    def __init__(self, adapter):
        self.adapter, self.called = adapter, False

    def preflight(self, prompt, **options):
        callback = getattr(self.adapter, 'preflight', None) or getattr(self.adapter, 'argv', None)
        if callable(callback):
            self.called = True
            try:
                return callback(prompt, **options)
            except FileNotFoundError as error:
                raise PreparationFailure('ENVIRONMENT', 'root_preflight', 'Required executable/capability unavailable') from error
            except PermissionError as error:
                raise PreparationFailure('APPROVAL', 'root_preflight', 'Executable preflight requires additional authority') from error


def preflight_failure(error, proxy):
    current = error; seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current) is PreparationFailure:
            result = PreparationFailure(current.reason_code, current.context, str(current), proof=current.proof)
            result.__cause__ = error
            return result
        current = current.__cause__ or current.__context__
    from .task_packets import NotDispatched
    code = ('AMBIGUOUS' if proxy.called else 'INTEGRITY') if isinstance(error, NotDispatched) else 'INTERNAL'
    result = PreparationFailure(code, 'root_preflight', 'Root preflight could not establish exact dispatch authority')
    result.__cause__ = error
    return result


def abort_before_intent(supervisor, slot):
    """Caller still holds this slot guard; durable cancellation precedes close."""
    reservation = pending_reservation(supervisor)
    if reservation:
        bound = reservation_bindings(supervisor, reservation)
        if bound and bound[-1]['lease']['lease_id'] == slot.value['lease_id']:
            proof = RootReservationProof(supervisor, reservation, bound[-1])
            proof.authorize(slot.value, slot.store._journal_stamp(slot.store.path(slot.name)))
        elif bound:
            fail('INTEGRITY', 'reservation_recovery', 'Cannot cancel a different root slot')
    slot.complete()
