"""HermesA2A v3 plugin — auto-discovered and loaded by Hermes Agent."""

from hermes_agent_a2a import HermesA2AV3Plugin, __version__
from hermes_agent_a2a.plugin import register

__all__ = ["HermesA2AV3Plugin", "register", "__version__"]
