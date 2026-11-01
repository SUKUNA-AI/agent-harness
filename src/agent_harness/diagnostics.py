"""Read-only, allowlisted configuration inventory; never an effective-config resolver."""
from __future__ import annotations

from pathlib import Path
import re
import tomllib


STRING_SETTINGS = (
    "model", "model_provider", "model_reasoning_effort", "model_reasoning_summary",
    "model_verbosity", "service_tier", "profile",
)
INTEGER_SETTINGS = (
    "model_context_window", "model_auto_compact_token_limit", "tool_output_token_limit",
)
AGENT_DEFAULTS = ("default_subagent_model", "default_subagent_reasoning_effort")
SUSPECT_VALUE = re.compile(r"(?i)(?:\bBearer\s|\bsk-[A-Za-z0-9]|https?://[^/\s]+@|(?:api[_-]?key|password|secret)\s*[=:])")


def _text(value: str) -> str:
    return "<redacted>" if len(value) > 200 or "\n" in value or SUSPECT_VALUE.search(value) else value


def _settings(data: dict, strings: tuple = STRING_SETTINGS, integers: tuple = INTEGER_SETTINGS) -> tuple[dict, list[str]]:
    values = {}
    invalid = []
    for key in (*strings, *integers):
        if key not in data:
            continue
        value = data[key]
        valid = isinstance(value, str) if key in strings else isinstance(value, int) and not isinstance(value, bool)
        if not valid:
            invalid.append(key)
        else:
            values[key] = _text(value) if isinstance(value, str) else value
    return values, invalid


def _source(path: Path, kind: str, data: dict | None = None, name: str | None = None, *, cache: dict | None = None, inspect_agents: bool = True, table: str = "") -> dict:
    result = {"source": str(path), "source_kind": kind, "status": "read", "settings": {}}
    if name is not None:
        result["name"] = _text(name)
    if kind == "agent_role_file":
        result.update(file_stem=path.stem, declared_name=None, name=None)
    if data is None:
        try:
            key = str(path.resolve())
            if cache is not None and key in cache:
                data, error_type = cache[key]
            else:
                try:
                    data, error_type = tomllib.loads(path.read_text(encoding="utf-8-sig")), None
                except (OSError, ValueError) as error:
                    data, error_type = None, type(error).__name__
                if cache is not None:
                    cache[key] = data, error_type
            if error_type:
                result.update(status="unreadable", error_type=error_type)
                return result
        except (OSError, ValueError, RuntimeError) as error:
            # TOML errors or arbitrary instruction text must not become a way
            # to print user config contents into the diagnostic report.
            result.update(status="unreadable", error_type=type(error).__name__)
            return result
    result["settings"], invalid = _settings(data)
    warnings = []
    if kind == "agent_role_file":
        declared = data.get("name")
        if isinstance(declared, str) and declared.strip():
            result["declared_name"] = result["name"] = _text(declared)
        else:
            warnings.append("missing_or_invalid_declared_role_name")
    if inspect_agents and "agents" in data:
        agents = data["agents"]
        if not isinstance(agents, dict):
            invalid.append("agents")
        else:
            defaults, bad_defaults = _settings(agents, AGENT_DEFAULTS, ())
            result["agent_defaults"] = defaults
            invalid.extend("agents." + field for field in bad_defaults)
            refs = []
            for role, values in sorted(agents.items()):
                if not isinstance(values, dict) or "config_file" not in values:
                    continue
                ref = {"role_name": _text(role), "declaring_source": str(path), "declaring_table": (table + "." if table else "") + "agents." + _text(role)}
                filename = values["config_file"]
                if not isinstance(filename, str) or not filename.strip() or _text(filename) != filename:
                    ref.update(status="invalid_config_file", error_type="invalid_or_redacted_path")
                else:
                    ref["config_file"] = filename
                    target = Path(filename)
                    if not target.is_absolute():
                        target = path.parent / target
                    ref["configuration"] = _source(target, "agent_role_config", cache=cache, inspect_agents=False)
                refs.append(ref)
            if refs:
                result["agent_role_references"] = refs
    if invalid:
        result["invalid_setting_types"] = invalid
    window = result["settings"].get("model_context_window")
    threshold = result["settings"].get("model_auto_compact_token_limit")
    if isinstance(threshold, int) and isinstance(window, int) and threshold >= window:
        warnings.append("compaction_threshold_at_or_above_declared_context_window")
    if isinstance(threshold, int) and threshold <= 0:
        warnings.append("non_positive_compaction_threshold_runtime_semantics_not_inferred")
    if warnings:
        result["warnings"] = warnings
    return result


def model_configuration(home: Path, base_config: dict) -> dict:
    """Inventory sources without guessing precedence, selected profile, or CLI overrides."""
    cache = {}
    profiles = [
        _source(path, "profile_file", name=path.name.removesuffix(".config.toml"), cache=cache)
        for path in sorted(home.glob("*.config.toml")) if path.is_file()
    ]
    roles = [
        _source(path, "agent_role_file", cache=cache, inspect_agents=False)
        for path in sorted((home / "agents").glob("*.toml")) if path.is_file()
    ]
    # Older configs can still contain inline profiles. Report their explicit
    # values with an origin, without claiming this client loads that format.
    inline_profiles = []
    inline = base_config.get("profiles", {})
    if isinstance(inline, dict):
        for name, values in sorted(inline.items()):
            if isinstance(values, dict):
                row = _source(home / "config.toml", "inline_profile_table", values, name, cache=cache, table="profiles." + name)
                row["table"] = "profiles." + name
                inline_profiles.append(row)
    names = {}
    for role in roles:
        name = role.get("declared_name")
        if name and name != "<redacted>":
            names.setdefault(name, []).append(role["source"])
    return {
        "observation": "files_only",
        "effective_runtime": "unknown_until_client_probe",
        "base": _source(home / "config.toml", "base_config", base_config, cache=cache),
        "profile_files": profiles,
        "inline_profile_tables": inline_profiles,
        "agent_role_files": roles,
        "declared_role_name_collisions": [{"name": name, "sources": paths} for name, paths in sorted(names.items()) if len(paths) > 1],
        "role_settings": "Declared fields only; support and ignored overrides depend on the observed client, not file presence",
        "profile_selection": "not_observed; a configured name is not proof of the active --profile",
        "inheritance": "Missing role/profile fields inherit or use runtime defaults; this report does not resolve them",
        "limits": "CLI/session/project/managed overrides and model capability limits are not observed",
    }
