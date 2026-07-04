"""Persistence helpers for the minimal Web configuration API.

The Web API stores the same YAML document that ``load_config`` already
understands.  Secrets are never generated here and blank secret fields in an
update mean "keep the existing value" so the UI can submit redacted forms
without leaking or deleting credentials accidentally.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml
from pydantic import TypeAdapter, ValidationError

from .schema import Config

SECRET_KEYS = {"api_hash", "password"}
WRITEABLE_TOP_LEVEL_FIELDS = {
    "api_id",
    "api_hash",
    "phone_number",
    "sources",
    "global_filters",
    "download_dir",
    "session_dir",
    "log_file",
    "flat_structure",
    "track_downloads",
    "daemon",
    "proxy",
    "max_concurrent_downloads",
    "verbosity",
    "test_mode",
}

_config_adapter = TypeAdapter(Config)


def load_config_document(path: str | Path) -> dict[str, Any]:
    """Load a raw YAML config document, returning ``{}`` for missing files."""
    config_path = Path(path)
    if not config_path.exists():
        return {}

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("Configuration document must be a mapping")
    return raw


def _mask_secret(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 8:
        return "********"
    return f"{text[:4]}…{text[-4:]}"


def redacted_config(data: dict[str, Any]) -> dict[str, Any]:
    """Return a copy safe to expose via the Web API."""
    result = deepcopy(data)

    def redact_mapping(mapping: dict[str, Any]) -> None:
        for key, value in list(mapping.items()):
            if isinstance(value, dict):
                redact_mapping(value)
                continue
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        redact_mapping(item)
                continue
            if key in SECRET_KEYS:
                mapping[f"{key}_set"] = bool(value)
                mapping[key] = _mask_secret(value)

    redact_mapping(result)
    return result


def _merge_secret_if_blank(new_data: dict[str, Any], old_data: dict[str, Any], key_path: tuple[str, ...]) -> None:
    """Preserve an existing secret when the submitted value is blank/masked."""
    new_parent: dict[str, Any] = new_data
    old_parent: dict[str, Any] = old_data
    for key in key_path[:-1]:
        new_child = new_parent.get(key)
        old_child = old_parent.get(key)
        if not isinstance(new_child, dict) or not isinstance(old_child, dict):
            return
        new_parent = new_child
        old_parent = old_child

    key = key_path[-1]
    submitted = new_parent.get(key)
    existing = old_parent.get(key)
    if existing and (submitted is None or submitted == "" or submitted == "********" or "…" in str(submitted)):
        new_parent[key] = existing


def normalize_web_payload(payload: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    """Filter and validate a Web config payload before persistence."""
    if not isinstance(payload, dict):
        raise ValueError("Configuration payload must be a JSON object")

    filtered = {k: deepcopy(v) for k, v in payload.items() if k in WRITEABLE_TOP_LEVEL_FIELDS}
    existing = existing or {}

    _merge_secret_if_blank(filtered, existing, ("api_hash",))
    _merge_secret_if_blank(filtered, existing, ("proxy", "password"))

    # Pydantic validates nested structures and coerces Path/datetime-compatible
    # values.  We dump in JSON mode so yaml.safe_dump receives plain types.
    validated = _config_adapter.validate_python(filtered)
    return validated.model_dump(mode="json", exclude_none=True)


def save_config_document(path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and atomically write the Web-submitted YAML configuration."""
    config_path = Path(path)
    existing = load_config_document(config_path)
    normalized = normalize_web_payload(payload, existing)

    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = config_path.with_suffix(config_path.suffix + ".tmp")
    tmp_path.write_text(
        yaml.safe_dump(normalized, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    tmp_path.replace(config_path)
    try:
        config_path.chmod(0o600)
    except OSError:
        pass
    return normalized


__all__ = [
    "ValidationError",
    "load_config_document",
    "normalize_web_payload",
    "redacted_config",
    "save_config_document",
]
