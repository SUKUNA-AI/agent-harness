"""Finite repair decisions from observed failures, never model self-report."""
from __future__ import annotations

import re
from .core import HarnessError, digest


PREPARATION_TARGETS = {
    "NO_PROGRESS": "NO_PROGRESS", "ATTEMPT_BUDGET": "FAILED_TERMINAL",
    "TOKEN_BUDGET": "FAILED_TERMINAL", "DEADLINE": "TIMED_OUT",
    "ENVIRONMENT": "BLOCKED_ENVIRONMENT", "APPROVAL": "BLOCKED_APPROVAL",
    "INTEGRITY": "UNKNOWN_OUTCOME", "AMBIGUOUS": "BLOCKED_AMBIGUOUS",
    "INTERNAL": "UNKNOWN_OUTCOME", "USAGE_UNVERIFIED": "BLOCKED_ENVIRONMENT",
    "CANCELLED": "CANCELLED",
}
PREPARATION_CONTEXTS = {"core_bootstrap", "core_prepare", "root_preflight",
    "reservation_recovery", "invocation_boundary", "failure_publication"}


class PreparationFailure(HarnessError):
    """Controller facts select the outcome; exception text is diagnostic only."""
    def __init__(self, reason_code, context, detail, *, proof=None):
        if reason_code not in PREPARATION_TARGETS or context not in PREPARATION_CONTEXTS:
            raise ValueError("Unsupported preparation reason/context")
        if not isinstance(detail, str) or not detail or proof is not None and type(proof) is not dict:
            raise ValueError("Preparation failure needs a safe diagnostic and structured proof")
        super().__init__(detail)
        self.reason_code, self.context, self.proof = reason_code, context, proof
        self.target = PREPARATION_TARGETS[reason_code]


def classify(value):
    """Accept a check/adapter result and keep unknown writes distinct from tests."""
    status = value.get("status", "unknown")
    text = str(value.get("error", "")) + " " + str(value.get("errors", ""))
    if status in {"unknown_outcome", "interrupted", "invalid_stream", "partial_stream"}:
        category = "unknown_outcome"
    elif status in {"cancelled", "timed_out", "timeout", "budget_exhausted"}:
        category = "timeout" if status != "cancelled" else "cancelled"
    elif status == "missing_dependency" or re.search(r"not found|missing.*(?:tool|dependency)|no space left|disk full", text, re.I):
        category = "environment"
    elif re.search(r"permission denied|access.*denied|approval|sandbox.*(?:denied|refus)", text, re.I):
        category = "permission"
    elif re.search(r"stale|changed.*(?:candidate|input|source)|candidate.*changed|toolchain.*changed", text, re.I):
        category = "stale_evidence"
    elif status == "unsafe_plan":
        category = "unsafe_plan"
    elif re.search(r"ambiguous|non-command|required.*evidence|not_run", text, re.I):
        category = "ambiguous_requirement"
    elif re.search(r"compiler|syntaxerror|error\[E\d+\]|cannot compile", text, re.I):
        category = "code_defect"
    else:
        category = "test_failure" if status in {"failed", "rejected"} else "unknown_outcome"
    normalized = re.sub(r"\b[0-9a-f]{32,64}\b|\b\d+(?:\.\d+)?\b", "#", text)[:4096]
    return {"category": category, "signature": digest([category, normalized]),
            "repairable": category in {"test_failure", "code_defect", "stale_evidence"},
            "detail": text[:2048]}


def repair_decision(history, failure, snapshot_id, *, attempts, max_attempts):
    if not failure["repairable"]:
        states = {"permission": "BLOCKED_APPROVAL", "environment": "BLOCKED_ENVIRONMENT",
                  "ambiguous_requirement": "BLOCKED_AMBIGUOUS", "timeout": "TIMED_OUT",
                  "cancelled": "CANCELLED", "unknown_outcome": "UNKNOWN_OUTCOME"}
        return {"action": "stop", "status": states.get(failure["category"], "FAILED_TERMINAL"),
                "reason": failure["category"]}
    if attempts >= max_attempts:
        return {"action": "stop", "status": "FAILED_TERMINAL", "reason": "model attempt budget exhausted"}
    repeated = 0
    for old in reversed(history):
        if old.get("signature") != failure["signature"] or old.get("snapshot_id") != snapshot_id:
            break
        repeated += 1
    if repeated >= 1:
        return {"action": "stop", "status": "NO_PROGRESS", "reason": "identical failure and unchanged source in consecutive attempts"}
    return {"action": "repair", "status": "REPAIRING", "reason": "targeted diagnosis using fresh failure evidence"}
