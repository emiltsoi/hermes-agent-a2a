"""Config schema for HermesA2A v2.

Defines required fields, defaults, and validation rules.
Unknown fields are warned (not rejected) for backward compat.
"""
import logging
from typing import Any

logger = logging.getLogger(__name__)

A2A_SCHEMA = {
    "type": "object",
    "required": ["platforms"],
    "properties": {
        "platforms": {
            "type": "object",
            "required": ["telegram"],
            "properties": {
                "telegram": {
                    "type": "object",
                    "required": ["bot_token", "default_chat_id"],
                    "properties": {
                        "bot_token": {"type": "string"},
                        "default_chat_id": {"type": "string"},
                    }
                }
            }
        },
        "enabled": {"type": "boolean", "default": True},
        "vault": {"type": "string", "default": "auto"},
        "defaults": {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "default": "telegram"},
                "chat_type": {"type": "string", "default": "dm"},
                "chat_id_resolver": {"type": "string", "default": "default_chat_id"},
            }
        },
        "routes": {
            "type": "object",
            "additionalProperties": True,
        }
    },
    "additionalProperties": True,
}


def validate_config(config: dict) -> tuple[bool, list[str]]:
    """Validate A2A config. Returns (valid, warnings).
    
    Checks:
    - Required top-level fields present
    - Field types match schema
    - Unknown keys generate warnings (not errors)
    
    Raises:
        RuntimeError: if required field is missing or type is invalid.
    """
    warnings = []

    # 1. Check required top-level fields
    required = A2A_SCHEMA.get("required", [])
    for field in required:
        if field not in config:
            raise RuntimeError(f"A2A config error: required field '{field}' is missing")

    # 2. Check field types (including nested required fields)
    _validate_types(
        config,
        A2A_SCHEMA["properties"],
        A2A_SCHEMA.get("required", []),
        "",
        warnings
    )

    # 3. Unknown key warnings (backward compat)
    known_keys = set(A2A_SCHEMA["properties"].keys())
    for key in config.keys():
        if key not in known_keys:
            warnings.append(f'Unknown config key "{key}" -- consider updating schema')

    return True, warnings


def _validate_types(value: Any, schema_props: dict, required: list, path: str, warnings: list[str]) -> None:
    """Recursively validate types and required fields against schema properties."""
    for key, schema_def in schema_props.items():
        current_path = f"{path}.{key}" if path else key

        # Check required sub-fields (for nested objects)
        if schema_def.get("required"):
            for req_key in schema_def["required"]:
                if req_key not in value.get(key, {}):
                    raise RuntimeError(
                        f"A2A config error: required field '{current_path}.{req_key}' is missing"
                    )

        if key not in value:
            continue

        val = value[key]
        expected_type = schema_def.get("type")

        # Type checks
        type_map = {"string": str, "boolean": bool, "object": dict, "number": (int, float)}
        if expected_type in type_map:
            py_types = type_map[expected_type]
            if not isinstance(val, py_types):
                raise RuntimeError(
                    f"A2A config error: '{current_path}' must be type {expected_type}, "
                    f"got {type(val).__name__} ('{repr(val)[:30]}')"
                )

        # Recurse into nested object
        if expected_type == "object" and isinstance(val, dict):
            nested_props = schema_def.get("properties", {})
            nested_required = schema_def.get("required", [])
            _validate_types(val, nested_props, nested_required, current_path, warnings)


def apply_defaults(config: dict) -> dict:
    """Apply schema defaults to config, filling in missing fields.

    Does NOT modify the input dict. Returns a new dict with defaults applied.
    """
    result = dict(config)

    if "enabled" not in result:
        result["enabled"] = True
    if "vault" not in result:
        result["vault"] = "auto"
    if "defaults" not in result:
        result["defaults"] = {
            "platform": "telegram",
            "chat_type": "dm",
            "chat_id_resolver": "default_chat_id",
        }
    elif isinstance(result["defaults"], dict):
        defaults = result["defaults"]
        result["defaults"] = dict(defaults)
        if "platform" not in defaults:
            result["defaults"]["platform"] = "telegram"
        if "chat_type" not in defaults:
            result["defaults"]["chat_type"] = "dm"
        if "chat_id_resolver" not in defaults:
            result["defaults"]["chat_id_resolver"] = "default_chat_id"

    return result
