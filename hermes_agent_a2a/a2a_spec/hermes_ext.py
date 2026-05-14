"""Hermes metadata extension for Google A2A-shaped task envelopes."""

from typing import Optional


def build_hermes_metadata(
    route: str = "protocol",
    execution: str = "remote_a2a",
    delivery: Optional[str] = None,
    reply_mode: Optional[str] = None,
    isolation: Optional[str] = None,
) -> dict:
    metadata = {
        "version": "1",
        "route": route,
        "execution": execution,
    }
    if delivery:
        metadata["delivery"] = delivery
    if reply_mode:
        metadata["reply_mode"] = reply_mode
    if isolation:
        metadata["isolation"] = isolation
    return metadata
