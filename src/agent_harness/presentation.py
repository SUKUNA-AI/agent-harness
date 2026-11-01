"""Bounded local run views. No resume, model calls, state writes or native hooks.

The byte budget reserves UTF-8 compact JSON plus either LF or Windows CRLF.
Receipts are recorded observations unless explicit verification is requested.
"""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import re

from .context import _read
from .core import HarnessError, IDENTIFIER, digest

MAX_METADATA_BYTES = 1024 * 1024
MAX_CHECKPOINT_BYTES = 4 * 1024 * 1024
MAX_RESULT_BYTES = 256 * 1024
MAX_CHECKS = 100
MAX_ITEMS = 20
SHA = re.compile(r"[a-f0-9]{64}\Z")
STATUSES = frozenset({"planned", "running", "verifying", "ready_for_review", "accepted",
                     "rejected", "blocked", "failed", "cancelled", "integrated", "stale",
                     "open", "idle", "done", "stopped", "closed", "unavailable", "unknown",
                     "passed", "satisfied", "confirmed", "not_run", "skipped", "timeout", "interrupted",
                     "missing_dependency", "budget_exhausted", "toolchain_changed", "inputs_changed",
                     "validation_timeout"})


def render_json(value):
    """Compact JSON with LF; max_bytes also reserves Windows CRLF translation."""
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"


def _shape(value):
    pending, nodes, text_bytes = [(value, 0)], 0, 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > 32 or nodes > 65536:
            raise HarnessError("Presentation input exceeds structural limits")
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise HarnessError("Presentation input needs string keys")
            pending.extend((key, depth + 1) for key in item)
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            if len(item) > MAX_CHECKPOINT_BYTES:
                raise HarnessError("Presentation input text exceeds limits")
            text_bytes += len(item.encode("utf-8"))
            if text_bytes > MAX_CHECKPOINT_BYTES:
                raise HarnessError("Presentation input text exceeds limits")
        elif item is not None and type(item) not in {int, float, bool}:
            raise HarnessError("Presentation input is not JSON metadata")


def _pairs(pairs):
    value = {}
    for key, child in pairs:
        if key in value:
            raise HarnessError("Duplicate metadata JSON key")
        value[key] = child
    return value


def _json(run, relative, limit=MAX_METADATA_BYTES):
    root = Path(run.path)
    data, _ = _read(root / relative, limit, root=root, name=relative)
    if data is None:
        raise HarnessError("Presentation metadata exceeds its byte limit")
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite metadata")))
        _shape(value)
    except (ValueError, RecursionError) as exc:
        raise HarnessError("Invalid presentation metadata") from exc
    if not isinstance(value, dict):
        raise HarnessError("Presentation metadata must be an object")
    return value


def _identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise HarnessError("Invalid run metadata identifier")
    return value


def _sha(value):
    return value if isinstance(value, str) and SHA.fullmatch(value) else None


def _state(value):
    _shape(value)
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise HarnessError("Unsupported presentation state")
    for key in ("run_id", "task_id", "current_attempt"):
        _identifier(value.get(key))
    if not _sha(value.get("contract_digest")):
        raise HarnessError("Invalid current contract digest")
    return value


def _contract(run, state):
    value = _json(run, "contracts/" + state["contract_digest"] + ".json")
    if digest(value) != state["contract_digest"]:
        raise HarnessError("Contract content integrity failed")
    for key, expected in (("run_id", state["run_id"]), ("task_id", state["task_id"]),
                          ("attempt_id", state["current_attempt"])):
        if value.get(key) != expected:
            raise HarnessError("Current contract binding failed")
    return value


def _status(value):
    return value if isinstance(value, str) and value in STATUSES else "unknown"


def _text(value, length=512):
    value = value if isinstance(value, str) else ""
    return {"text": value[:length], "omitted_chars": max(0, len(value) - length)}


def _group(values, category):
    if not isinstance(values, dict):
        raise HarnessError("Invalid " + category + " metadata")
    counts, selected, items = Counter(), 0, []
    for key, record in values.items():
        if not isinstance(record, dict):
            raise HarnessError("Invalid " + category + " record")
        status = _status(record.get("status"))
        counts[status] += 1
        include = (status != "closed" if category == "owners" else
                   status not in {"confirmed", "failed"} if category == "operations" else
                   status in {"running", "interrupted", "unknown"})
        if not include:
            continue
        selected += 1
        if len(items) >= MAX_ITEMS:
            continue
        row = {"id": _text(key, 128), "recorded_status": status}
        if category == "owners":
            row.update(native_ref=_text(record.get("native_ref"), 160),
                       needs_native_reconciliation=record.get("needs_native_reconciliation") is True)
        elif category == "processes":
            row["attempt_id"] = _text(record.get("attempt_id"), 128)
        else:
            row["action"] = _text(record.get("action"), 160)
        items.append(row)
    return {"total": len(values), "counts": dict(counts), "filtered": len(values) - selected,
            "items": items, "omitted": selected - len(items)}


def _checks(run, state, contract):
    definitions = contract.get("checks")
    if not isinstance(definitions, list):
        raise HarnessError("Invalid contract checks")
    counts, items, required, seen = Counter(), [], 0, set()
    for index, definition in enumerate(definitions):
        if not isinstance(definition, dict) or type(definition.get("required")) is not bool:
            raise HarnessError("Invalid contract check definition")
        check_id = _identifier(definition.get("id"))
        if check_id in seen:
            raise HarnessError("Duplicate contract check ID")
        seen.add(check_id)
        required += int(definition["required"])
        if index >= MAX_CHECKS:
            counts["not_inspected"] += 1
            continue
        reference = f"attempts/{state['current_attempt']}/checks/{check_id}/result.json"
        row = {"check_id": check_id, "required": definition["required"], "reference": reference}
        try:
            record = _json(run, reference, MAX_RESULT_BYTES)
            if _sha(record.get("digest")) != digest({k: v for k, v in record.items() if k != "digest"}):
                raise HarnessError("Check result integrity failed")
            expected = {"run_id": state["run_id"], "task_id": state["task_id"],
                        "attempt_id": state["current_attempt"], "check_id": check_id,
                        "contract_digest": state["contract_digest"], "candidate_digest": state.get("candidate_digest")}
            if any(record.get(k) != value for k, value in expected.items()):
                row["recorded_status"] = "stale"
            else:
                row["recorded_status"] = _status(record.get("status"))
            row.update(result_available=True, result_digest=record["digest"])
        except FileNotFoundError:
            processes = state.get("processes", {})
            process = processes.get(check_id, {}) if isinstance(processes, dict) else {}
            running = isinstance(process, dict) and process.get("status") == "running" and process.get("attempt_id") == state["current_attempt"]
            row.update(recorded_status="running" if running else "not_run", result_available=False)
        except (HarnessError, OSError, ValueError):
            row.update(recorded_status="invalid", result_available=None)
        counts[row["recorded_status"]] += 1
        if len(items) < MAX_ITEMS:
            items.append(row)
    return {"total": len(definitions), "required": required, "counts": dict(counts),
            "inspected": min(len(definitions), MAX_CHECKS), "items": items, "omitted": len(definitions) - len(items)}


def _summary(run, state):
    state = _state(state)
    contract = _contract(run, state)
    validation = state.get("evidence_validation", {})
    validation = validation if isinstance(validation, dict) else {}
    errors = validation.get("errors", [])
    errors = errors if isinstance(errors, list) else []
    validation_status = validation.get("status")
    if not isinstance(validation_status, str) or validation_status not in {"passed", "failed", "not_checked"}:
        validation_status = "not_checked"
    return {
        "schema_version": 1, "view": "status_summary", "run_id": state["run_id"],
        "task_id": state["task_id"], "current_attempt": state["current_attempt"],
        "contract_digest": state["contract_digest"], "candidate_digest": _sha(state.get("candidate_digest")),
        "recorded_status": _status(state.get("recorded_status", state.get("status"))),
        "status": _status(state.get("status")), "goal": _text(contract.get("goal")),
        "source_current": state.get("source_current") if type(state.get("source_current")) is bool else None,
        "evidence_current": state.get("evidence_current") if type(state.get("evidence_current")) is bool else None,
        "evidence_validation": {"status": validation_status,
                                "total": len(errors), "items": [_text(e, 160) for e in errors[:MAX_ITEMS]], "omitted": max(0, len(errors) - MAX_ITEMS)},
        "owners": _group(state.get("owners", {}), "owners"),
        "processes": _group(state.get("processes", {}), "processes"),
        "operations": _group(state.get("operations", {}), "operations"),
        "checks": _checks(run, state, contract),
        "details": {"state": "state.json", "contract": f"contracts/{state['contract_digest']}.json"},
        "limitation": "Recorded receipts, not task acceptance or observed native lifecycle. Omitted details remain available by reference.",
    }


def _budget(max_bytes):
    if type(max_bytes) is not int or not 2048 <= max_bytes <= 65536:
        raise HarnessError("Presentation max_bytes must be 2048..65536")


def _has_omissions(value):
    if isinstance(value, dict):
        return bool(value.get("omitted") or value.get("omitted_chars")) or any(_has_omissions(v) for v in value.values())
    return isinstance(value, list) and any(_has_omissions(v) for v in value)


def _fit(value, max_bytes):
    _budget(max_bytes)
    value["output"] = {"max_bytes": max_bytes, "truncated": _has_omissions(value)}
    # One extra byte reserves CRLF when the CLI prints through Windows TextIO.
    while len(render_json(value).encode("utf-8")) + 1 > max_bytes:
        choices = []

        def visit(item):
            if isinstance(item, dict):
                if isinstance(item.get("text"), str) and item.get("text"):
                    choices.append((len(render_json(item["text"]).encode("utf-8")), "text", item))
                if isinstance(item.get("items"), list) and item["items"]:
                    choices.append((len(render_json(item["items"][-1]).encode("utf-8")), "items", item))
                for child in item.values():
                    visit(child)
            elif isinstance(item, list):
                for child in item:
                    visit(child)

        visit(value)
        if not choices:
            raise HarnessError("Presentation budget is too small for required identity and counts")
        _, kind, target = max(choices, key=lambda choice: choice[0])
        if kind == "items":
            target["items"].pop()
            target["omitted"] += 1
        else:
            size = len(target["text"])
            keep = size // 2 if size > 128 else 0
            target["text"] = target["text"][:keep]
            target["omitted_chars"] += size - keep
        value["output"]["truncated"] = True
    return value


def status_summary(run, state, max_bytes=8192):
    """Summarize supplied status without mutating it or running fresh verification."""
    _budget(max_bytes)
    return _fit(_summary(run, state), max_bytes)


def _checkpoint(run, state):
    version = state.get("checkpoints", 0)
    if type(version) is not int or not 0 <= version <= 1_000_000:
        raise HarnessError("Invalid checkpoint counter")
    if not version:
        return {"status": "not_recorded", "version": 0}
    reference = f"checkpoints/{version:06d}.json"
    result = {"status": "invalid", "version": version, "reference": reference}
    try:
        record = _json(run, reference, MAX_CHECKPOINT_BYTES)
        if _sha(record.get("digest")) != digest({k: v for k, v in record.items() if k != "digest"}):
            raise HarnessError("Checkpoint content integrity failed")
        saved, contract = record.get("state"), record.get("contract")
        if (type(record.get("schema_version")) is not int or record["schema_version"] != 1
                or type(record.get("version")) is not int or record["version"] != version
                or not isinstance(saved, dict) or not isinstance(contract, dict)
                or type(saved.get("checkpoints")) is not int or saved["checkpoints"] != version
                or not isinstance(record.get("note"), str)
                or not isinstance(record.get("next_safe_step"), str)):
            raise HarnessError("Invalid checkpoint record")
        _state(saved)
        result["digest"] = record["digest"]
        if any(saved.get(key) != state[key] or contract.get(key) != state[key] for key in ("run_id", "task_id")):
            result["status"] = "foreign"
            return result
        if saved.get("contract_digest") != digest(contract) or saved.get("current_attempt") != contract.get("attempt_id"):
            raise HarnessError("Checkpoint internal binding failed")
        mismatches = [key for key in ("current_attempt", "contract_digest", "candidate_digest") if saved.get(key) != state.get(key)]
        if mismatches:
            result.update(status="stale", mismatched_fields=mismatches)
            return result
        result.update(status="current", note=_text(record["note"], 2048),
                      next_safe_step=_text(record["next_safe_step"], 1024),
                      created_at=_text(record.get("created_at"), 64),
                      source_freshness="not_checked")
    except FileNotFoundError:
        result["status"] = "missing"
    except (HarnessError, OSError, ValueError, TypeError):
        result["status"] = "invalid"
    return result


def handoff(run, verify=False, max_bytes=8192):
    """Read current receipts and a bound checkpoint; never resume or modify a run."""
    _budget(max_bytes)
    if type(verify) is not bool:
        raise HarnessError("verify must be an explicit boolean")
    state = _state(_json(run, "state.json"))
    if verify:
        state = _state(run.status(verify_evidence=True))
    else:
        state = {**state, "source_current": None, "evidence_current": None,
                 "evidence_validation": {"status": "not_checked", "errors": []}}
    result = _summary(run, state)
    result["view"] = "handoff"
    result["checkpoint"] = _checkpoint(run, state)
    result["native_resume"] = "not_claimed"
    latest = _state(_json(run, "state.json"))
    if any(latest.get(key) != state.get(key) for key in
           ("run_id", "task_id", "current_attempt", "contract_digest", "candidate_digest", "checkpoints")):
        raise HarnessError("Run changed while preparing handoff; retry from current receipts")
    return _fit(result, max_bytes)
