"""Google A2A-shaped helpers plus Hermes metadata extensions."""

from dataclasses import dataclass, asdict
from typing import Optional, List

from .hermes_ext import build_hermes_metadata


# ---------------------------------------------------------------------------
# ExtendedAgentCard dataclasses
# ---------------------------------------------------------------------------


@dataclass
class Provider:
    """Google A2A ExtendedAgentCard Provider.

    Per spec: organization (required), category (optional).
    """
    organization: str
    category: Optional[str] = None


@dataclass
class Skill:
    """Google A2A ExtendedAgentCard Skill.

    Per spec: id (required), name (required), description (optional), tags (optional).
    """
    id: str
    name: str
    description: Optional[str] = None
    tags: Optional[List[str]] = None


@dataclass
class AgentCapabilities:
    """Google A2A ExtendedAgentCard AgentCapabilities.

    Per spec: streaming, pushNotifications, stateTransitionHistory — all bool, default False.
    """
    streaming: bool = False
    pushNotifications: bool = False
    stateTransitionHistory: bool = False


@dataclass
class ExtendedAgentCard:
    """Google A2A ExtendedAgentCard.

    Combines standard AgentCard fields with ExtendedAgentCard-specific fields.
    """
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


def build_extended_agent_card(overrides: Optional[dict] = None) -> dict:
    """Build a full ExtendedAgentCard dict.

    Starts with base AgentCard fields and adds ExtendedAgentCard fields.
    Merges any overrides from the argument.

    Default provider: organization="Hermes Fleet", category="official"
    """
    card = {
        "name": "hermes-agent",
        "description": "A self-improving AI agent powered by Hermes",
        "url": None,
        "version": None,
        "documentationUrl": None,
        "provider": asdict(Provider(organization="Hermes Fleet", category="official")),
        "agentCapabilities": asdict(AgentCapabilities()),
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
    }
    if overrides:
        for key, value in overrides.items():
            if key in ("provider", "agentCapabilities") and isinstance(value, dict):
                card[key] = {**card[key], **value}
            else:
                card[key] = value
    return card


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