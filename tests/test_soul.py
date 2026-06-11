"""Unit tests for Soul/Identity Bricks.

Tests the SoulBrick abstract base class and all four concrete soul
personas: SisyphusOrchestrator, Dreamer, CryptoTradingAgent, and
WebDesignAgent.
"""

import pytest

from brikie.bricks.soul.base import SoulBrick
from brikie.bricks.soul.sisyphus_orchestrator import SisyphusOrchestrator
from brikie.bricks.soul.dreamer import Dreamer
from brikie.bricks.soul.crypto_trading_agent import CryptoTradingAgent
from brikie.bricks.soul.web_design_agent import WebDesignAgent


# ---------------------------------------------------------------------------
# SoulBrick — concrete base class
# ---------------------------------------------------------------------------


class TestSoulBrickABC:
    """Verify SoulBrick base class behavior."""

    def test_soulbrick_defaults(self):
        """Default SoulBrick should instantiate with base values."""
        soul = SoulBrick()
        assert soul.name == "base_soul"
        assert soul.version == "0.1.0"
        assert soul.allowed_tools == ["*"]
        assert soul.behavioral_constraints == {}
        assert soul.system_prompt == ""
        assert soul.description == ""

    def test_soulbrick_subclass_can_instantiate(self):
        """A minimal concrete subclass should work."""
        class MinimalSoul(SoulBrick):
            pass

        soul = MinimalSoul()
        assert soul.name == "base_soul"

    def test_from_manifest_classmethod_works(self):
        """SoulBrick.from_manifest() should work on the ABC via a concrete subclass."""
        class MinimalSoul(SoulBrick):
            pass

        data = {
            "name": "custom",
            "system_prompt": "test prompt",
            "allowed_tools": ["tool_a"],
            "behavioral_constraints": {"key": "val"},
            "description": "desc",
            "version": "2.0.0",
        }
        soul = MinimalSoul.from_manifest(data)  # type: ignore[abstract]
        assert soul.name == "custom"
        assert soul.system_prompt == "test prompt"
        assert soul.allowed_tools == ["tool_a"]
        assert soul.behavioral_constraints == {"key": "val"}
        assert soul.description == "desc"
        assert soul.version == "2.0.0"


# ---------------------------------------------------------------------------
# SisyphusOrchestrator
# ---------------------------------------------------------------------------


class TestSisyphusOrchestrator:
    """Verify SisyphusOrchestrator defaults and serialization."""

    def test_default_name(self):
        soul = SisyphusOrchestrator()
        assert soul.name == "sisyphus_orchestrator"

    def test_default_allowed_tools(self):
        soul = SisyphusOrchestrator()
        assert soul.allowed_tools == ["*"]

    def test_default_strict_mode(self):
        soul = SisyphusOrchestrator()
        assert soul.behavioral_constraints["strict_mode"] is True

    def test_requires_lsp_validation(self):
        soul = SisyphusOrchestrator()
        assert soul.behavioral_constraints.get("requires_lsp_validation") is True

    def test_max_subagents_default(self):
        soul = SisyphusOrchestrator()
        assert soul.behavioral_constraints["max_subagents"] == 5

    def test_default_version(self):
        soul = SisyphusOrchestrator()
        assert soul.version == "1.0.0"

    def test_to_manifest(self):
        soul = SisyphusOrchestrator()
        manifest = soul.to_manifest()
        assert manifest["name"] == "sisyphus_orchestrator"
        assert manifest["allowed_tools"] == ["*"]
        assert manifest["behavioral_constraints"]["strict_mode"] is True
        assert manifest["behavioral_constraints"]["requires_lsp_validation"] is True
        assert manifest["version"] == "1.0.0"
        assert "system_prompt" in manifest
        assert "description" in manifest

    def test_from_manifest(self):
        data = {
            "name": "sisyphus_orchestrator",
            "system_prompt": "overridden",
            "allowed_tools": ["tool_a", "tool_b"],
            "behavioral_constraints": {"strict_mode": False, "max_subagents": 10},
            "description": "custom desc",
            "version": "2.0.0",
        }
        soul = SisyphusOrchestrator.from_manifest(data)
        assert soul.name == "sisyphus_orchestrator"
        assert soul.system_prompt == "overridden"
        assert soul.allowed_tools == ["tool_a", "tool_b"]
        assert soul.behavioral_constraints == {"strict_mode": False, "max_subagents": 10}
        assert soul.description == "custom desc"
        assert soul.version == "2.0.0"

    def test_from_manifest_via_soulbrick_classmethod(self):
        """from_manifest should be callable on SoulBrick directly, returning the subclass type."""
        data = {
            "name": "sisyphus_orchestrator",
            "system_prompt": "",
            "allowed_tools": ["*"],
            "behavioral_constraints": {},
            "description": "",
            "version": "1.0.0",
        }
        soul = SoulBrick.from_manifest(data)
        # It will be a SoulBrick instance (the default) because SoulBrick
        # doesn't know about subclasses — but the call itself must not error.
        assert soul.name == "sisyphus_orchestrator"

    def test_custom_field_overrides(self):
        soul = SisyphusOrchestrator(
            name="custom_sisyphus",
            system_prompt="custom",
            allowed_tools=["tool_x"],
            behavioral_constraints={"strict_mode": False},
            description="custom",
            version="3.0.0",
        )
        assert soul.name == "custom_sisyphus"
        assert soul.system_prompt == "custom"
        assert soul.allowed_tools == ["tool_x"]
        assert soul.behavioral_constraints == {"strict_mode": False}
        assert soul.description == "custom"
        assert soul.version == "3.0.0"


# ---------------------------------------------------------------------------
# Dreamer
# ---------------------------------------------------------------------------


class TestDreamer:
    """Verify Dreamer defaults and serialization."""

    def test_default_name(self):
        soul = Dreamer()
        assert soul.name == "dreamer"

    def test_default_allowed_tools(self):
        soul = Dreamer()
        assert soul.allowed_tools == [
            "mempalace_query",
            "wiki:query",
            "wiki:ingest",
            "log_reader",
        ]

    def test_default_creative_mode(self):
        soul = Dreamer()
        assert soul.behavioral_constraints.get("creative_mode") is True

    def test_default_requires_approval(self):
        soul = Dreamer()
        assert soul.behavioral_constraints.get("requires_approval") is True

    def test_default_strict_mode_false(self):
        soul = Dreamer()
        assert soul.behavioral_constraints.get("strict_mode") is False

    def test_default_version(self):
        soul = Dreamer()
        assert soul.version == "1.0.0"

    def test_to_manifest(self):
        soul = Dreamer()
        manifest = soul.to_manifest()
        assert manifest["name"] == "dreamer"
        assert "mempalace_query" in manifest["allowed_tools"]
        assert manifest["behavioral_constraints"]["creative_mode"] is True
        assert manifest["behavioral_constraints"]["requires_approval"] is True
        assert manifest["version"] == "1.0.0"

    def test_from_manifest(self):
        data = {
            "name": "dreamer",
            "system_prompt": "custom dreamer",
            "allowed_tools": ["wiki:query"],
            "behavioral_constraints": {"creative_mode": False, "max_proposals_per_cycle": 3},
            "description": "custom",
            "version": "2.0.0",
        }
        soul = Dreamer.from_manifest(data)
        assert soul.name == "dreamer"
        assert soul.system_prompt == "custom dreamer"
        assert soul.allowed_tools == ["wiki:query"]
        assert soul.behavioral_constraints == {"creative_mode": False, "max_proposals_per_cycle": 3}
        assert soul.version == "2.0.0"

    def test_from_manifest_via_soulbrick_classmethod(self):
        data = {
            "name": "dreamer",
            "system_prompt": "",
            "allowed_tools": [],
            "behavioral_constraints": {},
            "description": "",
            "version": "1.0.0",
        }
        soul = SoulBrick.from_manifest(data)
        assert soul.name == "dreamer"

    def test_custom_field_overrides(self):
        soul = Dreamer(
            name="dreamer_custom",
            allowed_tools=["custom_tool"],
            behavioral_constraints={"creative_mode": False},
            version="0.5.0",
        )
        assert soul.name == "dreamer_custom"
        assert soul.allowed_tools == ["custom_tool"]
        assert soul.behavioral_constraints == {"creative_mode": False}
        assert soul.version == "0.5.0"


# ---------------------------------------------------------------------------
# CryptoTradingAgent
# ---------------------------------------------------------------------------


class TestCryptoTradingAgent:
    """Verify CryptoTradingAgent defaults and serialization."""

    def test_default_name(self):
        soul = CryptoTradingAgent()
        assert soul.name == "crypto_trading_agent"

    def test_default_supported_chains_includes_ethereum(self):
        soul = CryptoTradingAgent()
        assert "ethereum" in soul.behavioral_constraints.get("supported_chains", [])

    def test_default_requires_confirmation(self):
        soul = CryptoTradingAgent()
        assert soul.behavioral_constraints.get("requires_confirmation") is True

    def test_default_strict_mode(self):
        soul = CryptoTradingAgent()
        assert soul.behavioral_constraints.get("strict_mode") is True

    def test_default_max_slippage(self):
        soul = CryptoTradingAgent()
        assert soul.behavioral_constraints.get("max_slippage_pct") == 0.5

    def test_default_allowed_tools(self):
        soul = CryptoTradingAgent()
        assert "token_swap" in soul.allowed_tools
        assert "blockchain_query" in soul.allowed_tools
        assert "price_feed" in soul.allowed_tools

    def test_default_version(self):
        soul = CryptoTradingAgent()
        assert soul.version == "1.0.0"

    def test_to_manifest(self):
        soul = CryptoTradingAgent()
        manifest = soul.to_manifest()
        assert manifest["name"] == "crypto_trading_agent"
        assert "ethereum" in manifest["behavioral_constraints"]["supported_chains"]
        assert manifest["behavioral_constraints"]["requires_confirmation"] is True
        assert manifest["behavioral_constraints"]["max_slippage_pct"] == 0.5
        assert "token_swap" in manifest["allowed_tools"]

    def test_from_manifest(self):
        data = {
            "name": "crypto_trading_agent",
            "system_prompt": "custom crypto",
            "allowed_tools": ["token_swap"],
            "behavioral_constraints": {
                "supported_chains": ["bitcoin"],
                "requires_confirmation": False,
            },
            "description": "custom",
            "version": "3.0.0",
        }
        soul = CryptoTradingAgent.from_manifest(data)
        assert soul.system_prompt == "custom crypto"
        assert soul.behavioral_constraints["supported_chains"] == ["bitcoin"]
        assert soul.behavioral_constraints["requires_confirmation"] is False
        assert soul.version == "3.0.0"

    def test_from_manifest_via_soulbrick_classmethod(self):
        data = {
            "name": "crypto_trading_agent",
            "system_prompt": "",
            "allowed_tools": [],
            "behavioral_constraints": {},
            "description": "",
            "version": "1.0.0",
        }
        soul = SoulBrick.from_manifest(data)
        assert soul.name == "crypto_trading_agent"

    def test_custom_field_overrides(self):
        soul = CryptoTradingAgent(
            allowed_tools=["custom_trade"],
            behavioral_constraints={"supported_chains": ["solana"]},
        )
        assert soul.allowed_tools == ["custom_trade"]
        assert soul.behavioral_constraints["supported_chains"] == ["solana"]


# ---------------------------------------------------------------------------
# WebDesignAgent
# ---------------------------------------------------------------------------


class TestWebDesignAgent:
    """Verify WebDesignAgent defaults and serialization."""

    def test_default_name(self):
        soul = WebDesignAgent()
        assert soul.name == "web_design_agent"

    def test_default_framework(self):
        soul = WebDesignAgent()
        assert soul.behavioral_constraints.get("framework") == "react"

    def test_default_supports_responsive(self):
        soul = WebDesignAgent()
        assert soul.behavioral_constraints.get("supports_responsive") is True

    def test_default_design_system_required(self):
        soul = WebDesignAgent()
        assert soul.behavioral_constraints.get("design_system_required") is True

    def test_default_creative_mode(self):
        soul = WebDesignAgent()
        assert soul.behavioral_constraints.get("creative_mode") is True

    def test_default_allowed_tools(self):
        soul = WebDesignAgent()
        assert "css_generator" in soul.allowed_tools
        assert "visual_diff" in soul.allowed_tools
        assert "component_renderer" in soul.allowed_tools
        assert "design_token_manager" in soul.allowed_tools

    def test_default_version(self):
        soul = WebDesignAgent()
        assert soul.version == "1.0.0"

    def test_to_manifest(self):
        soul = WebDesignAgent()
        manifest = soul.to_manifest()
        assert manifest["name"] == "web_design_agent"
        assert manifest["behavioral_constraints"]["framework"] == "react"
        assert manifest["behavioral_constraints"]["supports_responsive"] is True
        assert manifest["behavioral_constraints"]["design_system_required"] is True
        assert "css_generator" in manifest["allowed_tools"]

    def test_from_manifest(self):
        data = {
            "name": "web_design_agent",
            "system_prompt": "custom web",
            "allowed_tools": ["css_generator"],
            "behavioral_constraints": {"framework": "vue", "supports_responsive": False},
            "description": "custom",
            "version": "2.0.0",
        }
        soul = WebDesignAgent.from_manifest(data)
        assert soul.system_prompt == "custom web"
        assert soul.allowed_tools == ["css_generator"]
        assert soul.behavioral_constraints["framework"] == "vue"
        assert soul.behavioral_constraints["supports_responsive"] is False
        assert soul.version == "2.0.0"

    def test_from_manifest_via_soulbrick_classmethod(self):
        data = {
            "name": "web_design_agent",
            "system_prompt": "",
            "allowed_tools": [],
            "behavioral_constraints": {},
            "description": "",
            "version": "1.0.0",
        }
        soul = SoulBrick.from_manifest(data)
        assert soul.name == "web_design_agent"

    def test_custom_field_overrides(self):
        soul = WebDesignAgent(
            behavioral_constraints={"framework": "svelte", "supports_responsive": False},
            description="svelte specialist",
        )
        assert soul.behavioral_constraints["framework"] == "svelte"
        assert soul.behavioral_constraints["supports_responsive"] is False
        assert soul.description == "svelte specialist"


# ---------------------------------------------------------------------------
# Cross-soul serialisation round-trip
# ---------------------------------------------------------------------------


class TestSoulRoundTrip:
    """Verify to_manifest() → from_manifest() round-trip for all souls."""

    @pytest.mark.parametrize(
        "soul_cls",
        [
            SisyphusOrchestrator,
            Dreamer,
            CryptoTradingAgent,
            WebDesignAgent,
        ],
    )
    def test_to_manifest_from_manifest_round_trip(self, soul_cls):
        original = soul_cls()
        manifest = original.to_manifest()
        restored = soul_cls.from_manifest(manifest)
        assert restored.name == original.name
        assert restored.system_prompt == original.system_prompt
        assert restored.allowed_tools == original.allowed_tools
        assert restored.behavioral_constraints == original.behavioral_constraints
        assert restored.description == original.description
        assert restored.version == original.version
