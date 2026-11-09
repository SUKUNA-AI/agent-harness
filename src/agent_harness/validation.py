"""Bounded evidence parsing; also a standalone stdlib-only worker entry point."""
from __future__ import annotations

import json
import os
import re
import stat
import sys
import time

MAX_REQUEST_BYTES = 256 * 1024
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
CASE_SUMMARY_LIMIT = 100
CASE_NAME_CHARS = 1000
REGEX_META = frozenset(r".^$*+?{}[]\|()")


class ValidationTimeout(TimeoutError):
    pass


class InputLimit(ValueError):
    pass


class Inputs:
    def __init__(self):
        self.bytes_read = 0

    def read(self, path, max_bytes=None):
        remaining = MAX_INPUT_BYTES - self.bytes_read
        if max_bytes is not None:
            remaining = min(remaining, max_bytes)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as source:
            observed = os.fstat(source.fileno())
            if not stat.S_ISREG(observed.st_mode):
                raise ValueError("validation input must be a regular file")
            if observed.st_size > remaining:
                raise InputLimit(f"validation inputs exceed {remaining} byte limit")
            value = source.read(remaining + 1)
        if len(value) > remaining:
            raise InputLimit(f"validation inputs exceed {remaining} byte limit")
        self.bytes_read += len(value)
        return value

    def text(self, path):
        # Match the previous Path.read_text() universal-newline semantics.
        return self.read(path).decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def literal_assertions(assertions):
    """Only exact literals qualify; escaped/metacharacter regex always use worker."""
    return all(isinstance(item, dict) and isinstance(item.get("regex"), str)
               and not REGEX_META.intersection(item["regex"]) for item in assertions)


def json_equal(left, right):
    """Compare JSON values recursively, keeping booleans distinct from numbers.

    JSON has one number category, so integral and fractional spellings such as
    1 and 1.0 retain their existing numeric equality.
    """
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(json_equal(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(json_equal(a, b) for a, b in zip(left, right))
    if type(left) is not type(right) and not (type(left) in (int, float) and type(right) in (int, float)):
        return False
    return left == right


REPORT_MAX_BYTES = 2 * 1024 * 1024
REPORT_MAX_DEPTH = 48
REPORT_MAX_NODES = 100000
JUNIT_PROFILE = "junit-harness-v2"


def validate_equals(value):
    """The sole dynamic contract map; use the same JSON bounds in both paths."""
    if type(value) is not dict or len(value) > 1000:
        raise ValueError("reports.equals must be a bounded object")
    if any(type(key) is not str or not 1 <= len(key) <= 1000 for key in value):
        raise ValueError("reports.equals requires bounded string selectors")
    _json_bounds(value)
    if len(json.dumps(value, ensure_ascii=True, allow_nan=False).encode()) > REPORT_MAX_BYTES:
        raise InputLimit("reports.equals exceeds byte limit")


def _json_bounds(value, depth=0, nodes=None):
    import math
    nodes = [0] if nodes is None else nodes
    nodes[0] += 1
    if depth > REPORT_MAX_DEPTH or nodes[0] > REPORT_MAX_NODES:
        raise InputLimit("JSON report exceeds depth/node limit")
    if type(value) not in (dict, list, str, int, float, bool, type(None)):
        raise ValueError("report value is not JSON")
    if type(value) is str and len(value) > 262144:
        raise InputLimit("JSON report exceeds string length limit")
    if type(value) is float and not math.isfinite(value):
        raise ValueError("non-finite report number")
    if isinstance(value, dict):
        for key, child in value.items():
            if type(key) is not str or key == "<<":
                raise ValueError("invalid JSON-compatible contract key")
            _json_bounds(key, depth + 1, nodes)
            _json_bounds(child, depth + 1, nodes)
    elif isinstance(value, list):
        for child in value:
            _json_bounds(child, depth + 1, nodes)


def _json_report(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON report key")
            result[key] = value
        return result
    def constant(_):
        raise ValueError("non-finite report number")
    value = json.loads(data.decode("utf-8-sig"), object_pairs_hook=pairs, parse_constant=constant)
    _json_bounds(value)
    return value


def _junit(data, definition):
    """v2: unnamespaced or urn:junit suites, exact subtree aggregate counts.

    Suite error/failure elements count as issues but not executed testcases.
    Optional summary attributes must match observed subtree values; parent
    aggregates are checked independently and never added to their children.
    """
    import xml.etree.ElementTree as ET
    if len(data) > REPORT_MAX_BYTES:
        raise InputLimit("JUnit report exceeds byte limit")
    # Reject declarations even for UTF-16/32 encodings, before XML expansion.
    if b"<!DOCTYPE" in data.replace(b"\x00", b"").upper() or b"<!ENTITY" in data.replace(b"\x00", b"").upper():
        raise ValueError("DTD/entity declarations are forbidden")
    parser = ET.XMLPullParser(events=("start", "end"))
    depth = nodes = 0
    root = None
    namespace = None
    for offset in range(0, len(data), 8192):
        parser.feed(data[offset:offset + 8192])
        for event, element in parser.read_events():
            if event == "start":
                depth += 1
                nodes += 1
                if depth > REPORT_MAX_DEPTH or nodes > REPORT_MAX_NODES:
                    raise InputLimit("JUnit report exceeds depth/node limit")
                if root is None:
                    root = element
                    namespace = "{urn:junit}" if element.tag.startswith("{urn:junit}") else ""
                if element.tag.startswith("{") and not element.tag.startswith("{urn:junit}"):
                    raise ValueError("unsupported JUnit namespace")
                if namespace and not element.tag.startswith(namespace):
                    raise ValueError("mixed JUnit namespaces")
                element.tag = element.tag.removeprefix(namespace) if namespace else element.tag
            else:
                depth -= 1
    parser.close()
    if root is None or root.tag not in {"testsuite", "testsuites"}:
        raise ValueError("unsupported JUnit root")
    required = [re.compile(pattern) for pattern in definition.get("required_cases", [])]
    found, passed = [False] * len(required), [True] * len(required)
    names, case_ids, errors = [], [], []
    shortened_names = 0

    def count_attributes(element, counts):
        for key, observed in counts.items():
            if key in element.attrib:
                raw = element.attrib[key]
                if not re.fullmatch(r"[0-9]+", raw) or int(raw) != observed:
                    errors.append("report_inconsistent: JUnit " + key + " count")

    def suite(element, lineage, route=()):
        nonlocal shortened_names
        counts = dict.fromkeys(("tests", "skipped", "failures", "errors", "disabled"), 0)
        lineage = [*lineage, element.get("name", element.tag)]
        for occurrence, child in enumerate(element):
            if child.tag == "testcase" and element.tag == "testsuite":
                if any(item.tag not in {"failure", "error", "skipped", "system-out", "system-err", "properties"} for item in child):
                    raise ValueError("unsupported JUnit testcase structure")
                name = f"{child.get('classname', '')}::{child.get('name', '')}"
                status = child.get("status", "").strip().lower()
                result = child.get("result", "").strip().lower()
                disabled = status == "disabled"
                skipped = (child.find("skipped") is not None or status in {"notrun", "skipped", "disabled", "suppressed"}
                           or result in {"suppressed", "skipped", "notrun", "disabled"})
                unknown = status not in {"", "run", "notrun", "skipped", "disabled", "suppressed"} or result not in {"", "completed", "suppressed", "skipped", "notrun", "disabled"}
                failure, error = child.find("failure") is not None, child.find("error") is not None or unknown
                counts["tests"] += 1
                counts["skipped"] += skipped
                counts["disabled"] += disabled
                counts["failures"] += failure
                counts["errors"] += error
                if len(names) < CASE_SUMMARY_LIMIT:
                    shortened_names += len(name) > CASE_NAME_CHARS
                    names.append(name[:CASE_NAME_CHARS])
                    case_ids.append("/".join(map(str, (*route, occurrence))))
                for index, pattern in enumerate(required):
                    if pattern.search(name):
                        found[index] = True
                        passed[index] &= not skipped and not failure and not error
            elif child.tag == "testsuite":
                child_counts = suite(child, lineage, (*route, occurrence))
                for key in counts:
                    counts[key] += child_counts[key]
            elif child.tag in {"failure", "error"}:
                counts["failures" if child.tag == "failure" else "errors"] += 1
            elif child.tag not in {"properties", "system-out", "system-err"}:
                raise ValueError("unsupported JUnit suite structure")
        count_attributes(element, counts)
        return counts

    counts = suite(root, [])
    tests, skipped = counts["tests"], counts["skipped"]
    failed = counts["failures"] + counts["errors"]
    if tests < definition.get("min_tests", 1):
        errors.append("test report collected too few cases")
    if skipped > definition.get("max_skipped", 0):
        errors.append("test report skipped mandatory cases")
    if failed:
        errors.append("test report contains failures")
    for pattern, exists, success in zip(required, found, passed):
        if not exists:
            errors.append(f"mandatory case missing: {pattern.pattern}")
        elif not success:
            errors.append(f"mandatory case did not pass: {pattern.pattern}")
    return errors, {"format": "junit", "parser_profile": JUNIT_PROFILE, "cases": names, "case_ids": case_ids,
                    "tests": tests, "skipped": skipped, "failed": failed,
                    "cases_truncated": tests > len(names) or bool(shortened_names),
                    "cases_omitted": tests - len(names), "case_names_shortened": shortened_names}


def validate(request, *, literals=False, timeout=None):
    """Parse only data; no subprocesses, dynamic imports from paths, eval or exec."""
    deadline = time.monotonic() + timeout if timeout is not None else None

    def check_deadline():
        if deadline is not None and time.monotonic() >= deadline:
            raise ValidationTimeout("evidence validation timed out")

    inputs = Inputs()
    errors, reports = [], []
    try:
        if request.get("schema_version") != 1:
            raise ValueError("unsupported validation request")
        assertions = request.get("assertions", [])
        if not isinstance(assertions, list):
            raise ValueError("assertions must be an array")
        if literals and (request.get("reports") or not literal_assertions(assertions)):
            raise ValueError("nonliteral validation requires the worker")
        if assertions:
            stdout = inputs.text(request["stdout_path"])
            stderr = inputs.text(request["stderr_path"])
            streams = {"stdout": stdout, "stderr": stderr}
            if any(item.get("stream", "combined") == "combined" for item in assertions):
                streams["combined"] = stdout + "\n" + stderr
            for assertion in assertions:
                check_deadline()
                stream = streams.get(assertion.get("stream", "combined"))
                if stream is None:
                    errors.append("unknown assertion stream")
                    continue
                minimum = assertion.get("min_matches", 1)
                if type(minimum) is not int or minimum < 1 or type(assertion.get("forbidden", False)) is not bool:
                    raise ValueError("invalid assertion threshold or forbidden flag")
                pattern = assertion["regex"]
                count = stream.count(pattern) if literals else sum(1 for _ in re.finditer(pattern, stream, re.M))
                check_deadline()
                if assertion.get("forbidden", False) and count:
                    errors.append(f"forbidden output pattern found: {pattern}")
                elif not assertion.get("forbidden", False) and count < minimum:
                    errors.append(f"required output pattern missing: {pattern}")
        for item in request.get("reports", []):
            check_deadline()
            definition = item["definition"]
            try:
                data = inputs.read(item["path"], max_bytes=REPORT_MAX_BYTES)
                if len(data) > REPORT_MAX_BYTES:
                    raise InputLimit("report exceeds byte limit")
                if definition["format"] == "junit":
                    report_errors, report = _junit(data, definition)
                    errors.extend(report_errors)
                    reports.append(report)
                elif definition["format"] == "json":
                    value = _json_report(data)
                    validate_equals(definition.get("equals", {}))
                    for key, expected in definition.get("equals", {}).items():
                        cursor = value
                        for segment in key.split("."):
                            if not isinstance(cursor, dict):
                                raise ValueError("JSON report path crosses a non-object")
                            cursor = cursor[segment]
                        if not json_equal(cursor, expected):
                            errors.append(f"report field mismatch: {key}")
                    reports.append({"format": "json", "verified_equals": definition.get("equals", {})})
                else:
                    errors.append("unsupported report format")
            except InputLimit:
                raise
            except Exception as error:
                errors.append(f"invalid test report: {type(error).__name__}")
        check_deadline()
        return {"schema_version": 1, "status": "failed" if errors else "passed", "errors": errors,
                "reports": reports, "input_bytes": inputs.bytes_read}
    except ValidationTimeout as error:
        return {"schema_version": 1, "status": "validation_timeout", "errors": [str(error)], "reports": [], "input_bytes": inputs.bytes_read}
    except (InputLimit, OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        message = str(error) if isinstance(error, InputLimit) else type(error).__name__
        return {"schema_version": 1, "status": "failed", "errors": ["evidence validation failed: " + message], "reports": [], "input_bytes": inputs.bytes_read}


def _encode(value, limit):
    blocks, size = [], 0
    for chunk in json.JSONEncoder(ensure_ascii=True, separators=(",", ":")).iterencode(value):
        block = chunk.encode("utf-8")
        size += len(block)
        if size > limit:
            raise InputLimit(f"encoded validation data exceeds {limit} bytes")
        blocks.append(block)
    return b"".join(blocks)


def encode_request(request):
    try:
        return _encode(request, MAX_REQUEST_BYTES)
    except InputLimit:
        raise InputLimit(f"validation request exceeds {MAX_REQUEST_BYTES} bytes") from None


def encode_response(response):
    try:
        return _encode(response, MAX_RESPONSE_BYTES)
    except InputLimit:
        failure = {"schema_version": 1, "status": "failed", "errors": [f"validation response exceeds {MAX_RESPONSE_BYTES} bytes"], "reports": [], "input_bytes": response.get("input_bytes")}
        return json.dumps(failure, separators=(",", ":")).encode("utf-8")


def main():
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise InputLimit(f"validation request exceeds {MAX_REQUEST_BYTES} bytes")
        request = json.loads(raw)
        response = validate(request)
    except Exception as error:
        message = str(error) if isinstance(error, InputLimit) else type(error).__name__
        response = {"schema_version": 1, "status": "failed", "errors": ["validation request failed: " + message], "reports": []}
    sys.stdout.buffer.write(encode_response(response))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
