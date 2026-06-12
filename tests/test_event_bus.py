from __future__ import annotations

import asyncio


from brikie.bricks.interface.event_bus import InternalEventBusBrick
from brikie.bricks.interface.base import InterfaceBrick
from brikie.bricks.soul.base import SoulBrick
from brikie.bricks.soul.dreamer import Dreamer
from brikie.bricks.soul.foreman import Foreman
from brikie.config.types import AFKMode, BrickState, BusEvent
from brikie.kernel.afk_manager import AFKManager
from brikie.kernel.afk_protocol import AFKProtocolEngine, AFKCycleResult, Proposal
from brikie.kernel.registry import BrickRegistry


# ── BusEvent ───────────────────────────────────────────────────────────


class TestBusEvent:
    def test_default_fields(self):
        event = BusEvent()
        assert event.event_type == ""
        assert event.source_soul == ""
        assert event.target_soul == "*"
        assert event.payload == {}
        assert event.correlation_id is not None
        assert event.timestamp > 0

    def test_custom_fields(self):
        event = BusEvent(
            event_type="dreamer.proposal",
            source_soul="dreamer",
            target_soul="foreman",
            payload={"proposal": "refactor X"},
        )
        assert event.event_type == "dreamer.proposal"
        assert event.payload["proposal"] == "refactor X"


# ── InternalEventBusBrick ──────────────────────────────────────────────


class TestInternalEventBusBrick:
    async def test_implements_interface_brick(self):
        bus = InternalEventBusBrick()
        assert isinstance(bus, InterfaceBrick)

    async def test_lifecycle(self):
        bus = InternalEventBusBrick()
        assert bus.state == BrickState.WARM_UP
        await bus.init()
        assert bus.state == BrickState.ACTIVE
        await bus.shutdown()
        assert bus.state == BrickState.WARM_UP

    async def test_register_and_consume(self):
        bus = InternalEventBusBrick()
        await bus.init()
        bus.register_soul("dreamer")

        event = BusEvent(
            event_type="test.event",
            source_soul="foreman",
            target_soul="dreamer",
            payload={"msg": "hello"},
        )
        await bus.publish(event)

        received = await bus.consume("dreamer")
        assert received.event_type == "test.event"
        assert received.payload["msg"] == "hello"
        await bus.shutdown()

    async def test_broadcast_to_all(self):
        bus = InternalEventBusBrick()
        await bus.init()
        bus.register_soul("dreamer")
        bus.register_soul("foreman")

        event = BusEvent(
            event_type="broadcast",
            source_soul="system",
            target_soul="*",
            payload={"msg": "hello all"},
        )
        await bus.publish(event)

        dreamer_event = await bus.consume("dreamer")
        foreman_event = await bus.consume("foreman")
        assert dreamer_event.payload["msg"] == "hello all"
        assert foreman_event.payload["msg"] == "hello all"
        await bus.shutdown()

    async def test_consume_unknown_soul_returns_error(self):
        bus = InternalEventBusBrick()
        await bus.init()
        event = await bus.consume("no_such_soul")
        assert event.event_type == "error"
        assert "no queue" in event.payload["error"].lower()
        await bus.shutdown()

    async def test_publish_to_unregistered_soul_is_noop(self):
        bus = InternalEventBusBrick()
        await bus.init()
        bus.register_soul("dreamer")
        event = BusEvent(target_soul="no_such_soul")
        await bus.publish(event)
        # No crash = pass
        await bus.shutdown()

    async def test_publish_after_shutdown_is_noop(self):
        bus = InternalEventBusBrick()
        await bus.init()
        await bus.shutdown()
        await bus.publish(BusEvent())
        # No crash = pass

    async def test_get_input_returns_event_string(self):
        bus = InternalEventBusBrick()
        await bus.init()
        bus.register_soul("dreamer")

        event = BusEvent(
            event_type="test.type",
            source_soul="dreamer",
        )
        await bus.publish(event)
        await asyncio.sleep(0.05)

        result = await bus.get_input()
        assert result.startswith("__bus_event__:")
        assert "test.type" in result
        await bus.shutdown()

    async def test_output_publishes_to_all(self):
        bus = InternalEventBusBrick()
        await bus.init()
        bus.register_soul("dreamer")
        bus.register_soul("foreman")

        await bus.output("test message")

        dreamer_event = await bus.consume("dreamer")
        assert dreamer_event.event_type == "orchestrator.output"
        assert dreamer_event.payload["content"] == "test message"
        await bus.shutdown()


# ── AFKManager ─────────────────────────────────────────────────────────


class FakeCLI(InterfaceBrick):
    """Minimal CLI-like brick for AFKManager testing."""

    def __init__(self):
        super().__init__()
        self._name = "cli"

    async def init(self):
        self._state = BrickState.ACTIVE

    async def get_input(self):
        return ""

    async def output(self, msg):
        pass


class TestAFKManager:
    async def test_starts_in_interactive_mode(self):
        registry = BrickRegistry()
        manager = AFKManager(registry)
        assert manager.mode == AFKMode.INTERACTIVE

    async def test_enter_afk_registers_event_bus(self):
        registry = BrickRegistry()
        cli = FakeCLI()
        registry.register(cli)

        manager = AFKManager(registry, cli_brick=cli)
        await manager.enter_afk_mode()

        assert manager.mode == AFKMode.AFK
        interfaces = registry.get_all(InterfaceBrick)
        names = [i.name for i in interfaces]
        assert "event_bus" in names
        assert "cli" not in names
        await manager.exit_afk_mode()

    async def test_exit_afk_restores_cli(self):
        registry = BrickRegistry()
        cli = FakeCLI()
        registry.register(cli)

        manager = AFKManager(registry, cli_brick=cli)
        await manager.enter_afk_mode()
        await manager.exit_afk_mode()

        assert manager.mode == AFKMode.INTERACTIVE
        interfaces = registry.get_all(InterfaceBrick)
        names = [i.name for i in interfaces]
        assert "cli" in names
        assert "event_bus" not in names

    async def test_enter_afk_registers_soul_queues(self):
        registry = BrickRegistry()
        manager = AFKManager(registry)

        souls = [
            type("Dreamer", (SoulBrick,), {})(),
            type("Foreman", (SoulBrick,), {})(),
        ]
        souls[0].name = "dreamer"
        souls[1].name = "foreman"

        await manager.enter_afk_mode(souls=souls)
        assert "dreamer" in manager.event_bus._queues
        assert "foreman" in manager.event_bus._queues
        await manager.exit_afk_mode()


# ── AFKProtocolEngine ──────────────────────────────────────────────────


class TestAFKProtocolEngine:
    def test_proposal_defaults(self):
        p = Proposal()
        assert p.status == "pending"
        assert p.title == ""

    def test_cycle_result_defaults(self):
        r = AFKCycleResult()
        assert r.cycle_number == 0
        assert r.proposals_count == 0
        assert r.executed_count == 0

    def test_stop_before_start(self):
        bus = InternalEventBusBrick()
        engine = AFKProtocolEngine(
            event_bus=bus,
            dreamer_soul=Dreamer(),
            foreman_soul=Foreman(),
        )
        assert engine.running is False
        engine.stop()
        assert engine.running is False

    def test_results_and_cycle_empty_before_running(self):
        bus = InternalEventBusBrick()
        engine = AFKProtocolEngine(
            event_bus=bus,
            dreamer_soul=Dreamer(),
            foreman_soul=Foreman(),
        )
        assert engine.cycle_count == 0
        assert engine.results == []
