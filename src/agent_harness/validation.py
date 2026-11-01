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

    def read(self, path):
        remaining = MAX_INPUT_BYTES - self.bytes_read
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as source:
            observed = os.fstat(source.fileno())
            if not stat.S_ISREG(observed.st_mode):
                raise ValueError("validation input must be a regular file")
            if observed.st_size > remaining:
                raise InputLimit(f"validation inputs exceed {MAX_INPUT_BYTES} bytes")
            value = source.read(remaining + 1)
        if len(value) > remaining:
            raise InputLimit(f"validation inputs exceed {MAX_INPUT_BYTES} bytes")
        self.bytes_read += len(value)
        return value

    def text(self, path):
        # Match the previous Path.read_text() universal-newline semantics.
        return self.read(path).decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def literal_assertions(assertions):
    """Only exact literals qualify; escaped/metacharacter regex always use worker."""
    return all(isinstance(item, dict) and isinstance(item.get("regex"), str)
               and not REGEX_META.intersection(item["regex"]) for item in assertions)


def _junit(data, definition):
    import xml.etree.ElementTree as ET

    root = ET.fromstring(data)
    required = [re.compile(pattern) for pattern in definition.get("required_cases", [])]
    found, passed = [False] * len(required), [False] * len(required)
    names = []
    tests = skipped = failed = shortened_names = 0
    for case in root.iter("testcase"):
        tests += 1
        name = f"{case.get('classname', '')}::{case.get('name', '')}"
        is_skipped = case.find("skipped") is not None
        is_failed = case.find("failure") is not None or case.find("error") is not None
        skipped += is_skipped
        failed += is_failed
        if len(names) < CASE_SUMMARY_LIMIT:
            shortened_names += len(name) > CASE_NAME_CHARS
            names.append(name[:CASE_NAME_CHARS])
        for index, pattern in enumerate(required):
            if pattern.search(name):
                found[index] = True
                passed[index] |= not is_skipped and not is_failed
    errors = []
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
    return errors, {"format": "junit", "cases": names, "tests": tests, "skipped": skipped,
                    "failed": failed, "cases_truncated": tests > len(names) or bool(shortened_names),
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
                data = inputs.read(item["path"])
                if definition["format"] == "junit":
                    report_errors, report = _junit(data, definition)
                    errors.extend(report_errors)
                    reports.append(report)
                elif definition["format"] == "json":
                    value = json.loads(data.decode("utf-8-sig"))
                    for key, expected in definition.get("equals", {}).items():
                        cursor = value
                        for segment in key.split("."):
                            if not isinstance(cursor, dict):
                                raise ValueError("JSON report path crosses a non-object")
                            cursor = cursor[segment]
                        if cursor != expected:
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
