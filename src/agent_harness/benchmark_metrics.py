"""Versioned benchmark metric observations; missing instrumentation is unknown.

Receipts prove local bytes/subject bindings. They do not qualify a producer's
operational isolation or turn caller-supplied observations into runtime facts.
"""
from copy import deepcopy
import hashlib
from pathlib import Path, PurePosixPath, PureWindowsPath
import uuid

from .core import HarnessError
from .execution_ledger import HASH, SHORT, object_schema, safe_private
from .file_io import read_regular_file
from .supervisor_store import atomic_write
from . import strict_yaml
from .validation import json_equal

REF = object_schema({"path": SHORT, "sha256": HASH}, ("path", "sha256"))
COUNT = {"type": ["integer", "null"], "minimum": 0}
DURATION = {"type": ["number", "null"], "minimum": 0}
METRIC_TYPES = {
    "mandatory_nodes_skipped": COUNT, "duplicate_writes": COUNT,
    "recovery": {"enum": [None, "not_applicable", "succeeded", "failed", "unknown"]},
    "useful_retrieval": {"type": ["boolean", "null"]}, "stale_reuse": COUNT,
    "hook_overhead_ms": DURATION, "manual_interventions": COUNT,
}
PHASES = ("environment_prepare", "bootstrap", "retrieval", "scheduler", "integration", "checks", "solve", "evaluator")
COVERAGE = {"enum": ["observed", "partial", "unavailable"]}


def metric_schema(value_schema):
    return object_schema({"value": value_schema, "coverage": COVERAGE,
        "evidence_refs": {"type": "array", "maxItems": 32, "items": REF}},
        ("value", "coverage", "evidence_refs"))


MEASUREMENTS_V2 = object_schema({"schema_version": {"const": 2},
    "metrics": object_schema({name: metric_schema(kind) for name, kind in METRIC_TYPES.items()}, tuple(METRIC_TYPES)),
    "phase_seconds": object_schema({name: metric_schema(DURATION) for name in PHASES}, PHASES)},
    ("schema_version", "metrics", "phase_seconds"))
RECEIPT = object_schema({"schema_version": {"const": 1}, "record_type": {"const": "benchmark_metric"},
    "trial_id": HASH, "manifest_digest": HASH, "metric": SHORT,
    "value": {"type": ["integer", "number", "boolean", "string", "null"]}, "coverage": COVERAGE,
    "producer_id": SHORT, "observation_scope": SHORT,
    "source_refs": {"type": "array", "maxItems": 32, "items": REF}},
    ("schema_version", "record_type", "trial_id", "manifest_digest", "metric", "value", "coverage", "producer_id", "observation_scope", "source_refs"))


def trial_schema(legacy):
    schema = deepcopy(legacy)
    schema["properties"]["schema_version"] = {"const": 2}
    for name in tuple(METRIC_TYPES):
        schema["properties"].pop(name, None)
        if name in schema["required"]:
            schema["required"].remove(name)
    for name in ("metrics", "phase_seconds"):
        schema["properties"][name] = MEASUREMENTS_V2["properties"][name]
        schema["required"].append(name)
    return schema


def unavailable_measurements():
    def missing():
        return {"value": None, "coverage": "unavailable", "evidence_refs": []}
    return {"schema_version": 2, "metrics": {name: missing() for name in METRIC_TYPES},
            "phase_seconds": {name: missing() for name in PHASES}}


def portable_relative_path(text):
    # Public refs use portable forward-slash paths. On Windows, a rooted
    # relative path, embedded drive component or ADS can escape/change a join.
    if not isinstance(text, str) or "\x00" in text:
        raise ValueError("Expected a portable relative asset path")
    portable = PurePosixPath(text)
    windows = PureWindowsPath(text)
    if (portable.is_absolute() or windows.anchor or windows.drive
            or "\\" in text or ":" in text or ".." in portable.parts or not portable.parts):
        raise ValueError("Expected a portable relative asset path")
    return Path(*portable.parts)


def _read_ref(ref, root):
    strict_yaml.validate(ref, REF)
    try:
        rel = portable_relative_path(ref["path"])
    except ValueError as error:
        raise HarnessError("BENCH_METRIC_UNSAFE_REF") from error
    raw = read_regular_file(safe_private(Path(root).absolute() / rel), max_bytes=2 * 1024 * 1024)
    if hashlib.sha256(raw).hexdigest() != ref["sha256"]:
        raise HarnessError("BENCH_METRIC_STALE_EVIDENCE")
    return raw


def _shape(name):
    if name.startswith("phase_seconds.") and name.split(".", 1)[1] in PHASES:
        return DURATION
    if name in METRIC_TYPES:
        return METRIC_TYPES[name]
    raise HarnessError("BENCH_METRIC_UNKNOWN_NAME")


def validate_measurements(value, root=None, *, trial_id=None, manifest_digest=None):
    strict_yaml.validate(value, MEASUREMENTS_V2)
    fields = list(value["metrics"].items()) + [("phase_seconds." + key, metric) for key, metric in value["phase_seconds"].items()]
    for name, metric in fields:
        if metric["coverage"] == "unavailable" and metric["value"] is not None:
            raise HarnessError("BENCH_METRIC_UNAVAILABLE_HAS_VALUE")
        if metric["coverage"] == "observed" and (metric["value"] is None or not metric["evidence_refs"]):
            raise HarnessError("BENCH_METRIC_OBSERVATION_REQUIRES_EVIDENCE")
        if metric["value"] is not None and not metric["evidence_refs"]:
            raise HarnessError("BENCH_METRIC_VALUE_REQUIRES_EVIDENCE")
        if root is None:
            continue
        for ref in metric["evidence_refs"]:
            receipt = strict_yaml.loads(_read_ref(ref, root), RECEIPT)
            if (receipt["metric"] != name or not json_equal(receipt["value"], metric["value"])
                    or receipt["coverage"] != metric["coverage"]
                    or trial_id is not None and receipt["trial_id"] != trial_id
                    or manifest_digest is not None and receipt["manifest_digest"] != manifest_digest):
                raise HarnessError("BENCH_METRIC_SUBJECT_MISMATCH")
            if metric["value"] is not None and not receipt["source_refs"]:
                raise HarnessError("BENCH_METRIC_SOURCE_REQUIRED")
            for source in receipt["source_refs"]:
                _read_ref(source, root)
    return value


class MetricRecorder:
    """Record explicit instrumentation, without filling gaps with zero."""
    def __init__(self, root, *, trial_id, manifest_digest, producer_id):
        self.root = safe_private(Path(root).absolute())
        self.trial_id, self.manifest_digest, self.producer_id = trial_id, manifest_digest, producer_id

    def record(self, name, value, *, coverage, source_refs, observation_scope):
        strict_yaml.validate(value, _shape(name))
        for ref in source_refs:
            _read_ref(ref, self.root)
        receipt = {"schema_version": 1, "record_type": "benchmark_metric", "trial_id": self.trial_id,
            "manifest_digest": self.manifest_digest, "metric": name, "value": value, "coverage": coverage,
            "producer_id": self.producer_id, "observation_scope": observation_scope, "source_refs": source_refs}
        strict_yaml.validate(receipt, RECEIPT)
        if (coverage == "unavailable" and value is not None or value is not None and not source_refs
                or coverage == "observed" and value is None):
            raise HarnessError("BENCH_METRIC_INVALID_OBSERVATION")
        relative = "metric-evidence/" + uuid.uuid4().hex + ".yaml"
        target = safe_private(self.root / relative)
        atomic_write(target, strict_yaml.dumps(receipt).encode("utf-8"), exclusive=True)
        ref = {"path": relative, "sha256": hashlib.sha256(read_regular_file(target)).hexdigest()}
        return {"value": value, "coverage": coverage, "evidence_refs": [ref]}


def metric_value(trial, name):
    if trial["schema_version"] == 2:
        return trial["metrics"][name]["value"]
    return trial.get(name)


def metric_unknown(trial, name):
    if trial["schema_version"] == 2:
        return trial["metrics"][name]["coverage"] != "observed"
    return trial.get(name) is None
