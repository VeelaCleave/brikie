"""Live verification for LoopDetectorBrick (BRK-910).

Boots the REAL BuildLoader + kernel warm-up (the path that was missing —
the brick tested green but never loaded), forces a genuine loop through
the real hook dispatcher, confirms the active realignment injects, then
asks the LOCAL model whether it acts on the injected nudge.
"""

import asyncio

from brikie.bricks.build.loader import BuildLoader
from brikie.bricks.improvement.loop_detector import LoopDetectorBrick
from brikie.config.types import HookEvent, HookType, ToolCall
from brikie.kernel.event_loop import EventLoop
from brikie.kernel.hooks import HookDispatcher
from brikie.kernel.registry import BrickRegistry, ProviderBrick
from brikie.kernel.state import StateManager


def ok(label: str, cond: bool) -> bool:
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


async def main() -> int:
    print("== 1. Load bricks through the REAL loader (BRK-910 + BRK-460) ==")
    registry = BrickRegistry()
    loader = BuildLoader(registry)
    specs = [
        ("BRK-200", {"config": {"model": "deepseek-v4-flash-spark",
                                "base_url": "http://localhost:8000/v1",
                                "api_key": "not-needed"}}),
        ("BRK-720", {}),   # diagnostics collector
        ("BRK-460", {}),   # goal (anchor target)
        ("BRK-910", {}),   # loop detector
    ]
    for brk, cfg in specs:
        b = loader._instantiate(brk, cfg)
        registry.register(b)
        print(f"  loaded {brk} -> {type(b).__name__} (name={b.name})")

    loop = EventLoop(registry=registry, state=StateManager(), hooks=HookDispatcher())
    await loop._phase_warm_up()

    results = []
    det = next((b for b in registry._bricks.values()
                if isinstance(b, LoopDetectorBrick)), None)
    results.append(ok("loop_detector loaded & active (not quarantined)", det is not None))
    if det is None:
        return 1

    print("== 2. Wiring confirmed in the assembled kernel ==")
    results.append(ok("diagnostics collector resolved from registry",
                      det._diagnostics is not None))
    results.append(ok("goal_status tool visible for realignment anchor",
                      det._goal_tool_available()))
    hook_count = len(loop._hooks._callbacks.get(HookType.POST_TOOL_CALL, []))
    results.append(ok("POST_TOOL_CALL hook registered", hook_count >= 1))

    print("== 3. Force a real loop through the live hook dispatcher ==")
    for _ in range(4):
        await loop._hooks.dispatch(HookType.POST_TOOL_CALL, HookEvent(
            hook_type=HookType.POST_TOOL_CALL,
            data=[ToolCall(name="read_file", args={"path": "/x"},
                           result="same content", tool_call_id="c1")],
            brick_name="event_loop",
        ))
    results.append(ok("loop detected after 4 identical calls",
                      det._current_loop is not None))

    print("== 4. Active realignment injects into the conversation ==")
    await loop._drain_realignments()
    nudges = [m for m in loop._message_history
              if m.role == "system" and "LOOP DETECTED" in (m.content or "")]
    results.append(ok("realignment nudge injected as a system turn", bool(nudges)))
    if nudges:
        print(f"      nudge: {nudges[0].content}")

    print("== 5. LOCAL model acts on the injected nudge ==")
    provider = registry.get_all(ProviderBrick)[0]
    nudge = nudges[0].content if nudges else "⚠️ LOOP DETECTED: stop repeating."
    test_msgs = [
        {"role": "user",
         "content": "You called read_file({'path':'/x'}) four times and got "
                    "the identical result each time."},
        {"role": "system", "content": nudge},
        {"role": "user",
         "content": "In one sentence, what will you do differently next?"},
    ]
    content, _calls, _meta = await provider.get_completion(test_msgs, [])
    print(f"      model reply: {content.strip()[:400]}")
    low = content.lower()
    # Acting on the nudge = it commits to changing course: stop/different/
    # re-assess, or anchors to the goal as the nudge instructed.
    acted = bool(content.strip()) and any(
        kw in low for kw in ("goal_status", "stop", "different",
                             "re-assess", "reassess", "instead", "change"))
    results.append(ok("model acts on the nudge (changes course / re-anchors)",
                      acted))

    await loop._phase_shutdown()
    print(f"\n== RESULT: {sum(results)}/{len(results)} checks passed ==")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
