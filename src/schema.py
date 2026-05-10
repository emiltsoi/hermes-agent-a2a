"""Config schema for HermesA2A v2.

Defines required fields, defaults, and validation rules.
Unknown fields are warned (not rejected) for backward compat.
"""
import logging

logger = logging.getLogger(__name__)

A2A_SCHEMA = {
    "type": "object",
    "properties": {
        "enabled": {"type": "boolean", "default": True},
        "vault": {"type": "string", "default": "auto"},
        "platforms": {
            "type": "object",
            "properties": {
                "telegram": {
                    "type": "object",
                    "properties": {
                        "bot_token": {"type": "string"},
                        "default_chat_id": {"type": "string"},
                    }
                }
            }
        },
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
    """Validate A2A config. Returns (valid, warnings)."""
    warnings = []
    known_keys = set(A2A_SCHEMA["properties"].keys())
    for key in config.keys():
        if key not in known_keys:
            warnings.append(f'Unknown config key "{key}" -- consider updating schema')
    vault = config.get("vault", "auto")
    if vault not in ("auto", "none") and not isinstance(vault, str):
        warnings.append(f"Unexpected vault value: {vault}")
    return True, warnings


def apply_defaults(config: dict) -> dict:
    """Apply schema defaults to config, filling in missing fields."""
    if "enabled" not in config:
        config["enabled"] = True
    if "vault" not in config:
        config["vault"] = "auto"
    if "defaults" not in config:
        config["defaults"] = {
            "platform": "telegram",
            "chat_type": "dm",
            "chat_id_resolver": "default_chat_id",
        }
    return config
