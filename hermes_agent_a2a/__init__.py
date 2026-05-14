"""Hermes Agent A2A plugin — auto-discovered and loaded by Hermes Agent."""

from .plugin import HermesAgentA2APlugin, __version__, register

__all__ = ["HermesAgentA2APlugin", "register", "__version__"]
