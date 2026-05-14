"""Hermes Agent A2A plugin — auto-discovered and loaded by Hermes Agent."""

from .hermes_agent_a2a import HermesAgentA2APlugin, __version__
from .hermes_agent_a2a.plugin import register

__all__ = ["HermesAgentA2APlugin", "register", "__version__"]
