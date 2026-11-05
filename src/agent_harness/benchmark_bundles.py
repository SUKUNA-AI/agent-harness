"""Pinned normalized task bundles and a data-only independent evaluator.

Materialization is useful offline preparation, not isolation qualification. The
concrete adapter needs an explicit execution boundary; the default cannot run.
No official cohort is inferred from a normalized local task or suite label.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys

from . import evaluation, strict_yaml
from .benchmark_runner import CodexBenchmarkAdapter
from .core import HarnessError, digest
from .execution_ledger import HASH, SHORT, object_schema
from .execution_receipts import build_digest
from .file_io import read_regular_file, parent_binding, verify_parent_binding
from .supervisor_store import SupervisorStore, atomic_write, _ordinary
from .validation import json_equal
from .benchmark_git import FRESH_GIT
from copy import deepcopy

OWN_SUITES = ("harness-recovery", "harness-retrieval-long-task")
PATH = {"type": "string", "minLength": 1, "maxLength": 240}
ASSET = evaluation.FILE_REF
FILE = object_schema({"path": PATH, "source": ASSET}, ("path", "source"))
MILESTONE = object_schema({"id": SHORT, "artifact": PATH, "expected": ASSET,
    "fault": {"enum": ["observe", "controller_restart", "after_durable_checkpoint"]}},
    ("id", "artifact", "expected", "fault"))
BUNDLE = object_schema({"schema_version": {"const": 1}, "record_type": {"const": "benchmark_task_bundle"},
    "suite": {"enum": list(evaluation.SUITES)}, "task_id": SHORT, "revision": SHORT,
    "provenance": {"enum": ["project_owned", "normalized_external"]},
    "api_profile": {"const": "artifact-json-v1"}, "license": ASSET, "objective": {"type": "string", "minLength": 1, "maxLength": 16384},
    "files": {"type": "array", "minItems": 1, "maxItems": 256, "items": FILE},
    "writable_paths": {"type": "array", "minItems": 1, "maxItems": 64, "items": PATH},
    "oracle": ASSET, "milestones": {"type": "array", "maxItems": 64, "items": MILESTONE},
    "max_attempts": {"type": "integer", "minimum": 1, "maximum": 10},
    "max_wall_seconds": {"type": "integer", "minimum": 1, "maximum": 86400},
    "turn_timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 86400}},
    ("schema_version", "record_type", "suite", "task_id", "revision", "provenance", "api_profile", "license", "objective", "files", "writable_paths", "oracle", "milestones", "max_attempts", "max_wall_seconds", "turn_timeout_seconds"))
CHECK = object_schema({"path": PATH, "expected": ASSET}, ("path", "expected"))
ORACLE = object_schema({"schema_version": {"const": 1}, "record_type": {"const": "artifact_json_oracle"},
    "checks": {"type": "array", "minItems": 1, "maxItems": 64, "items": CHECK}},
    ("schema_version", "record_type", "checks"))
GRADE_INPUT = object_schema({"schema_version": {"const": 1}, "manifest_digest": HASH,
    "trial_id": HASH, "evaluator_digest": HASH, "solve_digest": HASH, "cwd": SHORT,
    "solve_outcome": {"enum": ["completed", "failed", "cancelled", "timeout", "environment_error", "unknown"]}},
    ("schema_version", "manifest_digest", "trial_id", "evaluator_digest", "solve_digest", "cwd", "solve_outcome"))
INVENTORY = object_schema({"path": PATH, "sha256": HASH}, ("path", "sha256"))
BINDING_ITEM = object_schema({"path": SHORT, "device": {"type": "integer", "minimum": 0},
    "inode": {"type": "integer", "minimum": 0}}, ("path", "device", "inode"))
MATERIALIZED = object_schema({"schema_version": {"const": 1}, "record_type": {"const": "materialized_task_bundle"},
    "manifest_digest": HASH, "trial_id": HASH, "suite": SHORT, "task_id": SHORT,
    "bundle_digest": HASH, "bundle_ref": ASSET, "source_root": SHORT, "evaluator_root": SHORT,
    "source_binding": {"type": "array", "items": BINDING_ITEM},
    "evaluator_binding": {"type": "array", "items": BINDING_ITEM},
    "immutable_files": {"type": "array", "items": INVENTORY},
    "initial_files": {"type": "array", "items": INVENTORY},
    "protected_files": {"type": "array", "items": INVENTORY},
    "writable_paths": {"type": "array", "items": PATH},
    "milestones": {"type": "array", "items": MILESTONE}},
    ("schema_version", "record_type", "manifest_digest", "trial_id", "suite", "task_id", "bundle_digest", "bundle_ref", "source_root", "evaluator_root", "source_binding", "evaluator_binding", "immutable_files", "initial_files", "protected_files", "writable_paths", "milestones"))
MATERIALIZED_V1=MATERIALIZED
MATERIALIZED_V2=deepcopy(MATERIALIZED_V1)
MATERIALIZED_V2['properties']['schema_version']={'const':2}
MATERIALIZED_V2['properties']['fresh_git']=FRESH_GIT
MATERIALIZED_V2['required'].append('fresh_git')
MATERIALIZED={'oneOf':[MATERIALIZED_V1,MATERIALIZED_V2]}


def _deadline(deadline):
    if deadline is not None and datetime.fromisoformat(deadline.replace("Z", "+00:00")) <= datetime.now(timezone.utc):
        raise HarnessError("BENCH_DEADLINE_EXHAUSTED")


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _relative(value):
    path = PurePosixPath(value)
    reserved = {"CON", "PRN", "AUX", "NUL", *["COM" + str(i) for i in range(1, 10)], *["LPT" + str(i) for i in range(1, 10)]}
    if (not value or str(path) != value or path.is_absolute() or
        any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part) or part.casefold() in {".", "..", ".git", ".codex", ".agents"} or part.endswith(".") or part.split(".")[0].upper() in reserved for part in path.parts)):
        raise HarnessError("BENCH_UNSAFE_BUNDLE_PATH")
    return value


def _json(raw):
    # JSON syntax plus strict duplicate/depth/node/nonfinite checks. Comparison
    # below preserves booleans versus numbers, unlike Python's ==.
    json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    return strict_yaml.loads(raw)


def load_bundle(ref, assets_root, *, suite=None, task_id=None, deadline=None):
    _deadline(deadline)
    raw = evaluation.checked_ref(ref, assets_root)
    bundle = strict_yaml.loads(raw, BUNDLE)
    if (suite is not None and bundle["suite"] != suite) or (task_id is not None and bundle["task_id"] != task_id):
        raise HarnessError("BENCH_BUNDLE_SUBJECT_MISMATCH")
    if bundle["provenance"] == "project_owned" and bundle["suite"] not in OWN_SUITES:
        raise HarnessError("BENCH_OWN_CORPUS_NOT_OFFICIAL_SUITE")
    if bundle["revision"].casefold() in {"latest", "main", "head", "unknown", "tbd"}:
        raise HarnessError("BENCH_UNPINNED_BUNDLE_REVISION")
    writable = [_relative(path) for path in bundle["writable_paths"]]
    paths = [_relative(item["path"]) for item in bundle["files"]]
    if len(set(paths)) != len(paths) or len(set(writable)) != len(writable) or len({p.casefold() for p in paths + [p for p in writable if p not in paths]}) != len(set(paths + writable)):
        raise HarnessError("BENCH_DUPLICATE_BUNDLE_PATH")
    allpaths = set(paths + writable)
    if any(any(str(parent) in allpaths for parent in PurePosixPath(path).parents if str(parent) != ".") for path in allpaths):
        raise HarnessError("BENCH_OVERLAPPING_BUNDLE_PATH")
    assets = {"license": evaluation.checked_ref(bundle["license"], assets_root),
              "oracle.yaml": evaluation.checked_ref(bundle["oracle"], assets_root)}
    oracle = strict_yaml.loads(assets["oracle.yaml"], ORACLE)
    source = {item["path"]: evaluation.checked_ref(item["source"], assets_root) for item in bundle["files"]}
    expected = {}
    for index, check in enumerate(oracle["checks"]):
        if _relative(check["path"]) not in writable:
            raise HarnessError("BENCH_ORACLE_OUTSIDE_OUTPUT_SCOPE")
        value = evaluation.checked_ref(check["expected"], assets_root)
        _json(value)
        name = "expected-" + str(index) + ".json"
        assets[name] = value
        expected[check["path"]] = {"path": name, "sha256": _hash(value)}
    if len(expected) != len(oracle["checks"]):
        raise HarnessError("BENCH_DUPLICATE_ORACLE_PATH")
    local_oracle = {**oracle, "checks": [{"path": item["path"], "expected": expected[item["path"]]} for item in oracle["checks"]]}
    assets["oracle.yaml"] = strict_yaml.dumps(local_oracle, ORACLE).encode()
    milestones = []
    for index, item in enumerate(bundle["milestones"]):
        if _relative(item["artifact"]) not in writable:
            raise HarnessError("BENCH_MILESTONE_OUTSIDE_OUTPUT_SCOPE")
        value = evaluation.checked_ref(item["expected"], assets_root)
        _json(value)
        name = "milestone-" + str(index) + ".json"
        assets[name] = value
        milestones.append({**item, "expected": {"path": name, "sha256": _hash(value)}})
    if len({item["id"] for item in milestones}) != len(milestones):
        raise HarnessError("BENCH_DUPLICATE_MILESTONE")
    if sum(map(len, [*source.values(), *assets.values()])) > 16 * 1024 * 1024:
        raise HarnessError("BENCH_BUNDLE_TOO_LARGE")
    _deadline(deadline)
    return bundle, source, assets, milestones


def materialize_task_bundle(ref, *, assets_root, trial_root, row, manifest_digest, deadline=None,fresh_git=False):
    """Prepare bytes only; no process, inference, permissions or isolation claim."""
    if type(fresh_git)is not bool:raise HarnessError('BENCH_INVALID_FRESH_GIT_REQUEST')
    bundle, sources, protected, milestones = load_bundle(ref, assets_root, suite=row["suite"], task_id=row["task_id"], deadline=deadline)
    root = Path(trial_root).absolute()
    store = SupervisorStore(root)
    with store.lease("materialize pinned task bundle"):
        # Preserve an interrupted preparation. Never overwrite/replay partially
        # materialized inputs or reuse another arm's source/evaluator state.
        if any(path.name != "locks" for path in root.iterdir()):
            raise HarnessError("BENCH_MATERIALIZATION_EXISTS_OR_UNKNOWN")
        source, evaluator = root / "source", root / "evaluator"
        for name, raw in sources.items():
            _deadline(deadline)
            atomic_write(source / name, raw, exclusive=True)
        for name, raw in protected.items():
            _deadline(deadline)
            atomic_write(evaluator / name, raw, exclusive=True)
        initial = [{"path": name, "sha256": _hash(raw)} for name, raw in sorted(sources.items())]
        value = {"schema_version": 1, "record_type": "materialized_task_bundle", "manifest_digest": manifest_digest,
            "trial_id": row["trial_id"], "suite": row["suite"], "task_id": row["task_id"],
            "bundle_digest": digest(bundle), "bundle_ref": ref, "source_root": str(source), "evaluator_root": str(evaluator),
            "source_binding": parent_binding(source / "binding"), "evaluator_binding": parent_binding(evaluator / "binding"),
            "initial_files": initial, "immutable_files": [item for item in initial if item["path"] not in bundle["writable_paths"]],
            "protected_files": [{"path": name, "sha256": _hash(raw)} for name, raw in sorted(protected.items())],
            "writable_paths": bundle["writable_paths"], "milestones": milestones}
        if fresh_git:
            from .benchmark_git import initialize_fresh_git
            value.update(schema_version=2,fresh_git=initialize_fresh_git(source,deadline=deadline))
        store.write("materialized.yaml", value, MATERIALIZED, exclusive=True)
    verify_materialized(root / "materialized.yaml", initial=True, deadline=deadline)
    return value


def verify_materialized(receipt_path, *, initial=False, deadline=None):
    _deadline(deadline)
    value = strict_yaml.load(receipt_path, MATERIALIZED)
    root = Path(receipt_path).absolute().parent
    source, evaluator = Path(value["source_root"]), Path(value["evaluator_root"])
    if source != root / "source" or evaluator != root / "evaluator":
        raise HarnessError("BENCH_MATERIALIZED_ROOT_BINDING")
    verify_parent_binding(source / "binding", value["source_binding"])
    verify_parent_binding(evaluator / "binding", value["evaluator_binding"])
    if value['schema_version']==1 and ((source/'.git').exists() or (source/'.git').is_symlink()):
        raise HarnessError('BENCH_UNDECLARED_OUTPUT')
    if value['schema_version']==2:
        from .benchmark_git import verify_fresh_git
        if value['fresh_git']['root']!=str(source):raise HarnessError('BENCH_FRESH_GIT_ROOT_CONFLICT')
        verify_fresh_git(value['fresh_git'],initial=initial,deadline=deadline)
    for base, inventory in ((source, value["initial_files"] if initial else value["immutable_files"]), (evaluator, value["protected_files"])):
        for item in inventory:
            _deadline(deadline)
            if _hash(read_regular_file(base / _relative(item["path"]))) != item["sha256"]:
                raise HarnessError("BENCH_MATERIALIZED_INPUT_CHANGED")
    known = {item["path"] for item in value["initial_files"]} | (set() if initial else set(value["writable_paths"]))
    for index, path in enumerate(source.rglob("*")):
        if value['schema_version']==2 and path.relative_to(source).parts[0]=='.git':continue
        _deadline(deadline)
        if index >= 2048:
            raise HarnessError("BENCH_BUNDLE_TOO_LARGE")
        _ordinary(path, directory=path.is_dir())
        if path.is_file() and path.relative_to(source).as_posix() not in known:
            raise HarnessError("BENCH_MATERIALIZED_INPUT_CHANGED" if initial else "BENCH_UNDECLARED_OUTPUT")
    _deadline(deadline)
    return value


def evaluate_materialized(receipt_path, grade_input, *, receipt_digest=None, deadline=None):
    if receipt_digest is not None and _hash(read_regular_file(receipt_path)) != receipt_digest:
        raise HarnessError("BENCH_MATERIALIZED_RECEIPT_CHANGED")
    value = verify_materialized(receipt_path, deadline=deadline)
    inputs = strict_yaml.load(grade_input, GRADE_INPUT)
    if inputs["trial_id"] != value["trial_id"] or inputs["manifest_digest"] != value["manifest_digest"] or inputs["cwd"] != value["source_root"]:
        raise HarnessError("BENCH_EVALUATOR_SUBJECT_MISMATCH")
    outcome = inputs["solve_outcome"]
    if outcome == "unknown":
        raise HarnessError("BENCH_UNKNOWN_SOLVE_OUTCOME")
    if outcome in {"completed", "failed"}:
        checks_passed = True
        oracle = strict_yaml.load(Path(value["evaluator_root"]) / "oracle.yaml", ORACLE)
        for check in oracle["checks"]:
            _deadline(deadline)
            try:
                actual = _json(read_regular_file(Path(value["source_root"]) / check["path"]))
                expected = _json(evaluation.checked_ref(check["expected"], value["evaluator_root"]))
                if not json_equal(actual, expected):
                    checks_passed = False
            except (FileNotFoundError, ValueError):
                checks_passed = False
        # Acceptance is exclusively the independent oracle's result; a caller
        # cannot supply an already-accepted execution state or bypass checks.
        outcome = "accepted" if checks_passed else "failed"
    grade = {"schema_version": 1, "record_type": "independent_grade", "manifest_digest": value["manifest_digest"],
        "trial_id": value["trial_id"], "evaluator_digest": inputs["evaluator_digest"], "outcome": outcome}
    strict_yaml.validate(grade, evaluation.GRADE)
    _deadline(deadline)
    return grade


class TaskBundleBoundary:
    """Execution capability object, not a user approval or an isolation claim.

    An implementation must own transport/control/limits/metrics and route the
    evaluator outside solve visibility. Runtime qualification remains the
    driver's separate fresh-control contract and closed observed registry.
    """
    def identity(self):
        raise HarnessError("ENVIRONMENT_ISOLATION_UNAVAILABLE")

    def control_argv(self, challenge, *, deadline=None):
        raise HarnessError("ENVIRONMENT_ISOLATION_UNAVAILABLE")

    def require_available(self, *, deadline=None):
        self.identity()
        _deadline(deadline)

    def estimate_trial_tokens(self, row, bundle):
        return None

    def codex(self, prepared, *, deadline=None):
        raise HarnessError("BENCH_TRANSPORT_UNAVAILABLE")

    def evaluator_argv(self, argv, prepared, *, deadline=None):
        raise HarnessError("BENCH_EVALUATOR_BOUNDARY_UNAVAILABLE")

    def measurements(self, row, prepared, result, *, deadline=None):
        raise HarnessError("BENCH_MEASUREMENTS_UNAVAILABLE")


class LocalTaskBundleAdapter(CodexBenchmarkAdapter):
    """Concrete pinned materialization and evaluator bridge over Codex runtime."""
    def __init__(self, boundary=None,*,fresh_git=False):
        self.boundary = boundary or TaskBundleBoundary()
        self.fresh_git=fresh_git

    def identity(self):
        identity = self.boundary.identity()
        return {**identity, "adapter_id": "local-task-bundle-v1", "build_digest": digest([build_digest(), identity["build_digest"]])}

    def control_argv(self, challenge, *, deadline=None):
        return self.boundary.control_argv(challenge, deadline=deadline)

    def _task(self, row, manifest):
        suite = next(item for item in manifest["suites"] if item["id"] == row["suite"])
        return next(item for item in suite["tasks"] if item["id"] == row["task_id"])

    def estimate_trial_tokens(self, row, manifest, assets_root):
        bundle, _, _, _ = load_bundle(self._task(row, manifest)["source"], assets_root, suite=row["suite"], task_id=row["task_id"])
        return self.boundary.estimate_trial_tokens(row, bundle)

    def prepare(self, row, manifest, assets_root, trial_root, *, deadline=None):
        self.boundary.require_available(deadline=deadline)
        value = materialize_task_bundle(self._task(row, manifest)["source"], assets_root=assets_root, trial_root=trial_root,
            row=row, manifest_digest=digest(manifest), deadline=deadline,fresh_git=self.fresh_git)
        bundle, _, _, _ = load_bundle(value["bundle_ref"], assets_root, deadline=deadline)
        suite = next(item for item in manifest["suites"] if item["id"] == row["suite"])
        evaluation.checked_ref(suite["evaluator"], assets_root)
        evaluator_path = str(Path(assets_root).absolute() / suite["evaluator"]["path"])
        receipt_sha = _hash(read_regular_file(Path(trial_root) / "materialized.yaml"))
        oracle = strict_yaml.load(Path(value["evaluator_root"]) / "oracle.yaml", ORACLE)
        task = {"schema_version": 1, "task": {"id": row["task_id"], "objective": bundle["objective"], "allowed_write_scopes": bundle["writable_paths"]},
            "mode": "native", "memory": True, "ledger": "inform", "budgets": {key: bundle[key] for key in ("max_attempts", "max_wall_seconds", "turn_timeout_seconds")},
            # Independent gold checks run outside solve; this gate asserts only
            # that declared output artifacts are ordinary bounded JSON files.
            "acceptance": [{"id": "artifact-" + str(i), "kind": "command", "required": True,
                "argv": [sys.executable, "-c", "import json,pathlib,sys;json.loads(pathlib.Path(sys.argv[1]).read_text())", item["path"], receipt_sha, evaluator_path], "timeout": 5}
                for i, item in enumerate(oracle["checks"])]}
        contract = Path(trial_root) / "task.yaml"
        strict_yaml.dump(contract, task, exclusive=True)
        return {"schema_version": 1, "trial_id": row["trial_id"], "cwd": value["source_root"], "contract_path": str(contract),
            "root_policy_digest": manifest["root_policy"]["sha256"], "child_policy_digest": manifest["child_policy"]["sha256"],
            "token_upper_bound": self.boundary.estimate_trial_tokens(row, bundle)}

    def _verify(self, prepared, *, deadline=None):
        receipt = Path(prepared["contract_path"]).parent / "materialized.yaml"
        contract = strict_yaml.load(prepared["contract_path"])
        if _hash(read_regular_file(receipt)) != contract["acceptance"][0]["argv"][-2]:
            raise HarnessError("BENCH_MATERIALIZED_RECEIPT_CHANGED")
        value = verify_materialized(receipt, deadline=deadline)
        if value["trial_id"] != prepared["trial_id"] or value["source_root"] != prepared["cwd"]:
            raise HarnessError("BENCH_MATERIALIZED_PREPARED_MISMATCH")
        return value

    def codex(self, prepared, *, deadline=None):
        self._verify(prepared, deadline=deadline)
        return self.boundary.codex(prepared, deadline=deadline)

    def evaluator_argv(self, row, prepared, grade_input, *, deadline=None):
        self._verify(prepared, deadline=deadline)
        # Driver pins this literal evaluator asset. It is a tiny package bridge,
        # never a command selected by model output or an oracle executable.
        trial = Path(prepared["contract_path"]).parent
        contract = strict_yaml.load(prepared["contract_path"])
        receipt_sha, evaluator = contract["acceptance"][0]["argv"][-2:]
        argv = [sys.executable, evaluator, str(Path(__file__).parents[1]), str(trial / "materialized.yaml"), str(grade_input), deadline or "", receipt_sha]
        return self.boundary.evaluator_argv(argv, prepared, deadline=deadline)

    def measurements(self, row, prepared, result, *, deadline=None):
        self._verify(prepared, deadline=deadline)
        return self.boundary.measurements(row, prepared, result, deadline=deadline)


def evaluator_main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 4:
        raise HarnessError("BENCH_EVALUATOR_ARGUMENTS")
    print(json.dumps(evaluate_materialized(args[0], args[1], deadline=args[2] or None, receipt_digest=args[3]), sort_keys=True))
