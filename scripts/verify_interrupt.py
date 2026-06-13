"""Live verification for mid-turn interrupt / steer (#28).

Boots a REAL kernel (deepseek provider + shell tools) with interruptible=True
and exercises the genuine concurrency — a watcher reading interface input
WHILE the agent loop makes real model calls — for both paths:

  1. STOP: a multi-step task is interrupted with '/stop' mid-run; the loop
     halts cleanly (and does not finish all the steps).
  2. STEER: a steer message injected mid-run redirects the model — it ends up
     emitting a codeword the original task never mentioned.

Run: python3 scripts/verify_interrupt.py
"""

import asyncio
import time

from brikie.bricks.build.loader import BuildLoader
from brikie.config.types import BrickState, Message
from brikie.kernel.event_loop import EventLoop
from brikie.kernel.hooks import HookDispatcher
from brikie.kernel.registry import BrickRegistry, InterfaceBrick
from brikie.kernel.state import StateManager


class ControlIface(InterfaceBrick):
    """Delivers scripted input (after a delay) to simulate a user typing
    mid-turn, and records what the agent emits."""

    def __init__(self, script):
        self._script = list(script)          # [(delay_seconds, text)]
        self.assistant = []
        self.tools = []
        self.t0 = time.monotonic()

    @property
    def name(self):
        return "control"

    @property
    def state(self):
        return BrickState.ACTIVE

    async def init(self): ...
    async def shutdown(self): ...

    async def get_input(self):
        if self._script:
            delay, text = self._script.pop(0)
            await asyncio.sleep(delay)
            print(f"   [{time.monotonic()-self.t0:5.1f}s] » user types: {text!r}")
            return text
        await asyncio.sleep(3600)
        return ""

    async def output(self, msg):
        self.assistant.append(msg)

    async def render_assistant_response(self, content):
        self.assistant.append(content)
        print(f"   [{time.monotonic()-self.t0:5.1f}s] assistant: {content[:80]!r}")

    async def render_tool_calls(self, raw):
        for c in raw:
            n = c.get("function", {}).get("name", "?")
            self.tools.append(n)
            print(f"   [{time.monotonic()-self.t0:5.1f}s]   → {n}")

    async def render_info(self, title, body):
        print(f"   [{time.monotonic()-self.t0:5.1f}s]   [{title}] {body}")

    async def render_error(self, msg):
        print(f"   [{time.monotonic()-self.t0:5.1f}s]   ! {msg}")


def ok(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


async def _boot(script):
    registry = BrickRegistry()
    loader = BuildLoader(registry, hooks=HookDispatcher())
    registry.register(loader._instantiate("BRK-200", {"config": {
        "model": "deepseek-v4-flash-spark",
        "base_url": "http://localhost:8000/v1", "api_key": "not-needed"}}))
    registry.register(loader._instantiate("BRK-410", {}))   # shell tools
    iface = ControlIface(script)
    registry.register(iface)
    loop = EventLoop(registry=registry, state=StateManager(),
                     hooks=HookDispatcher(), interruptible=True)
    for b in list(registry._bricks.values()):
        await b.init()
    return loop, iface, registry


async def main() -> int:
    results = []

    print("== 1. STOP — interrupt a multi-step run with /stop ==")
    # /stop arrives ~2.5s in, after a round or two of real model calls.
    loop, iface, reg = await _boot([(2.5, "/stop")])
    loop._message_history.append(Message(role="user", content=(
        "Use the bash_execute tool to run these five commands ONE AT A TIME, "
        "reporting briefly after each — you MUST use the tool for every step, "
        "do not answer directly: "
        "`echo one`, then `echo two`, then `echo three`, then `echo four`, "
        "then `echo five`.")))
    await loop._run_agent_interruptible()
    stopped = any("Stopped at your request" in m for m in iface.assistant)
    results.append(ok("loop halted with the stop message", stopped))
    # Interrupted before completing all five echo steps (each step = 1 tool call).
    results.append(ok(f"interrupted mid-run (ran {len(iface.tools)} of 5 steps)",
                      0 < len(iface.tools) < 5))
    for b in reg._bricks.values():
        await b.shutdown()

    print("\n== 2. STEER — redirect the model mid-run to a new codeword ==")
    loop2, iface2, reg2 = await _boot([
        (2.5, "Ignore the previous task. Reply with exactly the single word "
              "KIWI and do not call any more tools.")])
    loop2._message_history.append(Message(role="user", content=(
        "Use the bash_execute tool to slowly run these commands one at a time, "
        "reporting after each (you MUST use the tool each time): "
        "`echo a`, `echo b`, `echo c`, `echo d`, `echo e`, `echo f`.")))
    await loop2._run_agent_interruptible()
    steered_in = any("interjected" in m.content for m in loop2._message_history
                     if m.role == "user")
    said_kiwi = any("KIWI" in str(m).upper() for m in iface2.assistant)
    results.append(ok("steer message was injected mid-run", steered_in))
    results.append(ok("model acted on the steer (final answer says KIWI)", said_kiwi))
    for b in reg2._bricks.values():
        await b.shutdown()

    passed = sum(1 for r in results if r)
    print(f"\n== {passed}/{len(results)} checks passed ==")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
