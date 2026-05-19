"""Tests for ExtendedAgentCard dataclasses and build_extended_agent_card()."""

import pytest
from hermes_agent_a2a.a2a_spec import (
    Provider,
    Skill,
    AgentCapabilities,
    ExtendedAgentCard,
    build_extended_agent_card,
)


class TestProvider:
    def test_provider_required_organization(self) -> None:
        p = Provider(organization="Hermes Fleet")
        assert p.organization == "Hermes Fleet"
        assert p.category is None

    def test_provider_with_category(self) -> None:
        p = Provider(organization="Acme Corp", category="community")
        assert p.organization == "Acme Corp"
        assert p.category == "community"

    def test_provider_repr(self) -> None:
        p = Provider(organization="Test Org", category="official")
        assert "Test Org" in repr(p)
        assert "official" in repr(p)


class TestSkill:
    def test_skill_required_id_and_name(self) -> None:
        s = Skill(id="code_review", name="Code Review")
        assert s.id == "code_review"
        assert s.name == "Code Review"
        assert s.description is None
        assert s.tags is None

    def test_skill_with_optional_fields(self) -> None:
        s = Skill(
            id="data_analysis",
            name="Data Analysis",
            description="Analyze datasets",
            tags=["ml", "analytics"],
        )
        assert s.id == "data_analysis"
        assert s.name == "Data Analysis"
        assert s.description == "Analyze datasets"
        assert s.tags == ["ml", "analytics"]


class TestAgentCapabilities:
    def test_defaults(self) -> None:
        caps = AgentCapabilities()
        assert caps.streaming is False
        assert caps.pushNotifications is False
        assert caps.stateTransitionHistory is False

    def test_explicit_values(self) -> None:
        caps = AgentCapabilities(streaming=True, pushNotifications=True, stateTransitionHistory=True)
        assert caps.streaming is True
        assert caps.pushNotifications is True
        assert caps.stateTransitionHistory is True


class TestExtendedAgentCard:
    def test_required_fields(self) -> None:
        card = ExtendedAgentCard(
            name="Test Agent",
            description="A test agent",
            provider=Provider(organization="Test Org"),
            agentCapabilities=AgentCapabilities(),
            defaultInputModes=["text"],
            defaultOutputModes=["text"],
        )
        assert card.name == "Test Agent"
        assert card.description == "A test agent"
        assert isinstance(card.provider, Provider)
        assert isinstance(card.agentCapabilities, AgentCapabilities)
        assert card.defaultInputModes == ["text"]
        assert card.defaultOutputModes == ["text"]
        assert card.skills is None
        assert card.url is None
        assert card.version is None
        assert card.documentationUrl is None

    def test_skills_optional(self) -> None:
        card = ExtendedAgentCard(
            name="Skilled Agent",
            description="Agent with skills",
            provider=Provider(organization="Fleet"),
            agentCapabilities=AgentCapabilities(streaming=True),
            defaultInputModes=["text", "image"],
            defaultOutputModes=["text"],
            skills=[
                Skill(id="code", name="Code Assistant", tags=["dev"]),
                Skill(id="docs", name="Docs Assistant"),
            ],
        )
        assert len(card.skills) == 2
        assert card.skills[0].id == "code"
        assert card.skills[1].name == "Docs Assistant"


class TestBuildExtendedAgentCard:
    def test_returns_dict(self) -> None:
        card = build_extended_agent_card()
        assert isinstance(card, dict)

    def test_has_required_keys(self) -> None:
        card = build_extended_agent_card()
        assert "name" in card
        assert "description" in card
        assert "provider" in card
        assert "agentCapabilities" in card
        assert "defaultInputModes" in card
        assert "defaultOutputModes" in card

    def test_provider_defaults(self) -> None:
        card = build_extended_agent_card()
        assert card["provider"]["organization"] == "Hermes Fleet"
        assert card["provider"]["category"] == "official"

    def test_capabilities_defaults(self) -> None:
        card = build_extended_agent_card()
        caps = card["agentCapabilities"]
        assert caps["streaming"] is False
        assert caps["pushNotifications"] is False
        assert caps["stateTransitionHistory"] is False

    def test_default_modes(self) -> None:
        card = build_extended_agent_card()
        assert "text" in card["defaultInputModes"]
        assert "text" in card["defaultOutputModes"]

    def test_overrides_merged(self) -> None:
        card = build_extended_agent_card(
            overrides={
                "name": "Override Name",
                "provider": {"organization": "Custom Org", "category": "community"},
            }
        )
        assert card["name"] == "Override Name"
        assert card["provider"]["organization"] == "Custom Org"
        assert card["provider"]["category"] == "community"

    def test_overrides_preserve_defaults(self) -> None:
        card = build_extended_agent_card(overrides={"description": "Custom desc"})
        assert card["description"] == "Custom desc"
        assert card["provider"]["organization"] == "Hermes Fleet"

    def test_overrides_empty(self) -> None:
        card1 = build_extended_agent_card()
        card2 = build_extended_agent_card(overrides={})
        assert card1["name"] == card2["name"]
        assert card1["provider"] == card2["provider"]

    def test_skills_included_when_provided(self) -> None:
        card = build_extended_agent_card(
            overrides={
                "skills": [
                    {"id": "test", "name": "Test Skill", "description": "A test skill"}
                ]
            }
        )
        assert len(card["skills"]) == 1
        assert card["skills"][0]["id"] == "test"
        assert card["skills"][0]["name"] == "Test Skill"