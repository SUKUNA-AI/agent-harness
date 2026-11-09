"""Typed pinned benchmark preparation protocol; never execution authorization.

Validation establishes the declared local bytes and internal relationships. It
cannot attest upstream authenticity, fresh isolation controls, observed policy,
or grant budget authority. The observed adapter registry remains separate.
"""
from pathlib import Path
import hashlib

from . import evaluation, strict_yaml
from .core import HarnessError, digest
from .execution_ledger import HASH, SHORT, object_schema
from .file_io import read_regular_file
from .supervisor_store import SupervisorStore

REF = evaluation.FILE_REF
POSITIVE = {"type": "integer", "minimum": 1}
REVISION = {"type": "string", "pattern": "^[a-f0-9]{40}([a-f0-9]{24})?$"}
IMAGE_DIGEST = {"type": "string", "pattern": "^sha256:[a-f0-9]{64}$"}
ROLE = object_schema({"role": SHORT, "model": SHORT, "provider": SHORT, "reasoning_effort": SHORT,
    "max_tokens": POSITIVE, "max_compaction_tokens": POSITIVE},
    ("role", "model", "provider", "reasoning_effort", "max_tokens", "max_compaction_tokens"))
ROLE_MAPPING = object_schema({'schema_version':{'const':1},'record_type':{'const':'benchmark_role_policy_observations'},
    'backend_build_digest':HASH,'coverage':{'enum':['complete','partial','unknown']},
    'method':{'enum':['runtime_query','client_runtime_metadata']},
    'roles':{'type':'array','maxItems':32,'items':object_schema({**ROLE['properties'],'evidence_ref':REF},(*ROLE['required'],'evidence_ref'))}},
    ('schema_version','record_type','backend_build_digest','coverage','method','roles'))
ROOT_POLICY = object_schema({"schema_version": {"const": 1}, "record_type": {"const": "benchmark_root_policy"},
    "model": SHORT, "provider": SHORT, "client_version": SHORT, "reasoning_effort": SHORT,
    "max_tokens": POSITIVE, "max_compaction_tokens": POSITIVE},
    ("schema_version", "record_type", "model", "provider", "client_version", "reasoning_effort", "max_tokens", "max_compaction_tokens"))
CHILD_POLICY = object_schema({"schema_version": {"const": 1}, "record_type": {"const": "benchmark_child_policy"},
    "roles": {"type": "array", "minItems": 1, "maxItems": 32, "items": ROLE},
    "observed_role_mapping_ref": {"anyOf": [REF, {"type": "null"}]}},
    ("schema_version", "record_type", "roles", "observed_role_mapping_ref"))
RESOURCES = object_schema({"schema_version": {"const": 1}, "record_type": {"const": "benchmark_resources"},
    "cpus": {"type": "number", "exclusiveMinimum": 0}, "memory_mb": POSITIVE, "storage_mb": POSITIVE,
    "pids": POSITIVE, "max_trial_wall_seconds": POSITIVE, "max_concurrent_trials": POSITIVE},
    ("schema_version", "record_type", "cpus", "memory_mb", "storage_mb", "pids", "max_trial_wall_seconds", "max_concurrent_trials"))
IMAGE = object_schema({"suite": {"enum": list(evaluation.SUITES)}, "image": SHORT, "digest": IMAGE_DIGEST,
    "platform": SHORT, "preparation_ref": REF}, ("suite", "image", "digest", "platform", "preparation_ref"))
IMAGES = object_schema({"schema_version": {"const": 1}, "record_type": {"const": "benchmark_images"},
    "images": {"type": "array", "minItems": 3, "maxItems": 10000, "items": IMAGE}},
    ("schema_version", "record_type", "images"))
IMAGE_PREPARATION = object_schema({"schema_version": {"const": 1}, "record_type": {"const": "benchmark_image_preparation"},
    "image_digest": IMAGE_DIGEST, "platform": SHORT, "toolchain_ref": REF, "workspace_inventory_ref": REF},
    ("schema_version", "record_type", "image_digest", "platform", "toolchain_ref", "workspace_inventory_ref"))
NETWORK = object_schema({"schema_version": {"const": 1}, "record_type": {"const": "benchmark_network_policy"},
    "agent_egress": {"const": "deny"}, "web_search": {"const": "deny"}, "package_fetch": {"const": "deny"},
    "model_transport": object_schema({"provider": SHORT, "route_ref": REF}, ("provider", "route_ref")),
    "boundary_profile_ref": REF},
    ("schema_version", "record_type", "agent_egress", "web_search", "package_fetch", "model_transport", "boundary_profile_ref"))
ROUTE = object_schema({"schema_version": {"const": 1}, "record_type": {"const": "benchmark_model_route"},
    "provider": SHORT, "channel": {"const": "separate_controlled_transport"}, "backend_identity_ref": REF},
    ("schema_version", "record_type", "provider", "channel", "backend_identity_ref"))
STATE = object_schema({"schema_version": {"const": 1}, "record_type": {"const": "benchmark_state_policy"},
    "trial_checkout": {"const": "fresh"}, "client_home": {"const": "per_trial_per_arm"},
    "memory_cohort": {"enum": ["cold", "warm_repeated_task"]}, "peer_state": {"const": "deny"},
    "provider_prompt_cache": {"enum": ["uncontrolled_observable_limitation", "qualified_control"]},
    "reset_profile_ref": REF},
    ("schema_version", "record_type", "trial_checkout", "client_home", "memory_cohort", "peer_state", "provider_prompt_cache", "reset_profile_ref"))
BOUNDARY_PROFILE = object_schema({"schema_version": {"const": 1}, "record_type": {"const": "benchmark_boundary_profile"},
    "backend_id": SHORT, "backend_build_digest": HASH, "filesystem": {"const": "deny_peer_and_controller"},
    "network": {"const": "deny_agent_egress"}, "evaluator": {"const": "separate_from_solve"}},
    ("schema_version", "record_type", "backend_id", "backend_build_digest", "filesystem", "network", "evaluator"))
RESET_PROFILE = object_schema({"schema_version": {"const": 1}, "record_type": {"const": "benchmark_reset_profile"},
    "home": {"const": "exclusive_empty"}, "history": {"const": "none"}, "cross_arm_cache": {"const": "deny"},
    "harness_installation": {"enum": ["arm_specific_pinned_inventory"]}},
    ("schema_version", "record_type", "home", "history", "cross_arm_cache", "harness_installation"))
TOOLCHAIN = object_schema({"schema_version": {"const": 1}, "record_type": {"const": "benchmark_toolchain"},
    "revision": REVISION, "python": SHORT, "tools": {"type": "array", "minItems": 1, "maxItems": 64, "items": object_schema({"name": SHORT, "version": SHORT, "artifact": REF}, ("name", "version", "artifact"))}},
    ("schema_version", "record_type", "revision", "python", "tools"))
WORKSPACE = object_schema({"schema_version": {"const": 1}, "record_type": {"const": "benchmark_workspace_inventory"},
    "files": {"type": "array", "maxItems": 10000, "items": object_schema({"path": SHORT, "asset": REF,'mode':{'enum':['100644','100755']}}, ("path", "asset"))}},
    ("schema_version", "record_type", "files"))
COHORT = object_schema({"schema_version": {"const": 1}, "record_type": {"const": "benchmark_cohort"},
    "suite": {"enum": list(evaluation.SUITES)}, "dataset_revision": REVISION,
    "selection": {"enum": ["selected_full_pinned_index", "custom_selection", "synthetic_fixture"]}, "split": SHORT,
    "declared_total": POSITIVE, "tasks": {"type": "array", "minItems": 1, "maxItems": 10000, "items": evaluation.TASK}},
    ("schema_version", "record_type", "suite", "dataset_revision", "selection", "split", "declared_total", "tasks"))
EVALUATOR = object_schema({"schema_version": {"const": 1}, "record_type": {"const": "benchmark_upstream_evaluator"},
    "suite": {"enum": list(evaluation.SUITES[:3])}, "revision": REVISION,
    "protocol": {"enum": ["swebench-jsonl-v1", "harbor-task-v1", "aider-exercism-v1"]},
    "entrypoint": REF, "helpers": {"type": "array", "maxItems": 16, "items": object_schema({"name": SHORT, "artifact": REF}, ("name", "artifact"))}},
    ("schema_version", "record_type", "suite", "revision", "protocol", "entrypoint", "helpers"))
PROFILE_SUITE = object_schema({"suite": {"enum": list(evaluation.SUITES[:3])}, "cohort_ref": REF, "evaluator_ref": REF},
    ("suite", "cohort_ref", "evaluator_ref"))
PROFILE = object_schema({"schema_version": {"const": 1}, "record_type": {"const": "benchmark_prepared_profile"},
    "profile_id": SHORT, **{name: REF for name in ("resources_ref", "images_ref", "network_ref", "state_ref", "root_policy_ref", "child_policy_ref")},
    "suites": {"type": "array", "minItems": 3, "maxItems": 3, "items": PROFILE_SUITE}},
    ("schema_version", "record_type", "profile_id", "resources_ref", "images_ref", "network_ref", "state_ref", "root_policy_ref", "child_policy_ref", "suites"))


def checked_bytes(ref, root, *, max_bytes=16 * 1024 * 1024):
    strict_yaml.validate(ref, REF)
    relative = Path(ref['path'])
    if relative.is_absolute() or relative.anchor or '..' in relative.parts or not relative.parts or any(':' in part for part in relative.parts):
        raise HarnessError('BENCH_UNSAFE_ASSET')
    raw = read_regular_file(Path(root).absolute() / relative, max_bytes=max_bytes)
    if hashlib.sha256(raw).hexdigest() != ref['sha256']:
        raise HarnessError('BENCH_STALE_ASSET')
    return raw


def checked_document(ref, root, schema):
    return strict_yaml.loads(checked_bytes(ref, root, max_bytes=2 * 1024 * 1024), schema)


def _identity(value):
    if not value or value.casefold() in {'unknown', 'unavailable', 'installed', 'latest', 'main', 'head', 'tbd'}:
        raise HarnessError('BENCH_UNKNOWN_PROFILE_IDENTITY')


def validate_prepared_profile(ref, assets_root, manifest=None):
    profile = checked_document(ref, assets_root, PROFILE)
    documents = {name: checked_document(profile[name+'_ref'], assets_root, schema) for name,schema in
        [('resources',RESOURCES),('images',IMAGES),('network',NETWORK),('state',STATE),('root_policy',ROOT_POLICY),('child_policy',CHILD_POLICY)]}
    for key in ('model','provider','client_version','reasoning_effort'):
        _identity(documents['root_policy'][key])
    roles = documents['child_policy']['roles']
    if len({row['role'] for row in roles}) != len(roles):
        raise HarnessError('BENCH_DUPLICATE_ROLE_POLICY')
    for role in roles:
        for key in ('role','model','provider','reasoning_effort'):
            _identity(role[key])
    if documents['child_policy']['observed_role_mapping_ref'] is not None:
        # Runtime interpretation/qualification belongs to the actual backend;
        # a pinned document alone never proves observed role mapping.
        mapping=checked_document(documents['child_policy']['observed_role_mapping_ref'], assets_root, ROLE_MAPPING)
        for item in mapping['roles']:checked_bytes(item['evidence_ref'],assets_root)
    route = checked_document(documents['network']['model_transport']['route_ref'], assets_root, ROUTE)
    boundary = checked_document(documents['network']['boundary_profile_ref'], assets_root, BOUNDARY_PROFILE)
    reset = checked_document(documents['state']['reset_profile_ref'], assets_root, RESET_PROFILE)
    checked_bytes(route['backend_identity_ref'], assets_root)
    if route['provider'] != documents['root_policy']['provider'] or route['provider'] != documents['network']['model_transport']['provider']:
        raise HarnessError('BENCH_MODEL_ROUTE_IDENTITY_CONFLICT')
    images = []
    for image in documents['images']['images']:
        preparation = checked_document(image['preparation_ref'], assets_root, IMAGE_PREPARATION)
        if preparation['image_digest'] != image['digest'] or preparation['platform'] != image['platform']:
            raise HarnessError('BENCH_IMAGE_PREPARATION_CONFLICT')
        toolchain = checked_document(preparation['toolchain_ref'], assets_root, TOOLCHAIN)
        workspace = checked_document(preparation['workspace_inventory_ref'], assets_root, WORKSPACE)
        for tool in toolchain['tools']:
            checked_bytes(tool['artifact'], assets_root)
        for item in workspace['files']:
            checked_bytes(item['asset'], assets_root)
        images.append({'image':image,'preparation':preparation,'toolchain':toolchain,'workspace':workspace})
    expected = set(evaluation.SUITES[:3])
    if {item['suite'] for item in profile['suites']} != expected or not expected <= {item['image']['suite'] for item in images}:
        raise HarnessError('BENCH_INCOMPLETE_PREPARED_PORTFOLIO')
    suites = {}
    protocols = dict(zip(evaluation.SUITES[:3], ('swebench-jsonl-v1','harbor-task-v1','aider-exercism-v1')))
    for item in profile['suites']:
        cohort = checked_document(item['cohort_ref'], assets_root, COHORT)
        evaluator = checked_document(item['evaluator_ref'], assets_root, EVALUATOR)
        if cohort['suite'] != item['suite'] or evaluator['suite'] != item['suite'] or evaluator['protocol'] != protocols[item['suite']]:
            raise HarnessError('BENCH_PREPARED_SUITE_BINDING_CONFLICT')
        ids = [task['id'] for task in cohort['tasks']]
        if len(set(ids)) != len(ids) or len(ids) != cohort['declared_total']:
            raise HarnessError('BENCH_COHORT_MEMBERSHIP_CONFLICT')
        from .benchmark_importers import RAW_SOURCE
        for task in cohort['tasks']:
            source=checked_document(task['source'],assets_root,RAW_SOURCE)
            if (source['task_id']!=task['id'] or source['suite']!=item['suite'] or
                source['dataset_revision']!=cohort['dataset_revision'] or source['evaluator_revision']!=evaluator['revision']):
                raise HarnessError('BENCH_COHORT_SOURCE_SUBJECT_CONFLICT')
        checked_bytes(evaluator['entrypoint'], assets_root)
        for helper in evaluator['helpers']:
            checked_bytes(helper['artifact'], assets_root)
        suites[item['suite']] = {'cohort':cohort,'evaluator':evaluator}
    if manifest is not None:
        for key in ('model','provider','client_version'):
            if manifest[key] != documents['root_policy'][key]:
                raise HarnessError('BENCH_PROFILE_CLIENT_CONFLICT')
        if manifest['root_policy'] != profile['root_policy_ref'] or manifest['child_policy'] != profile['child_policy_ref']:
            raise HarnessError('BENCH_PROFILE_POLICY_BINDING_CONFLICT')
        for suite in manifest['suites']:
            if suite['id'] in suites and suite['tasks'] != suites[suite['id']]['cohort']['tasks']:
                raise HarnessError('BENCH_COHORT_MEMBERSHIP_CONFLICT')
            if suite['id'] in suites and (suite['revision']!=suites[suite['id']]['cohort']['dataset_revision'] or suite['evaluator']!=suites[suite['id']]['evaluator']['entrypoint']):
                raise HarnessError('BENCH_PROFILE_EVALUATOR_REVISION_CONFLICT')
    return {'profile':profile,'profile_ref':ref,'assets_root':str(Path(assets_root).absolute()),'profile_digest':digest(profile),'documents':documents,'suites':suites,'images':images,
        'qualification':'pinned_declared_preparation_only','isolation_qualified':False,'budget_authority':False,
        'limitations':['Upstream authenticity and full official split membership need pinned authoritative collection evidence',
            'Fresh runtime boundary controls and observed role mapping remain separate qualification',
            'Budget files do not authorize model expenditure']}


def build_prepared_profile(destination, *, profile_id, resources, images, network, state, root_policy, child_policy, suites):
    """Persist typed reference documents; never approves spend or executes IO tools."""
    root = Path(destination).absolute()
    store = SupervisorStore(root)
    with store.lease('build typed benchmark preparation profile'):
        # Dataset/image references already live in this asset root. Exclusive
        # publication preserves them and refuses an interrupted profile replay.
        refs = {}
        for name,value,schema in [('resources',resources,RESOURCES),('images',images,IMAGES),('network',network,NETWORK),('state',state,STATE),('root_policy',root_policy,ROOT_POLICY),('child_policy',child_policy,CHILD_POLICY)]:
            path = store.write(name+'.yaml',value,schema,exclusive=True)
            refs[name+'_ref'] = {'path':path.name,'sha256':hashlib.sha256(read_regular_file(path)).hexdigest()}
        profile = {'schema_version':1,'record_type':'benchmark_prepared_profile','profile_id':profile_id,**refs,'suites':suites}
        path = store.write('profile.yaml',profile,PROFILE,exclusive=True)
    return {'path':path.name,'sha256':hashlib.sha256(read_regular_file(path)).hexdigest()}
