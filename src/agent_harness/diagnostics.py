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
SUSPECT_VALUE = re.compile(r"(?i)(?:\bBearer\s|\bsk-[A-Za-z0-9]|https?://[^/\s]+@|(?:api[_-]?key|password|secret)\s*[=:])")


def _settings(data: dict) -> tuple[dict, list[str]]:
    values = {}
    invalid = []
    for key in (*STRING_SETTINGS, *INTEGER_SETTINGS):
        if key not in data:
            continue
        value = data[key]
        valid = isinstance(value, str) if key in STRING_SETTINGS else isinstance(value, int) and not isinstance(value, bool)
        if not valid:
            invalid.append(key)
        elif isinstance(value, str) and (len(value) > 200 or "\n" in value or SUSPECT_VALUE.search(value)):
            values[key] = "<redacted>"
        else:
            values[key] = value
    return values, invalid


def _source(path: Path, kind: str, data: dict | None = None, name: str | None = None) -> dict:
    result = {"source": str(path), "source_kind": kind, "status": "read", "settings": {}}
    if name is not None:
        result["name"] = name
    if data is None:
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as error:
            # TOML errors or arbitrary instruction text must not become a way
            # to print user config contents into the diagnostic report.
            result.update(status="unreadable", error_type=type(error).__name__)
            return result
    result["settings"], invalid = _settings(data)
    if invalid:
        result["invalid_setting_types"] = invalid
    warnings = []
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
    profiles = [
        _source(path, "profile_file", name=path.name.removesuffix(".config.toml"))
        for path in sorted(home.glob("*.config.toml")) if path.is_file()
    ]
    roles = [
        _source(path, "agent_role_file", name=path.stem)
        for path in sorted((home / "agents").glob("*.toml")) if path.is_file()
    ]
    # Older configs can still contain inline profiles. Report their explicit
    # values with an origin, without claiming this client loads that format.
    inline_profiles = []
    inline = base_config.get("profiles", {})
    if isinstance(inline, dict):
        for name, values in sorted(inline.items()):
            if isinstance(values, dict):
                row = _source(home / "config.toml", "inline_profile_table", values, name)
                row["table"] = "profiles." + name
                inline_profiles.append(row)
    return {
        "observation": "files_only",
        "effective_runtime": "unknown_until_client_probe",
        "base": _source(home / "config.toml", "base_config", base_config),
        "profile_files": profiles,
        "inline_profile_tables": inline_profiles,
        "agent_role_files": roles,
        "profile_selection": "not_observed; a configured name is not proof of the active --profile",
        "inheritance": "Missing role/profile fields inherit or use runtime defaults; this report does not resolve them",
        "limits": "CLI/session/project/managed overrides and model capability limits are not observed",
    }
