"""Frozen plan-quality policy, bounded rounds and exact configured-reviewer evidence."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from .core import HarnessError, digest
from .file_io import read_regular_file
from .strict_yaml import _schema, validate
from .supervisor_store import atomic_write

STATE_SCHEMA = 'work-state.v3.schema.json'
POLICY_SCHEMA = 'plan-quality-policy.v1.schema.json'
ASSESSMENT_SCHEMA = 'plan-quality-assessment.v1.schema.json'
DETERMINISTIC = ['mandatory_coverage', 'acyclic_dependencies', 'exclusive_write_owners',
    'nonempty_outcomes_and_checks', 'frozen_original_request']
SEMANTIC = ['coherent_outcomes', 'independent_upstream_acceptance', 'meaningful_checks',
    'shared_interface_ownership', 'justified_atomicity_and_handoff']


class AssessmentStopped(HarnessError):
    def __init__(self, failure, completion_ref):
        super().__init__(failure['detail'])
        self.failure, self.completion_ref = failure, completion_ref


def _bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _artifact(path, raw):
    return {'path': path, 'data': raw.decode('utf-8'), 'sha256': _hash(raw)}


def _event(kind, artifact):
    return {'kind': kind, 'value': json.loads(artifact['data']),
        'artifact_digest': artifact['sha256'], 'path': artifact['path']}


def prepare_upgrade(source_state, contract, *, contract_path, max_rounds=None, completion_policy=None):
    """Pure frozen material builder; explicit migration owns the state CAS/barrier."""
    if (source_state['schema_version'] != 2 or contract['schema_version'] != 2 or contract['mode'] != 'managed'
            or source_state['contract_digest'] != digest(contract)
            or contract_path != source_state.get('contract_path', 'contract.yaml')):
        raise HarnessError('Plan-quality upgrade requires exact committed managed v2 contract')
    limit = contract['budgets']['max_attempts'] if max_rounds is None else max_rounds
    if type(limit) is not int or not 1 <= limit <= contract['budgets']['max_attempts']:
        raise HarnessError('Plan-quality round limit may only tighten original max_attempts')
    schema_raw = _bytes(_schema(ASSESSMENT_SCHEMA))
    schema_artifact = _artifact('context/required-' + _hash(schema_raw) + '.schema.json', schema_raw)
    policy = {'schema_version': 1, 'policy_id': 'coherent-outcomes-v1', 'max_rounds': limit,
        'output_schema_digest': schema_artifact['sha256'], 'deterministic_obligations': DETERMINISTIC,
        'semantic_obligations': SEMANTIC}
    validate(policy, POLICY_SCHEMA)
    policy_raw = _bytes(policy)
    policy_artifact = _artifact('policies/plan-quality-' + _hash(policy_raw) + '.json', policy_raw)
    state = deepcopy(source_state)
    state.update(schema_version=3, status='RECOVERY_REQUIRED', reason='Explicit upgrade requires current plan-quality assessment')
    state['plan_quality'] = {'policy_ref': {'path': policy_artifact['path'], 'sha256': policy_artifact['sha256'],
        'digest': digest(policy), 'output_schema_ref': {'path': schema_artifact['path'], 'sha256': schema_artifact['sha256']}},
        'max_rounds': limit, 'rounds_used': 0, 'plan_revision': 0, 'active_round': None,
        'active_plan_digest': None, 'assessment_ref': None, 'phase': 'unassessed'}
    artifacts = [schema_artifact, policy_artifact]
    events = [_event('plan_quality_policy', policy_artifact)]
    if completion_policy is None:
        # Completion policy has its own frozen version axis. It is installed
        # only behind the migration3 barrier or before a new run's first head.
        from .completion_eligibility import POLICY, POLICY_PATH
        review_raw = (json.dumps(_schema('managed-review.v3.schema.json'), sort_keys=True) + '\n').encode()
        review_artifact = _artifact('context/required-' + _hash(review_raw) + '.schema.json', review_raw)
        completion = {'schema_version': 1, 'policy': POLICY, 'review_version': 3, 'review_schema_digest': review_artifact['sha256']}
        completion_raw = (json.dumps(completion, sort_keys=True) + '\n').encode()
        completion_artifact = _artifact(POLICY_PATH, completion_raw)
        artifacts.extend([review_artifact, completion_artifact])
        events.append(_event('completion_policy', completion_artifact))
    validate(state, STATE_SCHEMA)
    return {'state': state, 'artifacts': artifacts, 'events': events}


def publish_bootstrap(store, material):
    """Idempotent exact publication under caller lease, after upgrade barrier."""
    if set(material) != {'state', 'artifacts', 'events'}:
        raise HarnessError('Invalid plan-quality bootstrap material')
    validate(material['state'], STATE_SCHEMA)
    artifacts = {}
    for item in material['artifacts']:
        if set(item) != {'path', 'data', 'sha256'} or not isinstance(item['data'], str):
            raise HarnessError('Invalid plan-quality bootstrap artifact')
        raw = item['data'].encode('utf-8')
        if _hash(raw) != item['sha256'] or item['path'] in artifacts:
            raise HarnessError('Plan-quality bootstrap artifact digest/conflict')
        path = store.path(item['path'])
        if path.exists() and read_regular_file(path) != raw:
            raise HarnessError('Immutable plan-quality bootstrap artifact changed')
        artifacts[item['path']] = raw
    events = [event['payload'] for event in store.events()]
    missing = []
    for value in material['events']:
        if (set(value) != {'kind', 'value', 'artifact_digest', 'path'}
                or value['kind'] not in {'completion_policy', 'plan_quality_policy'}
                or value['path'] not in artifacts or _hash(artifacts[value['path']]) != value['artifact_digest']
                or json.loads(artifacts[value['path']]) != value['value']):
            raise HarnessError('Invalid plan-quality bootstrap authoritative pin')
        previous = [event for event in events if event.get('kind') == value['kind']]
        if previous and (len(previous) != 1 or previous[0] != value):
            raise HarnessError('Plan-quality bootstrap authoritative pin conflict')
        if not previous:
            missing.append(value)
    for relative, raw in artifacts.items():
        path = store.path(relative)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, raw, exclusive=True)
    for value in missing:
        store.append_event(value)


def enabled(supervisor):
    return supervisor.state['schema_version'] == 3 and supervisor.contract['mode'] == 'managed' and supervisor.contract['schema_version'] == 2


def initialize(supervisor, *, max_rounds=None):
    material = prepare_upgrade(supervisor.state, supervisor.contract,
        contract_path=supervisor.state.get('contract_path', 'contract.yaml'), max_rounds=max_rounds)
    publish_bootstrap(supervisor.store, material)
    status, reason = supervisor.state['status'], supervisor.state['reason']
    supervisor.state = material['state']
    supervisor.state.update(status=status, reason=reason)


def read_policy(supervisor):
    if not enabled(supervisor):
        raise HarnessError('Legacy run has no plan-quality assessment policy')
    quality = supervisor.state['plan_quality']
    ref = quality['policy_ref']
    raw = read_regular_file(supervisor.store.path(ref['path']))
    value = json.loads(raw)
    validate(value, POLICY_SCHEMA)
    pins = [event['payload'] for event in supervisor.store.events() if event['payload'].get('kind') == 'plan_quality_policy']
    expected = {'kind': 'plan_quality_policy', 'value': value, 'artifact_digest': ref['sha256'], 'path': ref['path']}
    schema_ref = ref['output_schema_ref']
    schema_raw = read_regular_file(supervisor.store.path(schema_ref['path']))
    if (len(pins) != 1 or pins[0] != expected or _hash(raw) != ref['sha256'] or digest(value) != ref['digest']
            or _hash(schema_raw) != schema_ref['sha256'] or value['output_schema_digest'] != schema_ref['sha256']
            or ref['path'] != 'policies/plan-quality-' + ref['sha256'] + '.json'
            or schema_ref['path'] != 'context/required-' + schema_ref['sha256'] + '.schema.json'
            or quality['max_rounds'] != value['max_rounds']):
        raise HarnessError('Plan-quality policy/schema authoritative binding changed')
    from .completion_eligibility import policy as completion_policy
    if completion_policy(supervisor) is None:
        raise HarnessError('Plan-quality state lacks authoritative completion policy')
    return value


def _publish(supervisor, value):
    from .task_packets import _publish as publish
    raw = _bytes(value)
    artifact = publish(supervisor, raw, '.json')
    return {'path': Path(artifact['path']).relative_to(supervisor.path).as_posix(), 'sha256': artifact['sha256']}


def _read(supervisor, ref):
    relative = ref['path'].replace('\\', '/')
    if relative != 'context/required-' + ref['sha256'] + '.json':
        raise HarnessError('Plan-quality artifact is outside its addressed owned path')
    raw = read_regular_file(supervisor.store.path(relative))
    if _hash(raw) != ref['sha256']:
        raise HarnessError('Plan-quality immutable input/assessment artifact changed')
    return json.loads(raw)


def _validate_plan(supervisor, plan):
    from .scheduling import validate_plan
    validate_plan(plan, supervisor.contract)
    checks = {item['id']: item for item in supervisor.contract['node_checks']}
    for node in plan['tasks']:
        if not node['goal'].strip() or not node['verification'] or not all(str(value).strip() for value in node['verification']):
            raise HarnessError('PLAN_QUALITY_EMPTY_OUTCOME: observable outcome and verification are required')
        for phase in ('local', 'integration'):
            if not node['checks'][phase]:
                raise HarnessError('PLAN_QUALITY_EMPTY_CHECK: outcome needs executable checks')
            for name in node['checks'][phase]:
                check = checks[name]
                if check['kind'] != 'command' or not check['required'] or not check.get('argv'):
                    raise HarnessError('PLAN_QUALITY_EMPTY_CHECK: non-executable outcome check')


def _binding(supervisor, plan, input_digest):
    quality = supervisor.state['plan_quality']
    return {'mission_digest': digest(supervisor.contract['mission']),
        'mission_generation': supervisor.contract['mission']['generation'],
        'contract_digest': supervisor.state['contract_digest'], 'plan_revision': quality['plan_revision'],
        'plan_digest': digest(plan), 'policy_version': 1, 'policy_digest': quality['policy_ref']['digest'],
        'input_digest': input_digest, 'round': quality['active_round']}


def _input(supervisor, plan, request):
    return {'schema_version': 1, 'purpose': 'plan_quality', 'contract': supervisor.contract,
        'mission': supervisor.contract['mission'], 'plan': plan, 'policy': read_policy(supervisor),
        'mission_generation': supervisor.contract['mission']['generation'],
        'plan_revision': supervisor.state['plan_quality']['plan_revision'],
        'round': supervisor.state['plan_quality']['active_round'], 'feedback': request['feedback'],
        'source': {'revision': supervisor.state['revision'], 'baseline_digest': supervisor.state['baseline_digest']}}


def _validate_assessment(supervisor, plan, wire, binding):
    schema = supervisor.store.path(supervisor.state['plan_quality']['policy_ref']['output_schema_ref']['path'])
    validate(wire, schema)
    if any(wire[key] != value for key, value in binding.items()):
        raise HarnessError('Plan-quality assessment subject/round binding mismatch')
    _validate_plan(supervisor, plan)
    expected = {node['id']: node for node in plan['tasks']}
    ids = [row['node_id'] for row in wire['nodes']]
    if len(ids) != len(set(ids)) or set(ids) != set(expected):
        raise HarnessError('Plan-quality node rationale coverage missing or ambiguous')
    interfaces = {}
    for row in wire['nodes']:
        node = expected[row['node_id']]
        if (row['owner'] != node['id'] or row['requirement_ids'] != node['requirement_ids']
                or row['depends_on'] != node['depends_on'] or row['local_checks'] != node['checks']['local']
                or row['integration_checks'] != node['checks']['integration']):
            raise HarnessError('Plan-quality reviewer changed frozen node/check/dependency subjects')
        for interface in row['shared_interfaces']:
            if interface['owner'] not in expected or not set(interface['consumers']) <= set(expected):
                raise HarnessError('Plan-quality shared interface owner/consumer is unknown')
            value = (interface['owner'], tuple(sorted(interface['consumers'])))
            if interface['name'] in interfaces and interfaces[interface['name']] != value:
                raise HarnessError('Plan-quality shared interface has conflicting owners/consumers')
            interfaces[interface['name']] = value
        if wire['verdict'] == 'approved' and (row['atomicity'] != 'coherent'
                or row['dependency_valid'] is not True or row['checks_meaningful'] is not True):
            raise HarnessError('Plan-quality semantic assessment does not permit approval')
    if wire['verdict'] == 'approved' and wire['findings']:
        raise HarnessError('Plan-quality approval contains unresolved findings')
    if wire['verdict'] != 'approved' and not wire['findings']:
        raise HarnessError('Plan-quality revision/block requires actionable findings')
    for finding in wire['findings']:
        if not set(finding['node_ids']) <= set(expected):
            raise HarnessError('Plan-quality finding cites unknown node')


def _refs(supervisor, binding):
    return [supervisor.state['contract_digest'], binding['policy_digest'], binding['plan_digest'],
        binding['mission_digest'], binding['input_digest'],
        supervisor.state['plan_quality']['policy_ref']['output_schema_ref']['sha256'], digest(binding)]


def _quality_intent(supervisor, binding):
    receipts = supervisor.receipts()
    value = receipts.envelope('invocation_intent', '@plan-quality', 'plan-quality-' + str(binding['round']))
    value['invocation_id'] = __import__('uuid').uuid4().hex
    value['record_id'] = value['invocation_id'] + '-intent'
    value['input_refs'] = _refs(supervisor, binding)
    return value


def _current_request(supervisor):
    quality = supervisor.state['plan_quality']
    events = [event['payload'] for event in supervisor.store.events() if event['payload'].get('kind') == 'plan_quality_round'
        and event['payload']['round'] == quality['active_round']]
    if len(events) != 1:
        raise HarnessError('Plan-quality active round lacks exact authoritative request')
    value = _read(supervisor, events[0]['request_ref'])
    if (digest(value) != events[0]['request_digest'] or value['round'] != quality['active_round']
            or value['plan_revision'] != quality['plan_revision'] or value['contract_digest'] != supervisor.state['contract_digest']
            or value['mission_digest'] != digest(supervisor.contract['mission'])
            or value['policy_digest'] != quality['policy_ref']['digest']):
        raise HarnessError('Plan-quality current request subject changed')
    return value


def _publish_assessment(supervisor, intent, completion, wire, binding):
    receipts = supervisor.receipts()
    identity = 'plan-quality-' + digest([intent['invocation_id'], binding])[:48]
    accounting = receipts.reconcile_usage()
    existing = next((record for record in accounting['records'] if record['record_id'] == identity), None)
    if existing is not None:
        actual, _, actual_binding = _verified_assessment_record(supervisor, existing, accounting)
        if actual != wire or actual_binding != binding:
            raise HarnessError('Published plan-quality receipt differs from exact completion')
        # Receipt publication follows artifact publication. A missing/changed
        # already-published artifact is integrity failure, never file repair.
        return existing
    artifact = _publish(supervisor, wire)
    value = receipts.envelope('plan_quality_assessment', '@plan-quality', intent['attempt_id'], intent['invocation_id'])
    value['record_id'] = identity
    for key in ('created_at', 'generation', 'harness_version', 'harness_build_digest', 'client_version',
            'producer_id', 'producer_kind', 'capability_receipt_ref', 'execution_owner'):
        value[key] = completion[key]
    value['input_refs'] = list(intent['input_refs'])
    value['payload'] = {'completion_ref': completion['record_id'], 'assessment_ref': artifact['path'],
        'assessment_digest': artifact['sha256'], **{key: binding[key] for key in
            ('mission_digest', 'contract_digest', 'plan_digest', 'policy_digest', 'input_digest', 'round')},
        'output_schema_digest': supervisor.state['plan_quality']['policy_ref']['output_schema_ref']['sha256'],
        'verdict': wire['verdict']}
    return receipts.publish(value)


def assess(supervisor, plan):
    """Configured read-only reviewer, or exact completion/raw replay; no second runner."""
    import time
    from .agent_lifecycle import GlobalSlots, guard_invocations, invocation_timeout
    from .task_packets import compile_packet, preflight, invoke
    from .managed_review import _raw_verdict
    from .core import snapshot
    from .work_contract import legacy_contract
    policy = read_policy(supervisor)
    _validate_plan(supervisor, plan)
    quality = supervisor.state['plan_quality']
    if quality['phase'] == 'approved':
        errors = evidence_errors(supervisor, plan)
        if errors:
            raise HarnessError('Published plan-quality proof is no longer current: ' + '; '.join(errors))
        accounting = supervisor.receipts().reconcile_usage()
        existing = next(record for record in accounting['records'] if record['record_id'] == quality['assessment_ref'])
        wire, _, _ = _verified_assessment_record(supervisor, existing, accounting)
        return wire
    request = _current_request(supervisor)
    input_value = _input(supervisor, plan, request)
    input_ref = _publish(supervisor, input_value)
    binding = _binding(supervisor, plan, input_ref['sha256'])
    template = _quality_intent(supervisor, binding)
    receipts = supervisor.receipts()
    accounting = guard_invocations(supervisor)
    matching = [r for r in accounting['intents'].values() if r['node_id'] == '@plan-quality' and r['attempt_id'] == template['attempt_id']]
    if len(matching) > 1 or matching and (matching[0]['plan_digest'] != digest(plan) or matching[0]['input_refs'] != template['input_refs']):
        raise HarnessError('Plan-quality dispatched input binding conflict')
    if matching:
        intent = matching[0]
        expected_ref = 'plan-quality-' + digest([intent['invocation_id'], binding])[:48]
        if quality['assessment_ref'] is not None and quality['assessment_ref'] != expected_ref:
            raise HarnessError('Current plan-quality assessment pointer conflicts with dispatched binding')
        completion = accounting['completions'][intent['invocation_id']]
    else:
        if not supervisor._model_budget_ok(for_spawn=True):
            return None
        if not supervisor._adapter().capabilities().get('output_schema'):
            raise HarnessError('Configured plan-quality reviewer requires structured output capability')
        schema_ref = quality['policy_ref']['output_schema_ref']
        schema_path = supervisor.store.path(schema_ref['path'])
        prompt = compile_packet(supervisor, 'HARNESS_PLAN_QUALITY\nPurpose plan_quality. Review the original request and frozen plan read-only. '
            'Judge coherent outcomes, dependency acceptance, shared owners and actual check value. '
            'Do not split a justified atomic operation; reject unrelated giant outcomes or cosmetic fragments. '
            'Do not rewrite requirements/requiredness or trusted bindings. Return the exact supplied binding and per-node rationales. '
            'Do not edit, spawn, publish, change model/provider/effort/settings or call session connectors.',
            {'purpose': 'plan_quality', 'binding': binding, 'input': input_value, 'input_ref': input_ref})
        preflight(supervisor._adapter(), prompt, read_only=True, write_authorized=False, native_children=False, output_schema=str(schema_path))
        quality['phase'] = 'reviewing'
        supervisor._save('plan-quality reviewer admission; original round/usage/deadline retained')
        source_contract = legacy_contract(supervisor.contract, cwd=supervisor.state['source_cwd'],
            run_id=supervisor.state['run_id'], attempt=1, deadline=supervisor.state['deadline'])
        before = snapshot(source_contract)['digest']
        with GlobalSlots(supervisor.state_dir).acquire(supervisor.state['run_id'], '@plan-quality', 'reviewer',
                cancel_file=supervisor.path / 'cancel.yaml', timeout=min(60, supervisor.remaining())) as slot:
            if not supervisor._model_budget_ok(for_spawn=True):
                slot.complete()
                return None
            template['payload'] = {'checkout': supervisor.state['source_cwd'], 'owner_id': slot.value['lease_id'],
                'dependency_vector': [], 'contract_digest': supervisor.state['contract_digest'],
                'deadline': supervisor.state['deadline'], 'dispatch_state': 'possibly_dispatched',
                'candidate_digest': digest(plan), 'review_binding': None}
            intent = receipts.publish(template)
            started = time.monotonic()
            result = invoke(supervisor._adapter(), prompt, slot=slot, cwd=supervisor.state['source_cwd'],
                read_only=True, write_authorized=False, native_children=False, output_schema=str(schema_path),
                timeout=invocation_timeout(supervisor, 'reviewer'), deadline=supervisor.state['deadline'],
                cancel_file=supervisor.path / 'cancel.yaml', on_process=lambda value: (slot.bind(value), supervisor._on_process(value)),
                log_dir=supervisor.path / 'codex' / ('plan-quality-' + str(binding['round'])))
            result['slot_wait_ms'] = slot.wait_seconds * 1000
            completion = receipts.completion(intent, result, duration_ms=(time.monotonic() - started) * 1000)
            guard_invocations(supervisor, node_ids={'@plan-quality'})
            slot.complete(result)
        supervisor.state.update(process_identity=None, process_status=result['status'])
        supervisor._save('plan-quality completion persisted before assessment publication')
        if snapshot(source_contract)['digest'] != before:
            raise HarnessError('Plan-quality reviewer modified original source; reconcile before any admission')
    if completion['payload']['outcome'] != 'completed':
        if completion['payload']['dispatch_state'] == 'not_dispatched_proven':
            raise HarnessError('Plan-quality reviewer refused locally before dispatch')
        from .failure_policy import classify
        raise AssessmentStopped(classify(receipts.result(completion)), completion['record_id'])
    artifact = completion['payload']['logs']
    if artifact['stdout'] is None:
        raise HarnessError('Plan-quality completion raw verdict missing')
    raw = read_regular_file(artifact['stdout'])
    if _hash(raw) != artifact['stdout_digest']:
        raise HarnessError('Plan-quality completion raw verdict changed')
    wire = _raw_verdict(raw, supervisor.store.path(quality['policy_ref']['output_schema_ref']['path']))
    _validate_assessment(supervisor, plan, wire, binding)
    record = _publish_assessment(supervisor, intent, completion, wire, binding)
    quality.update(assessment_ref=record['record_id'], phase=wire['verdict'])
    supervisor._save('controller published exact plan-quality assessment; approval is plan-bound')
    return wire


def evidence_errors(supervisor, plan=None):
    """Independent read-only gate; an approved wire alone is never evidence."""
    if not enabled(supervisor):
        return []
    try:
        from .agent_lifecycle import guard_invocations
        read_policy(supervisor)
        quality = supervisor.state['plan_quality']
        events = _round_events(supervisor)
        if quality['rounds_used'] != len(events) or quality['active_round'] is not None and quality['active_round'] != quality['rounds_used']:
            return ['Plan-quality stage counter/current round is stale']
        if not supervisor.state['plan_path'] or quality['assessment_ref'] is None:
            return ['Current plan-quality assessment missing']
        current_plan = supervisor.store.read(supervisor.state['plan_path'], 'agent-plan.schema.json')
        if plan is not None and digest(plan) != digest(current_plan):
            return ['Selected plan no longer matches authoritative current plan']
        plan = current_plan
        _validate_plan(supervisor, plan)
        if quality['phase'] != 'approved' or quality['active_plan_digest'] != digest(plan):
            return ['Current plan-quality assessment is not approved/current']
        request = _current_request(supervisor)
        input_value = _input(supervisor, plan, request)
        input_sha = _hash(_bytes(input_value))
        input_ref = {'path': 'context/required-' + input_sha + '.json', 'sha256': input_sha}
        if _read(supervisor, input_ref) != input_value:
            return ['Plan-quality evaluator input does not match current mission/plan']
        binding = _binding(supervisor, plan, input_sha)
        # Quality currentness is independent of healthy concurrent workers.
        # Overall acceptance/root lifecycle guards still reconcile every role.
        accounting = guard_invocations(supervisor, node_ids={'@plan-quality'})
        records = {r['record_id']: r for r in accounting['records']}
        record = records.get(quality['assessment_ref'])
        if record is None or record['schema_version'] != 2 or record['record_type'] != 'plan_quality_assessment' or record['node_id'] != '@plan-quality':
            return ['Plan-quality assessment receipt missing or wrong kind/version']
        wire, actual_input, actual_binding = _verified_assessment_record(supervisor, record, accounting)
        if actual_input != input_value or actual_binding != binding or wire['verdict'] != 'approved':
            return ['Plan-quality assessment does not match current mission/contract/plan/policy/round']
        if _unchanged_rejected_plan(supervisor, plan, accounting=accounting):
            return ['Plan-quality approval repeats previously rejected unchanged task content']
        return []
    except (HarnessError, OSError, ValueError, KeyError, TypeError) as error:
        return ['Plan-quality evidence unavailable: ' + str(error)]


def quality_status(supervisor):
    if not enabled(supervisor):
        return {'status': 'legacy_not_assessed', 'policy_version': None, 'passed': False,
            'rounds_used': None, 'max_rounds': None, 'plan_revision': None, 'assessment_ref': None}
    quality = supervisor.state['plan_quality']
    used = quality['rounds_used']
    counter_current = False
    try:
        read_policy(supervisor)
        used = len(_round_events(supervisor))
        counter_current = used == quality['rounds_used']
        errors = evidence_errors(supervisor)
    except (HarnessError, OSError, ValueError, KeyError, TypeError) as error:
        errors = [str(error)]
    status = ('current' if not errors else 'missing' if quality['assessment_ref'] is None else 'stale')
    return {'status': status, 'policy_version': 1, 'passed': not errors, 'phase': quality['phase'],
        'rounds_used': used, 'counter_current': counter_current,
        'max_rounds': min(quality['max_rounds'], supervisor.contract['budgets']['max_attempts']),
        'plan_revision': quality['plan_revision'], 'assessment_ref': quality['assessment_ref'], 'errors': errors}


def publish_creation(supervisor, material):
    """Prepared immutable new-run intent precedes contract/pins/first checkpoint."""
    from .strict_yaml import MAX_BYTES
    document = {'schema_version': 1, 'contract': supervisor.contract, 'material': material}
    if len(_bytes(document)) > MAX_BYTES:
        raise HarnessError('Plan-quality creation material exceeds bounded storage')
    ref = _publish(supervisor, document)
    value = {'kind': 'plan_quality_creation_prepared', 'document_ref': ref,
        'contract_digest': digest(supervisor.contract), 'state_digest': digest(material['state'])}
    previous = [event['payload'] for event in supervisor.store.events() if event['payload'].get('kind') == value['kind']]
    if previous and (len(previous) != 1 or previous[0] != value):
        raise HarnessError('Prepared plan-quality creation conflict')
    if not previous:
        supervisor.store.append_event(value)
    _finish_creation(supervisor, document)


def _finish_creation(supervisor, document):
    contract = document['contract']
    if supervisor.store.path('contract.yaml').exists():
        if supervisor.store.read('contract.yaml', 'work-contract.schema.json') != contract:
            raise HarnessError('Prepared creation contract bytes changed')
    else:
        supervisor.store.write('contract.yaml', contract, 'work-contract.schema.json', exclusive=True)
    publish_bootstrap(supervisor.store, document['material'])
    supervisor.state = deepcopy(document['material']['state'])
    supervisor._save('prepared plan-quality creation committed; original identity/deadline retained')


def recover_creation(supervisor, contract, cwd, *, max_rounds=None):
    """Only explicit same-create retry recovers a prepared headless creation."""
    from .core import snapshot
    from .work_contract import legacy_contract, source_revision
    from .file_io import same_directory
    events = [event['payload'] for event in supervisor.store.events()]
    prepared = [event for event in events if event.get('kind') == 'plan_quality_creation_prepared']
    if not prepared or any(event.get('kind') == 'checkpoint' for event in events):
        return False
    if len(prepared) != 1 or any(event.get('kind') == 'execution_receipt' for event in events):
        raise HarnessError('Prepared creation authority ambiguous; preserve state for recovery')
    document = _read(supervisor, prepared[0]['document_ref'])
    state = document['material']['state']
    validate(state, STATE_SCHEMA)
    if (document['schema_version'] != 1 or document['contract'] != contract
            or prepared[0]['contract_digest'] != digest(contract) or prepared[0]['state_digest'] != digest(state)
            or state['status'] != 'NEW' or state['attempts_used'] != 0 or state['agents'] or state['plan_path'] is not None
            or not same_directory(state['cwd'], cwd) or not same_directory(state['source_cwd'], cwd)
            or not same_directory(state['state_dir'], supervisor.state_dir)
            or max_rounds is not None and (type(max_rounds) is not int or max_rounds != state['plan_quality']['max_rounds'])):
        raise HarnessError('Explicit creation recovery input binding conflict')
    source = legacy_contract(contract, cwd=cwd, run_id=state['run_id'], attempt=1, deadline=state['deadline'])
    if source_revision(cwd) != state['revision'] or snapshot(source)['digest'] != state['baseline_digest']:
        raise HarnessError('Prepared creation original source changed; no fresh identity or deadline')
    _finish_creation(supervisor, document)
    return True


def _round_events(supervisor):
    events = [event['payload'] for event in supervisor.store.events() if event['payload'].get('kind') == 'plan_quality_round']
    if len(events) > supervisor.state['plan_quality']['max_rounds'] or any(
            type(event.get('round')) is not int or event['round'] != index for index, event in enumerate(events, 1)):
        raise HarnessError('Plan-quality stage counter/history conflict')
    return events


def reconcile_rounds(supervisor):
    """Recover derived stage fields from durable reservation, never reset them."""
    if not enabled(supervisor):
        return
    events = _round_events(supervisor)
    quality = supervisor.state['plan_quality']
    if quality['rounds_used'] > len(events):
        raise HarnessError('Plan-quality counter lacks authoritative reservation')
    if quality['rounds_used'] < len(events):
        request = _read(supervisor, events[-1]['request_ref'])
        if digest(request) != events[-1]['request_digest'] or request['contract_digest'] != supervisor.state['contract_digest']:
            raise HarnessError('Interrupted plan-quality reservation changed subject')
        quality.update(rounds_used=len(events), active_round=request['round'], plan_revision=request['plan_revision'],
            active_plan_digest=None, assessment_ref=None, phase='planning')
        supervisor.state['plan_path'] = request['retained_plan_path']
        supervisor._save('recover exact reserved planning stage without a second round')


def _reserve_round(supervisor, *, feedback=(), retained_plan_path=None, same_revision=False):
    from .task_packets import _publish as publish
    quality = supervisor.state['plan_quality']
    events = _round_events(supervisor)
    if quality['rounds_used'] != len(events):
        raise HarnessError('Reconcile interrupted stage reservation before new planning')
    if supervisor.remaining() <= 0:
        _stop(supervisor, 'TIMED_OUT', 'Original deadline expired before planning round')
        return None
    limit = min(quality['max_rounds'], supervisor.contract['budgets']['max_attempts'])
    if len(events) >= limit:
        _stop(supervisor, 'FAILED_TERMINAL', 'Original planning round allowance exhausted')
        return None
    explicit_ref = None
    explicit = getattr(supervisor, 'plan_input', None) if retained_plan_path is None else None
    if explicit:
        raw = read_regular_file(explicit, max_bytes=2 * 1024 * 1024)
        artifact = publish(supervisor, raw, '.plan.yaml')
        explicit_ref = {'path': Path(artifact['path']).relative_to(supervisor.path).as_posix(), 'sha256': artifact['sha256']}
    request = {'schema_version': 1, 'round': len(events) + 1,
        'plan_revision': quality['plan_revision'] if same_revision else quality['plan_revision'] + 1,
        'contract_digest': supervisor.state['contract_digest'], 'mission_digest': digest(supervisor.contract['mission']),
        'policy_digest': quality['policy_ref']['digest'], 'explicit_ref': explicit_ref,
        'retained_plan_path': retained_plan_path, 'feedback': [str(value)[:2048] for value in feedback][:32]}
    ref = _publish(supervisor, request)
    supervisor.store.append_event({'kind': 'plan_quality_round', 'round': request['round'],
        'request_ref': ref, 'request_digest': digest(request)})
    quality.update(rounds_used=request['round'], active_round=request['round'], plan_revision=request['plan_revision'],
        active_plan_digest=None, assessment_ref=None, phase='planning')
    supervisor.state['plan_path'] = retained_plan_path
    supervisor._save('planning round reserved before deterministic validation; original allowance retained')
    return request


def _stop(supervisor, status, reason):
    supervisor.state['plan_quality']['phase'] = 'blocked'
    supervisor._save(reason)
    if status == 'NO_PROGRESS':
        supervisor.transition('FAILED_REPAIRABLE', reason)
    supervisor.transition(status, reason)


def _round_result(supervisor, request, *, plan_digest=None, feedback=(), verdict='requires_revision', signature=None):
    value = {'kind': 'plan_quality_round_result', 'round': request['round'], 'contract_digest': request['contract_digest'],
        'plan_digest': plan_digest, 'verdict': verdict, 'feedback': list(feedback)}
    value['signature'] = signature or digest([value['plan_digest'], value['verdict'], value['feedback']])
    previous = [event['payload'] for event in supervisor.store.events() if event['payload'].get('kind') == value['kind']]
    exact = [event for event in previous if event['round'] == request['round']]
    if exact:
        if len(exact) != 1 or exact[0] != value:
            raise HarnessError('Plan-quality round result conflict')
    else:
        supervisor.store.append_event(value)
    earlier = [event for event in previous if event['round'] < request['round']]
    return bool(earlier and earlier[-1]['signature'] == value['signature'] and earlier[-1]['contract_digest'] == value['contract_digest'])


def _bind_plan(supervisor, plan):
    _validate_plan(supervisor, plan)
    quality = supervisor.state['plan_quality']
    if quality['active_plan_digest'] is not None and quality['active_plan_digest'] != digest(plan):
        raise HarnessError('Plan-quality proposed plan changed after binding')
    if quality['active_plan_digest'] is None:
        quality['active_plan_digest'] = digest(plan)
        supervisor._save('plan-quality round selected exact immutable proposed plan')


def _proposal(supervisor, capabilities, request):
    from .agent_lifecycle import guard_invocations
    from .managed_review import _raw_verdict
    from .strict_yaml import load
    from .managed_execution import _plan, _plan_path
    if supervisor.state['plan_path']:
        return supervisor.store.read(supervisor.state['plan_path'], 'agent-plan.schema.json')
    if request['retained_plan_path']:
        supervisor.state['plan_path'] = request['retained_plan_path']
        return supervisor.store.read(request['retained_plan_path'], 'agent-plan.schema.json')
    accounting = guard_invocations(supervisor)
    refs = [request['policy_digest'], digest(request), _hash(_bytes(request)), request['mission_digest']]
    intents = [intent for intent in accounting['intents'].values() if intent['node_id'] == '@planner'
        and intent['attempt_id'] == 'planner-' + str(request['round']) and digest(request) in intent['input_refs']]
    if len(intents) > 1:
        raise HarnessError('Multiple planner invocations for one reserved round')
    if intents:
        completion = accounting['completions'][intents[0]['invocation_id']]
        if completion['payload']['outcome'] != 'completed':
            raise HarnessError('Known stopped planner did not produce a successful plan')
        logs = completion['payload']['logs']
        raw = read_regular_file(logs['stdout']) if logs['stdout'] else None
        if raw is None or _hash(raw) != logs['stdout_digest']:
            raise HarnessError('Planner exact raw completion unavailable or changed')
        # Generic bounded parser validates against the frozen concrete plan2
        # output schema, not the final-review or assessment schema.
        from .task_packets import schema_file
        plan = _raw_verdict(raw, schema_file(supervisor, 'agent-plan.schema.json', 2))
        _validate_plan(supervisor, plan)
        relative = _plan_path(supervisor)
        if supervisor.store.path(relative).exists():
            if supervisor.store.read(relative, 'agent-plan.schema.json') != plan:
                raise HarnessError('Recovered proposed plan conflicts with immutable artifact')
        else:
            supervisor.store.write(relative, plan, 'agent-plan.schema.json', exclusive=True)
        supervisor.store.append_event({'kind': 'plan_frozen', 'plan_digest': digest(plan),
            'mission_generation': supervisor.contract['mission']['generation'], 'quality_round': request['round']})
        supervisor.state['plan_path'] = relative
        supervisor._save('recover exact planner completion into original reserved round; no new call')
        return plan
    explicit = request['explicit_ref']
    if explicit:
        relative = explicit['path'].replace('\\', '/')
        if relative != 'context/required-' + explicit['sha256'] + '.plan.yaml':
            raise HarnessError('Explicit plan escaped immutable input ownership')
        raw = read_regular_file(supervisor.store.path(relative), max_bytes=2 * 1024 * 1024)
        if _hash(raw) != explicit['sha256']:
            raise HarnessError('Frozen explicit plan changed')
        supervisor.plan_input = str(supervisor.store.path(relative))
    else:
        supervisor.plan_input = None
    supervisor.plan_quality_request = request
    supervisor.plan_quality_input_refs = refs
    return _plan(supervisor, capabilities)


def select_plan(supervisor, capabilities):
    """Bounded complete planning/replanning stage before any worker admission."""
    from .agent_lifecycle import guard_invocations, InvocationUnresolved
    read_policy(supervisor)
    reconcile_rounds(supervisor)
    quality = supervisor.state['plan_quality']
    request = None
    if quality['active_round'] is not None:
        request = _current_request(supervisor)
    else:
        request = _reserve_round(supervisor, retained_plan_path=supervisor.state['plan_path'])
    while request is not None:
        try:
            plan = _proposal(supervisor, capabilities, request)
            _bind_plan(supervisor, plan)
        except InvocationUnresolved:
            raise
        except (HarnessError, OSError, ValueError, KeyError, TypeError) as error:
            guard_invocations(supervisor)  # Unknown side effect never replans.
            feedback = ['Deterministic plan validation failed: ' + str(error)[:1800]]
            repeated = _round_result(supervisor, request, feedback=feedback)
            if request['explicit_ref'] or request['retained_plan_path']:
                _stop(supervisor, 'BLOCKED_AMBIGUOUS', feedback[0])
                return None
            if repeated:
                _stop(supervisor, 'NO_PROGRESS', 'Unchanged planning failure without new hypothesis: ' + feedback[0])
                return None
            supervisor.plan_input = None
            request = _reserve_round(supervisor, feedback=feedback)
            continue
        try:
            if _unchanged_rejected_plan(supervisor, plan):
                _stop(supervisor, 'NO_PROGRESS', 'Rejected outcome/check/dependency/scope content unchanged; revision or session is not a new hypothesis')
                return None
            wire = assess(supervisor, plan)
        except InvocationUnresolved:
            raise
        except AssessmentStopped as error:
            failure = error.failure
            feedback = ['Known stopped plan-quality reviewer: ' + failure['category'] + ' ' + failure['detail']
                + ' Exact completion: ' + error.completion_ref]
            repeated = _round_result(supervisor, request, plan_digest=digest(plan), feedback=feedback,
                signature=digest([digest(plan), failure['signature']]))
            if not failure['repairable']:
                from .failure_policy import repair_decision
                decision = repair_decision([], failure, digest(plan), attempts=quality['rounds_used'], max_attempts=quality['max_rounds'])
                _stop(supervisor, decision['status'], 'Plan-quality reviewer stopped: ' + failure['detail'])
                return None
            if repeated:
                _stop(supervisor, 'NO_PROGRESS', 'Same plan-quality reviewer failure without new hypothesis')
                return None
            request = _reserve_round(supervisor, feedback=feedback,
                retained_plan_path=supervisor.state['plan_path'], same_revision=True)
            continue
        except (HarnessError, OSError, ValueError, KeyError, TypeError) as error:
            guard_invocations(supervisor)
            _stop(supervisor, 'BLOCKED_AMBIGUOUS', 'Plan-quality evidence cannot authorize work: ' + str(error))
            return None
        if wire is None:
            return None
        if wire['verdict'] == 'approved':
            errors = evidence_errors(supervisor, plan)
            if errors:
                _stop(supervisor, 'BLOCKED_AMBIGUOUS', '; '.join(errors))
                return None
            return plan
        feedback = [finding['reason'] + ' Revision: ' + finding['revision_request'] for finding in wire['findings']]
        repeated = _round_result(supervisor, request, plan_digest=digest(plan), feedback=feedback, verdict=wire['verdict'])
        if wire['verdict'] == 'blocked':
            _stop(supervisor, 'BLOCKED_AMBIGUOUS', 'Plan-quality reviewer blocked: ' + '; '.join(feedback))
            return None
        if repeated:
            _stop(supervisor, 'NO_PROGRESS', 'Same plan/assessment rejection without new hypothesis')
            return None
        supervisor.plan_input = None
        request = _reserve_round(supervisor, feedback=feedback)
    return None


def _semantic_fingerprint(plan, contract, policy):
    mission = deepcopy(contract['mission'])
    mission.pop('generation', None)
    mission.pop('previous_digest', None)
    return digest({'tasks': plan['tasks'], 'mission': mission, 'node_checks': contract['node_checks'],
        'semantic_policy': {key: policy[key] for key in ('policy_id', 'deterministic_obligations', 'semantic_obligations')}})


def _verified_assessment_record(supervisor, record, accounting):
    """Verify historical/current producer chain against its actual frozen input."""
    from types import SimpleNamespace
    from .agent_lifecycle import post_invocation_guard
    from .managed_review import _raw_verdict
    if record['schema_version'] != 2 or record['record_type'] != 'plan_quality_assessment' or record['node_id'] != '@plan-quality':
        raise HarnessError('Plan-quality receipt kind/version subject mismatch')
    value = record['payload']
    input_ref = {'path': 'context/required-' + value['input_digest'] + '.json', 'sha256': value['input_digest']}
    actual_input = _read(supervisor, input_ref)
    if (set(actual_input) != {'schema_version', 'purpose', 'contract', 'mission', 'plan', 'policy', 'mission_generation',
            'plan_revision', 'round', 'feedback', 'source'} or actual_input['schema_version'] != 1
            or actual_input['purpose'] != 'plan_quality'):
        raise HarnessError('Plan-quality actual evaluator input shape changed')
    contract, plan, policy = actual_input['contract'], actual_input['plan'], actual_input['policy']
    validate(contract, 'work-contract.schema.json')
    validate(policy, POLICY_SCHEMA)
    if contract['schema_version'] != 2 or contract['mode'] != 'managed' or actual_input['mission'] != contract['mission']:
        raise HarnessError('Plan-quality actual evaluator input contract/mission mismatch')
    binding = {'mission_digest': digest(contract['mission']), 'mission_generation': contract['mission']['generation'],
        'contract_digest': digest(contract), 'plan_revision': actual_input['plan_revision'], 'plan_digest': digest(plan),
        'policy_version': 1, 'policy_digest': digest(policy), 'input_digest': value['input_digest'], 'round': actual_input['round']}
    if actual_input['mission_generation'] != binding['mission_generation']:
        raise HarnessError('Plan-quality evaluator mission generation mismatch')
    round_events = _round_events(supervisor)
    if type(binding['round']) is not int or not 1 <= binding['round'] <= len(round_events):
        raise HarnessError('Plan-quality receipt lacks its authoritative stage reservation')
    request_event = round_events[binding['round'] - 1]
    request = _read(supervisor, request_event['request_ref'])
    if (digest(request) != request_event['request_digest'] or request['contract_digest'] != binding['contract_digest']
            or request['mission_digest'] != binding['mission_digest'] or request['policy_digest'] != binding['policy_digest']
            or request['plan_revision'] != binding['plan_revision'] or request['feedback'] != actual_input['feedback']):
        raise HarnessError('Plan-quality evaluator input differs from reserved stage')
    schema_sha = policy['output_schema_digest']
    schema_path = 'context/required-' + schema_sha + '.schema.json'
    if _hash(read_regular_file(supervisor.store.path(schema_path))) != schema_sha:
        raise HarnessError('Plan-quality bound historical schema changed')
    view = SimpleNamespace(contract=contract, store=supervisor.store,
        state={'contract_digest': binding['contract_digest'], 'plan_quality': {'policy_ref': {'output_schema_ref': {'path': schema_path, 'sha256': schema_sha}}}})
    records = {r['record_id']: r for r in accounting['records']}
    completion = records.get(value['completion_ref'])
    intent = accounting['intents'].get(record['invocation_id'])
    if completion is None or intent is None:
        raise HarnessError('Plan-quality receipt invocation evidence missing')
    post_invocation_guard(intent, completion, accounting)
    if (completion['payload']['outcome'] != 'completed' or intent['node_id'] != '@plan-quality'
            or intent['attempt_id'] != 'plan-quality-' + str(binding['round'])
            or intent['input_refs'] != _refs(view, binding) or record['input_refs'] != intent['input_refs']
            or any(record[key] != completion[key] for key in ('plan_digest', 'attempt_id', 'invocation_id', 'created_at', 'generation', 'harness_build_digest'))
            or record['record_id'] != 'plan-quality-' + digest([intent['invocation_id'], binding])[:48]):
        raise HarnessError('Plan-quality receipt invocation/input identity changed')
    wire = _read(supervisor, {'path': value['assessment_ref'], 'sha256': value['assessment_digest']})
    _validate_assessment(view, plan, wire, binding)
    expected = {key: binding[key] for key in ('mission_digest', 'contract_digest', 'plan_digest', 'policy_digest', 'input_digest', 'round')}
    expected.update(completion_ref=completion['record_id'], output_schema_digest=schema_sha, verdict=wire['verdict'],
        assessment_ref=value['assessment_ref'], assessment_digest=value['assessment_digest'])
    logs = completion['payload']['logs']
    raw = read_regular_file(logs['stdout']) if logs['stdout'] else None
    if (value != expected or raw is None or _hash(raw) != logs['stdout_digest']
            or _raw_verdict(raw, supervisor.store.path(schema_path)) != wire
            or _hash(_bytes(wire)) != value['assessment_digest']):
        raise HarnessError('Plan-quality receipt/materialization differs from exact raw reviewer verdict')
    return wire, actual_input, binding


def _unchanged_rejected_plan(supervisor, plan, *, accounting=None):
    from .agent_lifecycle import guard_invocations
    accounting = accounting if accounting is not None else guard_invocations(supervisor, node_ids={'@plan-quality'})
    fingerprint = _semantic_fingerprint(plan, supervisor.contract, read_policy(supervisor))
    active = supervisor.state['plan_quality']['active_round']
    for record in accounting['records']:
        if record['record_type'] != 'plan_quality_assessment' or record['payload']['verdict'] != 'requires_revision' or record['payload']['round'] >= active:
            continue
        _, actual_input, _ = _verified_assessment_record(supervisor, record, accounting)
        if _semantic_fingerprint(actual_input['plan'], actual_input['contract'], actual_input['policy']) == fingerprint:
            return True
    return False
