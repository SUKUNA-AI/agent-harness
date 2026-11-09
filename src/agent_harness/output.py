"""Locale independent UTF-8 machine transport. Budget includes transport CRLF."""
from __future__ import annotations

import sys

from .core import HarnessError, canonical


def json_bytes(value, *, max_bytes=None):
    """Validate serialization and the complete transport bound before any write."""
    data = canonical(value) + b"\n"
    if max_bytes is not None and len(data) + 1 > max_bytes:
        raise HarnessError("BUDGET_TOO_SMALL: complete UTF-8 envelope exceeds budget")
    return data


def write_utf8(data, *, stream=None):
    """Write bytes to pipes regardless of PYTHONIOENCODING; no lossy escaping."""
    if isinstance(data, str):
        data = data.encode("utf-8", errors="strict")
    stream = sys.stdout if stream is None else stream
    binary = getattr(stream, "buffer", None)
    if binary is not None:
        binary.write(data)
        binary.flush()
    else:  # In-process text capture has no operating-system byte transport.
        stream.write(data.decode("utf-8", errors="strict"))
        stream.flush()


def write_json(value, *, max_bytes=None, stream=None):
    write_utf8(json_bytes(value, max_bytes=max_bytes), stream=stream)
