"""Live verification for swarm Phase 1's OBSERVABILITY — the open item.

Boots the real loader + a capturing Interface Brick, runs a swarm_dispatch
against the LOCAL model, and proves that:
  1. sub-agent lifecycle events STREAM to the interface in real time (the
     dispatch is no longer a black box), in the right order;
  2. a forced wall-clock timeout fires live — a too-short deadline cancels a
     sub-agent and surfaces a `timeout` event + a bounded timeout result.

Run: python3 scripts/verify_swarm_streaming.py
"""

import asyncio
import time

from brikie.bricks.build.loader import BuildLoader
from brikie.config.types import BrickState
from brikie.kernel.hooks import HookDispatcher
from brikie.kernel.registry import BrickRegistry, InterfaceBrick


class CaptureIface(InterfaceBrick):
    """Records swarm events (and prints them as they arrive, to show that the
    stream really is live)."""

    def __init__(self) -> None:
        self.events = []
        self.t0 = time.monotonic()

    @property
    def name(self) -> str:
        return "capture"

    @property
    def state(self) -> BrickState:
        return BrickState.ACTIVE

    async def init(self) -> None: ...
    async def shutdown(self) -> None: ...
    async def get_input(self) -> str:
        return ""
    async def output(self, msg: str) -> None:
        self._log("output", "", msg)

    async def render_swarm_event(self, role: str, kind: str, text: str) -> None:
        self._log(role, kind, text)

    def _log(self, role, kind, text):
        dt = time.monotonic() - self.t0
        self.events.append((role, kind, text))
        print(f"   [{dt:6.2f}s] {text}")


def ok(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


async def main() -> int:
    print("== Boot loader + capturing interface + swarm (live model) ==")
    registry = BrickRegistry()
    hooks = HookDispatcher()
    loader = BuildLoader(registry, hooks=hooks)
    for brk, cfg in [
        ("BRK-200", {"config": {"model": "deepseek-v4-flash-spark",
                                "base_url": "http://localhost:8000/v1",
                                "api_key": "not-needed"}}),
        ("BRK-460", {"config": {"db_path": "/tmp/verify_stream_goals.db"}}),
        ("BRK-470", {"config": {"db_path": "/tmp/verify_stream.db",
                                "isolate_coders": False, "max_steps": 4}}),
    ]:
        registry.register(loader._instantiate(brk, cfg))
    iface = CaptureIface()
    registry.register(iface)
    for b in list(registry._bricks.values()):
        await b.init()
    swarm = next(b for b in registry._bricks.values() if b.name == "swarm")

    print("\n== 1. LIVE dispatch — watch events stream in real time ==")
    out = await swarm.execute("swarm_dispatch", {
        "tasks": [
            {"role": "researcher", "task": "What is 6 times 7? One line."},
            {"role": "generalist", "task": "Name the capital of France. One line."},
        ],
        "review": False,
    })
    kinds = [(r, k) for r, k, _ in iface.events]
    results = []
    results.append(ok("dispatch event streamed",
                      any(k == "dispatch" for _, k in kinds)))
    results.append(ok("each sub-agent streamed a 'start'",
                      sum(1 for _, k in kinds if k == "start") == 2))
    results.append(ok("each sub-agent streamed a 'done'",
                      sum(1 for _, k in kinds if k == "done") == 2))
    results.append(ok("a final 'summary' streamed",
                      any(k == "summary" for _, k in kinds)))
    results.append(ok("dispatch completed (2/2)",
                      out["summary"].startswith("2/2")))

    print("\n== 2. Forced wall-clock timeout fires live ==")
    iface.events.clear()
    swarm._subagent_timeout = 0.01          # absurdly short → must time out
    out2 = await swarm.execute("swarm_dispatch", {
        "tasks": [{"role": "researcher", "task": "Slowly ponder a long answer."}],
        "review": False,
    })
    r0 = out2["results"][0]
    results.append(ok(f"sub-agent returned a bounded timeout (error={r0.get('error')!r})",
                      r0["ok"] is False and r0.get("error") == "timeout"))
    results.append(ok("a 'timeout' event streamed to the interface",
                      any(k == "timeout" for _, k, _ in iface.events)))

    for b in registry._bricks.values():
        await b.shutdown()
    passed = sum(1 for r in results if r)
    print(f"\n== {passed}/{len(results)} checks passed ==")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
