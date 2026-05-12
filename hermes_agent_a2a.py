"""Re-export hermes_agent_a2a from the plugin root for import compatibility.

The package is installed as 'hermes-agent-a2a' (hyphens) but Python
requires 'hermes_agent_a2a' (underscores) for imports. This module
resolves the mismatch.
"""
from hermes_agent_a2a.src.plugin import HermesA2AV3Plugin, __version__

__all__ = ["HermesA2AV3Plugin", "__version__"]
