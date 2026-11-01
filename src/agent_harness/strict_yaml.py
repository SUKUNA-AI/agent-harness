"""Bounded YAML with JSON scalar rules and closed, explicit schemas.

The vendored parser is PyYAML 6.0.3 (MIT), used without executable constructors.
This module deliberately does not implement the YAML grammar itself.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re

from ._vendor import yaml
from .file_io import read_regular_file

MAX_BYTES = 2 * 1024 * 1024
MAX_DEPTH = 48
MAX_NODES = 100000
MAX_STRING = 262144
SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"


class StrictYAMLError(ValueError):
    pass


class _Loader(yaml.SafeLoader):
    yaml_implicit_resolvers = {}

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key == "<<":
                raise StrictYAMLError("Mapping keys must be strings; merge keys are forbidden")
            if key in result:
                raise StrictYAMLError("Duplicate YAML key: " + key)
            result[key] = self.construct_object(value_node, deep=deep)
        return result


# Only JSON spelling is converted; yes/no/on/off, dates and octal remain strings.
_Loader.add_implicit_resolver("tag:yaml.org,2002:bool", re.compile(r"^(?:true|false)$"), list("tf"))
_Loader.add_implicit_resolver("tag:yaml.org,2002:null", re.compile(r"^null$"), ["n"])
_Loader.add_implicit_resolver("tag:yaml.org,2002:int", re.compile(r"^-?(?:0|[1-9][0-9]*)$"), list("-0123456789"))
_Loader.add_implicit_resolver("tag:yaml.org,2002:float", re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+(?:[eE][+-]?[0-9]+)?|[eE][+-]?[0-9]+)$"), list("-0123456789"))


def _bounds(value, depth=0, counter=None):
    counter = [0] if counter is None else counter
    counter[0] += 1
    if depth > MAX_DEPTH or counter[0] > MAX_NODES:
        raise StrictYAMLError("Structured input exceeds depth/node limit")
    if type(value) not in (dict, list, str, int, float, bool, type(None)):
        raise StrictYAMLError("Only JSON-compatible YAML values are permitted")
    if isinstance(value, str) and len(value) > MAX_STRING:
        raise StrictYAMLError("String exceeds field length limit")
    if isinstance(value, float) and not math.isfinite(value):
        raise StrictYAMLError("Nonfinite numbers are forbidden")
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or key == "<<":
                raise StrictYAMLError("Invalid mapping key")
            _bounds(key, depth + 1, counter)
            _bounds(child, depth + 1, counter)
    elif isinstance(value, list):
        for child in value:
            _bounds(child, depth + 1, counter)


def _schema(value):
    if isinstance(value, (str, Path)):
        path = Path(value)
        if not path.is_absolute():
            path = SCHEMAS / path
        data = read_regular_file(path, max_bytes=MAX_BYTES)
        return json.loads(data)
    if isinstance(value, (dict, bool)):
        return value
    raise StrictYAMLError("Schema must be an object, boolean, or schema filename")


def _equal(a, b):
    from .validation import json_equal
    return json_equal(a, b)


def validate(value, schema):
    """Validate the supported JSON Schema vocabulary; unknown keywords fail closed.

    Object schemas are closed by default. Explicit additionalProperties permits
    dictionary-like extension fields; it never happens through silent ignoring.
    """
    _bounds(value)
    schema = _schema(schema)
    root = schema
    known = {"$schema", "$id", "$defs", "$ref", "title", "description", "default", "examples", "type", "const", "enum", "properties", "required", "additionalProperties", "propertyNames", "minProperties", "maxProperties", "items", "prefixItems", "minItems", "maxItems", "uniqueItems", "minLength", "maxLength", "pattern", "format", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "allOf", "anyOf", "oneOf", "not", "if", "then", "else", "dependentRequired"}

    def visit(v, s, at="$", depth=0):
        if depth > MAX_DEPTH:
            raise StrictYAMLError(at + ": schema recursion limit")
        if s is True:
            return
        if s is False or not isinstance(s, dict):
            raise StrictYAMLError(at + ": schema forbids value")
        unknown = set(s) - known
        if unknown:
            raise StrictYAMLError("Unsupported schema keywords: " + ", ".join(sorted(unknown)))
        if "$ref" in s:
            ref = s["$ref"]
            if not isinstance(ref, str) or not ref.startswith("#/"):
                raise StrictYAMLError("Only document-local schema references are permitted")
            target = root
            try:
                for part in ref[2:].split("/"):
                    target = target[part.replace("~1", "/").replace("~0", "~")]
            except (KeyError, TypeError):
                raise StrictYAMLError("Unknown schema reference: " + ref) from None
            visit(v, target, at, depth + 1)
        types = {"object": type(v) is dict, "array": type(v) is list, "string": type(v) is str, "integer": type(v) is int, "number": type(v) in (int, float), "boolean": type(v) is bool, "null": v is None}
        if "type" in s:
            allowed = s["type"] if isinstance(s["type"], list) else [s["type"]]
            if not any(types.get(t, False) for t in allowed):
                raise StrictYAMLError(at + ": expected " + str(s["type"]))
        if "const" in s and not _equal(v, s["const"]):
            message = "unsupported schema_version; migrate with a compatible harness" if at.endswith(".schema_version") else "value differs from const"
            raise StrictYAMLError(at + ": " + message)
        if "enum" in s and not any(_equal(v, x) for x in s["enum"]):
            raise StrictYAMLError(at + ": value outside enum")
        for branch in s.get("allOf", []):
            visit(v, branch, at, depth + 1)
        for key in ("anyOf", "oneOf"):
            if key in s:
                successes = 0
                for branch in s[key]:
                    try:
                        visit(v, branch, at, depth + 1)
                        successes += 1
                    except StrictYAMLError:
                        pass
                if (key == "anyOf" and not successes) or (key == "oneOf" and successes != 1):
                    raise StrictYAMLError(at + ": " + key + " failed")
        if "not" in s:
            try:
                visit(v, s["not"], at, depth + 1)
            except StrictYAMLError:
                pass
            else:
                raise StrictYAMLError(at + ": forbidden schema match")
        if "if" in s:
            try:
                visit(v, s["if"], at, depth + 1)
                branch = "then"
            except StrictYAMLError:
                branch = "else"
            if branch in s:
                visit(v, s[branch], at, depth + 1)
        if isinstance(v, dict):
            for key in s.get("required", []):
                if key not in v:
                    raise StrictYAMLError(at + ": missing required field " + key)
            properties = s.get("properties", {})
            # A schema containing only combinators does not additionally close
            # an object already described by its referenced/combined schemas.
            close = "properties" in s or s.get("type") == "object" or "additionalProperties" in s
            for key, child in v.items():
                if "propertyNames" in s:
                    visit(key, s["propertyNames"], at + ".<key>", depth + 1)
                if key in properties:
                    visit(child, properties[key], at + "." + key, depth + 1)
                elif close:
                    additional = s.get("additionalProperties", False)
                    if additional is False:
                        raise StrictYAMLError(at + ": unknown field " + key)
                    visit(child, additional, at + "." + key, depth + 1)
            for key, deps in s.get("dependentRequired", {}).items():
                if key in v and any(dep not in v for dep in deps):
                    raise StrictYAMLError(at + ": dependent required field missing")
            if len(v) < s.get("minProperties", 0) or len(v) > s.get("maxProperties", MAX_NODES):
                raise StrictYAMLError(at + ": object size limit")
        elif isinstance(v, list):
            if len(v) < s.get("minItems", 0) or len(v) > s.get("maxItems", MAX_NODES):
                raise StrictYAMLError(at + ": array size limit")
            if s.get("uniqueItems"):
                seen = set()
                for child in v:
                    marker = json.dumps(child, sort_keys=True, ensure_ascii=False)
                    if marker in seen:
                        raise StrictYAMLError(at + ": duplicate array item")
                    seen.add(marker)
            prefix = s.get("prefixItems", [])
            for i, child in enumerate(v):
                visit(child, prefix[i] if i < len(prefix) else s.get("items", True), at + f"[{i}]", depth + 1)
        elif isinstance(v, str):
            if len(v) < s.get("minLength", 0) or len(v) > s.get("maxLength", MAX_STRING):
                raise StrictYAMLError(at + ": string length limit")
            if "pattern" in s and re.search(s["pattern"], v) is None:
                raise StrictYAMLError(at + ": pattern mismatch")
            if s.get("format") in ("date-time", "utc"):
                try:
                    parsed = datetime.fromisoformat(v.replace("Z", "+00:00"))
                    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
                        raise ValueError("not UTC")
                except ValueError:
                    raise StrictYAMLError(at + ": UTC timestamp required") from None
            elif s.get("format"):
                raise StrictYAMLError(at + ": unsupported format " + s["format"])
        elif type(v) in (int, float):
            if v < s.get("minimum", -math.inf) or v > s.get("maximum", math.inf) or v <= s.get("exclusiveMinimum", -math.inf) or v >= s.get("exclusiveMaximum", math.inf):
                raise StrictYAMLError(at + ": number outside bounds")
            if "multipleOf" in s and not math.isclose(v / s["multipleOf"], round(v / s["multipleOf"])):
                raise StrictYAMLError(at + ": not a multiple")
    visit(value, schema)
    return value


def loads(text, schema=None):
    if isinstance(text, bytes):
        text = text.decode("utf-8-sig")
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_BYTES:
        raise StrictYAMLError("YAML exceeds byte limit or is not text")
    try:
        depth = 0
        for token in yaml.scan(text, Loader=_Loader):
            if isinstance(token, (yaml.TagToken, yaml.AnchorToken, yaml.AliasToken, yaml.DirectiveToken)):
                raise StrictYAMLError("Tags, anchors, aliases and directives are forbidden")
            if isinstance(token, (yaml.BlockMappingStartToken, yaml.BlockSequenceStartToken, yaml.FlowMappingStartToken, yaml.FlowSequenceStartToken)):
                depth += 1
                if depth > MAX_DEPTH:
                    raise StrictYAMLError("YAML exceeds depth limit")
            elif isinstance(token, (yaml.BlockEndToken, yaml.FlowMappingEndToken, yaml.FlowSequenceEndToken)):
                depth -= 1
        value = yaml.load(text, Loader=_Loader)
    except (yaml.YAMLError, RecursionError, UnicodeError) as error:
        raise StrictYAMLError("Invalid Strict YAML: " + str(error)[:512]) from error
    _bounds(value)
    if schema is not None:
        validate(value, schema)
    return value


def dumps(value, schema=None):
    _bounds(value)
    if schema is not None:
        validate(value, schema)
    # SafeDumper emits no aliases because object sharing has no state semantics.
    class Dumper(yaml.SafeDumper):
        def ignore_aliases(self, data):
            return True
    result = yaml.dump(value, Dumper=Dumper, allow_unicode=True, sort_keys=True, default_flow_style=False)
    if len(result.encode("utf-8")) > MAX_BYTES:
        raise StrictYAMLError("YAML output exceeds byte limit")
    # Round-trip through our JSON scalar policy prevents PyYAML output surprises.
    if loads(result) != value:
        raise StrictYAMLError("Strict YAML roundtrip changed value")
    return result


def load(path, schema=None):
    return loads(read_regular_file(path, max_bytes=MAX_BYTES), schema)


def dump(path, value, schema=None, exclusive=False):
    from .supervisor_store import atomic_write
    atomic_write(Path(path), dumps(value, schema).encode("utf-8"), exclusive=exclusive)


def migrate_json(source, destination, schema):
    """Non-destructive, exclusive migration; caller supplies the legacy schema."""
    data = read_regular_file(source, max_bytes=MAX_BYTES)
    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result:
                raise StrictYAMLError("Duplicate legacy JSON key: " + key)
            result[key] = value
        return result
    try:
        value = json.loads(data.decode("utf-8-sig"), object_pairs_hook=pairs)
    except (ValueError, UnicodeError) as error:
        raise StrictYAMLError("Legacy migration rejected: " + str(error)[:256]) from error
    validate(value, schema)
    dump(destination, value, schema, exclusive=True)
    import hashlib
    return {"status": "migrated", "source_preserved": True, "source_sha256": hashlib.sha256(data).hexdigest(), "destination_sha256": hashlib.sha256(read_regular_file(destination, max_bytes=MAX_BYTES)).hexdigest()}
