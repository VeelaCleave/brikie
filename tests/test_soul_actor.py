"""Tests for the LLM-driven soul actors and the Phase C negotiation loop."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List


from brikie.bricks.interface.event_bus import InternalEventBusBrick
from brikie.bricks.soul.dreamer import Dreamer
from brikie.bricks.soul.foreman import Foreman
from brikie.config.types import BusEvent
from brikie.kernel.afk_protocol import AFKProtocolEngine
from brikie.kernel.soul_actor import (
    DreamerActor,
    ForemanActor,
    SoulActor,
    extract_json,
)


class FakeProvider:
    """Provider stub that returns queued canned responses."""

    def __init__(self, responses: List[str]) -> None:
        self._responses = list(responses)
        self.calls: List[List[Dict[str, Any]]] = []

    @property
    def name(self) -> str:
        return "fake_provider"

    async def get_completion(self, messages, tools):
        self.calls.append(messages)
        content = self._responses.pop(0) if self._responses else ""
        return content, [], {}


# ---------------------------------------------------------------------------
# extract_json
# ---------------------------------------------------------------------------


class TestExtractJson:
    def test_plain_object(self):
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_plain_array(self):
        assert extract_json('[1, 2]') == [1, 2]

    def test_fenced_block(self):
        text = '```json\n{"decision": "approve", "feedback": "ok"}\n```'
        assert extract_json(text) == {"decision": "approve", "feedback": "ok"}

    def test_json_embedded_in_prose(self):
        text = 'Sure! Here is my evaluation:\n{"decision": "reject", "feedback": "vague"}\nHope that helps.'
        assert extract_json(text)["decision"] == "reject"

    def test_array_embedded_in_prose(self):
        text = "My proposals:\n[{\"title\": \"X\"}]\nDone."
        assert extract_json(text) == [{"title": "X"}]

    def test_garbage_returns_none(self):
        assert extract_json("no json here at all") is None

    def test_empty_returns_none(self):
        assert extract_json("") is None


# ---------------------------------------------------------------------------
# SoulActor base
# ---------------------------------------------------------------------------


class TestSoulActor:
    async def test_complete_uses_soul_system_prompt(self):
        provider = FakeProvider(["hello"])
        actor = SoulActor(Dreamer(), provider)
        out = await actor.complete("hi")
        assert out == "hello"
        sent = provider.calls[0]
        assert sent[0]["role"] == "system"
        assert "Dreamer" in sent[0]["content"]

    async def test_complete_falls_back_to_reasoning(self):
        class ReasoningProvider(FakeProvider):
            async def get_completion(self, messages, tools):
                return "", [], {"reasoning": "thought-stream"}

        actor = SoulActor(Dreamer(), ReasoningProvider([]))
        assert await actor.complete("hi") == "thought-stream"


# ---------------------------------------------------------------------------
# DreamerActor
# ---------------------------------------------------------------------------


class TestDreamerActor:
    async def test_propose_parses_proposals(self):
        payload = json.dumps([
            {"title": "Add /status command", "description": "Show uptime",
             "impact": "medium", "complexity": "low"},
            {"title": "Cache tool schemas", "description": "Avoid rebuilds",
             "impact": "low", "complexity": "low"},
        ])
        actor = DreamerActor(Dreamer(), FakeProvider([payload]))
        proposals = await actor.propose("ctx", 5)
        assert len(proposals) == 2
        assert proposals[0].title == "Add /status command"
        assert proposals[0].impact == "medium"

    async def test_propose_caps_at_max(self):
        payload = json.dumps([{"title": f"P{i}"} for i in range(8)])
        actor = DreamerActor(Dreamer(), FakeProvider([payload]))
        proposals = await actor.propose("ctx", 3)
        assert len(proposals) == 3

    async def test_propose_sanitizes_invalid_levels(self):
        payload = json.dumps([{"title": "X", "impact": "MASSIVE", "complexity": "tiny"}])
        actor = DreamerActor(Dreamer(), FakeProvider([payload]))
        proposals = await actor.propose("ctx", 5)
        assert proposals[0].impact == "low"
        assert proposals[0].complexity == "low"

    async def test_propose_unparseable_returns_empty(self):
        actor = DreamerActor(Dreamer(), FakeProvider(["I have no ideas today."]))
        assert await actor.propose("ctx", 5) == []

    async def test_propose_skips_titleless_items(self):
        payload = json.dumps([{"description": "no title"}, {"title": "ok"}])
        actor = DreamerActor(Dreamer(), FakeProvider([payload]))
        proposals = await actor.propose("ctx", 5)
        assert len(proposals) == 1
        assert proposals[0].title == "ok"


# ---------------------------------------------------------------------------
# ForemanActor
# ---------------------------------------------------------------------------


class TestForemanActor:
    async def test_evaluate_approve(self):
        provider = FakeProvider(['{"decision": "approve", "feedback": "solid"}'])
        actor = ForemanActor(Foreman(), provider, InternalEventBusBrick())
        decision, feedback = await actor.evaluate({"title": "X"})
        assert decision == "approve"
        assert feedback == "solid"

    async def test_evaluate_invalid_decision_becomes_reject(self):
        provider = FakeProvider(['{"decision": "maybe", "feedback": "?"}'])
        actor = ForemanActor(Foreman(), provider, InternalEventBusBrick())
        decision, _ = await actor.evaluate({"title": "X"})
        assert decision == "reject"

    async def test_evaluate_unparseable_becomes_reject(self):
        provider = FakeProvider(["LGTM!"])
        actor = ForemanActor(Foreman(), provider, InternalEventBusBrick())
        decision, feedback = await actor.evaluate({"title": "X"})
        assert decision == "reject"
        assert "unparseable" in feedback

    async def test_serve_answers_evaluate_events(self):
        bus = InternalEventBusBrick()
        await bus.init()
        bus.register_soul("foreman")
        bus.register_soul("dreamer")

        provider = FakeProvider(['{"decision": "approve", "feedback": "go"}'])
        actor = ForemanActor(Foreman(), provider, bus)
        task = asyncio.create_task(actor.serve())

        await bus.publish(BusEvent(
            event_type="foreman.evaluate_proposal",
            source_soul="dreamer",
            target_soul="foreman",
            payload={"proposal": {"title": "X", "proposal_id": "p1"}, "attempt": 1},
        ))
        response = await asyncio.wait_for(bus.consume("dreamer"), timeout=5)
        task.cancel()

        assert response.event_type == "foreman.decision"
        assert response.payload["decision"] == "approve"
        assert response.payload["proposal_id"] == "p1"


# ---------------------------------------------------------------------------
# Full negotiation cycle through the engine
# ---------------------------------------------------------------------------


class TestNegotiationCycle:
    async def test_llm_cycle_dream_evaluate_execute(self):
        bus = InternalEventBusBrick()
        await bus.init()
        bus.register_soul("foreman")
        bus.register_soul("dreamer")

        dreamer_payload = json.dumps([
            {"title": "Improve glob speed", "description": "Cache results",
             "impact": "low", "complexity": "low"},
        ])
        dreamer = DreamerActor(Dreamer(), FakeProvider([dreamer_payload]))
        foreman = ForemanActor(
            Foreman(),
            FakeProvider(['{"decision": "approve", "feedback": "in scope"}']),
            bus,
        )
        foreman_task = asyncio.create_task(foreman.serve())

        executed: List[str] = []

        async def on_execute(title: str, payload: Dict[str, Any]) -> bool:
            executed.append(title)
            return True

        stages: List[str] = []

        async def on_stage(actor: str, text: str) -> None:
            stages.append(f"{actor}: {text}")

        engine = AFKProtocolEngine(
            event_bus=bus,
            dreamer_soul=Dreamer(),
            foreman_soul=Foreman(),
            on_execute=on_execute,
            dreamer_propose=dreamer.propose,
            on_stage=on_stage,
            evaluation_timeout=5,
        )
        await engine.start(cycles=1)
        foreman_task.cancel()

        assert executed == ["Improve glob speed"]
        result = engine.results[0]
        assert result.proposals_count == 1
        assert result.approved_count == 1
        assert result.executed_count == 1
        assert result.failed_count == 0
        assert any("APPROVED" in s for s in stages)

    async def test_llm_cycle_rejection(self):
        bus = InternalEventBusBrick()
        await bus.init()
        bus.register_soul("foreman")
        bus.register_soul("dreamer")

        dreamer = DreamerActor(
            Dreamer(),
            FakeProvider([json.dumps([{"title": "Rewrite everything in Rust"}])]),
        )
        foreman = ForemanActor(
            Foreman(),
            FakeProvider(['{"decision": "reject", "feedback": "out of scope"}']),
            bus,
        )
        foreman_task = asyncio.create_task(foreman.serve())

        engine = AFKProtocolEngine(
            event_bus=bus,
            dreamer_soul=Dreamer(),
            foreman_soul=Foreman(),
            on_execute=None,
            dreamer_propose=dreamer.propose,
            evaluation_timeout=5,
        )
        await engine.start(cycles=1)
        foreman_task.cancel()

        result = engine.results[0]
        assert result.approved_count == 0
        assert result.failed_count == 1

    async def test_timeout_without_foreman_actor_rejects(self):
        """No actor on the bus → honest reject, no deadlock."""
        bus = InternalEventBusBrick()
        await bus.init()
        bus.register_soul("foreman")
        bus.register_soul("dreamer")

        dreamer = DreamerActor(
            Dreamer(), FakeProvider([json.dumps([{"title": "Orphan idea"}])])
        )
        engine = AFKProtocolEngine(
            event_bus=bus,
            dreamer_soul=Dreamer(),
            foreman_soul=Foreman(),
            dreamer_propose=dreamer.propose,
            evaluation_timeout=0.2,
        )
        await asyncio.wait_for(engine.start(cycles=1), timeout=10)
        assert engine.results[0].approved_count == 0
