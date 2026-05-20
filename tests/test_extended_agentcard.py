"""Tests for spec-compliant ExtendedAgentCard dataclasses and build_extended_agent_card()."""

import pytest
from hermes_agent_a2a.a2a_spec import (
    AgentProvider,
    AgentSkill,
    AgentCapabilities,
    AgentInterface,
    ExtendedAgentCard,
    build_extended_agent_card,
)


class TestAgentProvider:
    def test_provider_required_fields(self) -> None:
        p = AgentProvider(url="https://hermes.fleet", organization="Hermes Fleet")
        assert p.url == "https://hermes.fleet"
        assert p.organization == "Hermes Fleet"


class TestAgentSkill:
    def test_skill_required_fields(self) -> None:
        s = AgentSkill(
            id="code_review",
            name="Code Review",
            description="Reviews code",
            tags=["dev"],
        )
        assert s.id == "code_review"
        assert s.name == "Code Review"
        assert s.description == "Reviews code"
        assert s.tags == ["dev"]

    def test_skill_optional_fields(self) -> None:
        s = AgentSkill(
            id="data_analysis",
            name="Data Analysis",
            description="Analyze datasets",
            tags=["ml", "analytics"],
            examples=["example1"],
            input_modes=["text", "json"],
            output_modes=["text"],
        )
        assert s.examples == ["example1"]
        assert s.input_modes == ["text", "json"]
        assert s.output_modes == ["text"]


class TestAgentCapabilities:
    def test_defaults(self) -> None:
        caps = AgentCapabilities()
        assert caps.streaming is False
        assert caps.push_notifications is False
        assert caps.extensions is False
        assert caps.extended_agent_card is False

    def test_explicit_values(self) -> None:
        caps = AgentCapabilities(streaming=True, push_notifications=True, extensions=True, extended_agent_card=True)
        assert caps.streaming is True
        assert caps.push_notifications is True
        assert caps.extensions is True
        assert caps.extended_agent_card is True


class TestAgentInterface:
    def test_required_fields(self) -> None:
        iface = AgentInterface(
            url="https://agent.example.com/a2a",
            protocol_binding="https://a2aproject.github.io/A2A/spec",
            protocol_version="1.0.0",
        )
        assert iface.url == "https://agent.example.com/a2a"
        assert iface.protocol_binding == "https://a2aproject.github.io/A2A/spec"
        assert iface.protocol_version == "1.0.0"

    def test_optional_tenant(self) -> None:
        iface = AgentInterface(
            url="https://agent.example.com/a2a",
            protocol_binding="https://a2aproject.github.io/A2A/spec",
            protocol_version="1.0.0",
            tenant="acme",
        )
        assert iface.tenant == "acme"


class TestExtendedAgentCard:
    def test_required_fields(self) -> None:
        card = ExtendedAgentCard(
            name="Test Agent",
            description="A test agent",
            provider=AgentProvider(url="https://test.fleet", organization="Test Org"),
            capabilities=AgentCapabilities(),
            default_input_modes=["text"],
            default_output_modes=["text"],
        )
        assert card.name == "Test Agent"
        assert card.description == "A test agent"
        assert isinstance(card.provider, AgentProvider)
        assert isinstance(card.capabilities, AgentCapabilities)
        assert card.default_input_modes == ["text"]
        assert card.default_output_modes == ["text"]

    def test_skills_and_interfaces_optional(self) -> None:
        card = ExtendedAgentCard(
            name="Skilled Agent",
            description="Agent with skills",
            provider=AgentProvider(url="https://fleet.fleet", organization="Fleet"),
            capabilities=AgentCapabilities(streaming=True),
            default_input_modes=["text", "image"],
            default_output_modes=["text"],
            supported_interfaces=[
                AgentInterface(
                    url="https://agent.example.com/a2a",
                    protocol_binding="https://a2aproject.github.io/A2A/spec",
                    protocol_version="1.0.0",
                )
            ],
            skills=[
                AgentSkill(id="code", name="Code Assistant", description="Helps with code", tags=["dev"]),
                AgentSkill(id="docs", name="Docs Assistant", description="Helps with docs", tags=["writing"]),
            ],
        )
        assert len(card.skills) == 2
        assert card.skills[0].id == "code"
        assert card.skills[1].name == "Docs Assistant"
        assert len(card.supported_interfaces) == 1
        assert card.supported_interfaces[0].url == "https://agent.example.com/a2a"


class TestBuildExtendedAgentCard:
    def test_returns_dict(self) -> None:
        card = build_extended_agent_card()
        assert isinstance(card, dict)

    def test_has_required_keys(self) -> None:
        card = build_extended_agent_card()
        assert "name" in card
        assert "description" in card
        assert "provider" in card
        assert "capabilities" in card
        assert "default_input_modes" in card
        assert "default_output_modes" in card

    def test_provider_defaults(self) -> None:
        card = build_extended_agent_card()
        assert card["provider"]["organization"] == "Hermes Fleet"
        assert card["provider"]["url"] == "https://hermes.fleet"

    def test_capabilities_defaults(self) -> None:
        card = build_extended_agent_card()
        caps = card["capabilities"]
        assert caps["streaming"] is False
        assert caps["push_notifications"] is False
        assert caps["extensions"] is False
        assert caps["extended_agent_card"] is False

    def test_default_modes(self) -> None:
        card = build_extended_agent_card()
        assert "text" in card["default_input_modes"]
        assert "text" in card["default_output_modes"]

    def test_overrides_merged(self) -> None:
        card = build_extended_agent_card(
            overrides={
                "name": "Override Name",
                "provider": {"organization": "Custom Org", "url": "https://custom.fleet"},
            }
        )
        assert card["name"] == "Override Name"
        assert card["provider"]["organization"] == "Custom Org"
        assert card["provider"]["url"] == "https://custom.fleet"

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
                    {
                        "id": "test",
                        "name": "Test Skill",
                        "description": "A test skill",
                        "tags": ["testing"],
                    }
                ]
            }
        )
        assert len(card["skills"]) == 1
        assert card["skills"][0]["id"] == "test"
        assert card["skills"][0]["name"] == "Test Skill"
        assert card["skills"][0]["tags"] == ["testing"]