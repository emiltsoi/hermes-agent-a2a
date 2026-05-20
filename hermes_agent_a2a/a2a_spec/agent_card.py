"""Google A2A AgentCard dataclasses — spec-compliant per a2a.proto.

Spec reference: a2a.proto:356-447 (AgentCard, AgentProvider, AgentCapabilities, AgentSkill, AgentInterface)
"""

from dataclasses import dataclass, asdict
from typing import Optional, List


# ---------------------------------------------------------------------------
# AgentCard dataclasses — spec-compliant
# ---------------------------------------------------------------------------


@dataclass
class AgentProvider:
    """AgentProvider per a2a.proto:396-403.

    Per spec: url (REQUIRED), organization (REQUIRED).
    """
    url: str
    organization: str


@dataclass
class AgentSkill:
    """AgentSkill per a2a.proto:430-447.

    Per spec: id (REQUIRED), name (REQUIRED), description (REQUIRED), tags (REQUIRED).
    """
    id: str
    name: str
    description: str
    tags: List[str]
    examples: Optional[List[str]] = None
    input_modes: Optional[List[str]] = None
    output_modes: Optional[List[str]] = None


@dataclass
class AgentCapabilities:
    """AgentCapabilities per a2a.proto:405-416.

    Per spec: streaming, push_notifications, extensions, extended_agent_card.
    """
    streaming: bool = False
    push_notifications: bool = False
    extensions: bool = False
    extended_agent_card: bool = False


@dataclass
class AgentInterface:
    """AgentInterface per a2a.proto:336-350.

    Per spec: url (REQUIRED), protocol_binding (REQUIRED), protocol_version (REQUIRED).
    """
    url: str
    protocol_binding: str
    protocol_version: str
    tenant: Optional[str] = None


@dataclass
class ExtendedAgentCard:
    """Full ExtendedAgentCard per a2a.proto:356-393.

    Fields:
      name, description (REQUIRED)
      url, version, documentation_url (optional)
      provider (REQUIRED)
      capabilities (REQUIRED)
      supported_interfaces (optional)
      default_input_modes, default_output_modes (REQUIRED repeated strings)
      skills (optional)
      security_schemes, security_requirements (optional)
    """
    name: str
    description: str
    provider: AgentProvider
    capabilities: AgentCapabilities
    default_input_modes: List[str]
    default_output_modes: List[str]
    url: Optional[str] = None
    version: Optional[str] = None
    documentation_url: Optional[str] = None
    supported_interfaces: Optional[List[AgentInterface]] = None
    skills: Optional[List[AgentSkill]] = None
    security_schemes: Optional[dict] = None
    security_requirements: Optional[List[str]] = None


def build_extended_agent_card(overrides: Optional[dict] = None) -> dict:
    """Build a full ExtendedAgentCard dict.

    Starts with base AgentCard fields and adds ExtendedAgentCard fields.
    Merges any overrides from the argument.

    Default provider: url="https://hermes.fleet", organization="Hermes Fleet"
    """
    card = {
        "name": "hermes-agent",
        "description": "Hermes fleet agent with A2A HTTP/JSON-RPC protocol support — exposes A2A server, HMAC auth, push notifications, SSE streaming, and Telegram session routing.",
        "url": None,
        "version": None,
        "documentation_url": None,
        "supported_interfaces": None,
        "provider": asdict(AgentProvider(
            url="https://hermes.fleet",
            organization="Hermes Fleet",
        )),
        "capabilities": asdict(AgentCapabilities()),
        "default_input_modes": ["text"],
        "default_output_modes": ["text"],
        "skills": [
            {
                "id": "brainstorming",
                "name": "brainstorming",
                "description": "Fleet-shared creative and ideation skill for structured brainstorming sessions. Use before any creative work.",
                "tags": ["creative", "ideation", "fleet-shared"],
            }
        ],
        "security_schemes": None,
        "security_requirements": None,
    }
    if overrides:
        for key, value in overrides.items():
            if key in ("provider", "capabilities") and isinstance(value, dict):
                card[key] = {**card[key], **value}
            else:
                card[key] = value
    return card


# ---------------------------------------------------------------------------
# Legacy aliases for backward compatibility
# ---------------------------------------------------------------------------

@dataclass
class Provider:
    """Legacy alias for AgentProvider — DEPRECATED, use AgentProvider."""
    organization: str
    url: Optional[str] = None


@dataclass
class Skill:
    """Legacy alias for AgentSkill — DEPRECATED, use AgentSkill."""
    id: str
    name: str
    description: Optional[str] = None
    tags: Optional[List[str]] = None


# Backward-compat wrapper for ExtendedAgentCard with old field names
@dataclass
class LegacyAgentCard:
    """Legacy ExtendedAgentCard with camelCase field names — DEPRECATED."""
    name: str
    description: str
    provider: Provider
    agentCapabilities: AgentCapabilities
    defaultInputModes: List[str]
    defaultOutputModes: List[str]
    url: Optional[str] = None
    version: Optional[str] = None
    documentationUrl: Optional[str] = None
    skills: Optional[List[Skill]] = None


# ---------------------------------------------------------------------------
# Legacy skill helpers (pre-existing)
# ---------------------------------------------------------------------------


def skill_names(agent_info: dict) -> set[str]:
    if not isinstance(agent_info, dict):
        return set()
    known_skills = agent_info.get("metadata", {}).get("skills", []) or agent_info.get("skills", [])
    return {
        str(item.get("name") or item.get("id") or "").lower()
        for item in known_skills
        if isinstance(item, dict) and (item.get("name") or item.get("id"))
    }


def validate_skill(agent_info: dict, skill: str) -> tuple[bool, list[str]]:
    names = skill_names(agent_info)
    if not skill or not names:
        return True, sorted(names)
    return skill.lower() in names, sorted(names)