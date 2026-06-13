"""Tests for the always-on operating-discipline block.

The local model was told to "follow AGENTS.md" but never shown the rules.
These guard that the distilled contract is actually injected into the
model's context — and stays toggleable.
"""

from __future__ import annotations

import pytest

from brikie.config.operating_discipline import OPERATING_DISCIPLINE, discipline_block
from brikie.kernel.event_loop import EventLoop
from brikie.kernel.hooks import HookDispatcher
from brikie.kernel.registry import BrickRegistry
from brikie.kernel.state import StateManager


class TestDisciplineBlock:
    def test_on_by_default(self, monkeypatch):
        monkeypatch.delenv("BRIKIE_DISCIPLINE", raising=False)
        block = discipline_block()
        assert block == OPERATING_DISCIPLINE
        # The hardest-won rules are present.
        assert "is NOT done" in block
        assert "BRICK_INDEX" in block
        assert "Do not loop" in block

    @pytest.mark.parametrize("val", ["0", "false", "no"])
    def test_toggle_off(self, monkeypatch, val):
        monkeypatch.setenv("BRIKIE_DISCIPLINE", val)
        assert discipline_block() == ""


class TestInjection:
    async def test_discipline_injected_after_system_prompt(self, monkeypatch):
        monkeypatch.delenv("BRIKIE_DISCIPLINE", raising=False)
        loop = EventLoop(
            registry=BrickRegistry(), state=StateManager(),
            hooks=HookDispatcher(), system_prompt="BASE PROMPT",
        )
        messages = await loop._build_provider_messages()
        systems = [m["content"] for m in messages if m["role"] == "system"]
        assert systems[0] == "BASE PROMPT"
        assert any("Operating discipline" in s for s in systems)

    async def test_toggle_off_omits_block(self, monkeypatch):
        monkeypatch.setenv("BRIKIE_DISCIPLINE", "0")
        loop = EventLoop(
            registry=BrickRegistry(), state=StateManager(),
            hooks=HookDispatcher(), system_prompt="BASE PROMPT",
        )
        messages = await loop._build_provider_messages()
        assert not any("Operating discipline" in m["content"]
                       for m in messages if m["role"] == "system")
