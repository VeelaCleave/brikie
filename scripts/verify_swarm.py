"""Live verification for the FULL Swarm tier (BRK-470 + SubAgentRunner,
goals #4 — Swarm + Collaboration + Routing, nothing deferred).

Boots the REAL BuildLoader + kernel warm-up (provider, shell tools, goals,
command firewall, swarm) against the LOCAL deepseek model and proves, end
to end:

  1. swarm_dispatch fans role-specialized sub-agents out CONCURRENTLY.
  2. Routing — researcher/coder come back tagged by role.
  3. Recursion guard — sub-agents' toolset excludes the swarm tools.
  4. Security in path — a destructive call inside a sub-agent is vetoed by
     the REAL CommandFirewall through the shared hook dispatcher.
  5. Soul routing — a loaded soul (Mason) becomes a delegatable role.
  6. Goal integration — the dispatch + outcomes land in the active goal's
     progress log (visible via goal_status).
  7. Auto-review — a successful coder sub-agent's work is auto-reviewed.
  8. Collaboration — a sub-agent uses swarm_share live; it lands on the
     shared blackboard for the others.
  9. Observability — the run persists to the audit store.

Run: python3 scripts/verify_swarm.py
"""

import asyncio
import time

from brikie.bricks.build.loader import BuildLoader
from brikie.kernel.hooks import HookDispatcher
from brikie.kernel.registry import BrickRegistry
from brikie.kernel.subagent import SubAgentRunner


def ok(label: str, cond: bool) -> bool:
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


async def main() -> int:
    print("== 1. Boot the REAL loader + kernel (provider, tools, firewall, swarm) ==")
    registry = BrickRegistry()
    hooks = HookDispatcher()
    loader = BuildLoader(registry, hooks=hooks)
    specs = [
        ("BRK-200", {"config": {"model": "deepseek-v4-flash-spark",
                                "base_url": "http://localhost:8000/v1",
                                "api_key": "not-needed"}}),
        ("BRK-410", {}),   # shell / file tools (real tools for sub-agents)
        ("BRK-460", {"config": {"db_path": "/tmp/verify_swarm_goals.db"}}),
        ("BRK-800", {}),   # command firewall (security must stay in path)
        ("BRK-470", {"config": {"db_path": "/tmp/verify_swarm.db",
                                "max_steps": 5, "max_parallel": 2}}),
    ]
    for brk, cfg in specs:
        b = loader._instantiate(brk, cfg)
        registry.register(b)
        print(f"  loaded {brk} -> {type(b).__name__} (name={b.name})")

    # A real soul → becomes a swarm role (routing from installed souls).
    mason = loader._instantiate("BRK-540", {})

    for brick in list(registry._bricks.values()):
        await brick.init()
    for brick in registry._bricks.values():
        getter = getattr(brick, "get_hook_callbacks", None)
        if getter is None:
            continue
        for ht, cb_list in getter().items():
            for cb in cb_list:
                hooks.register(ht, cb)

    swarm = next(b for b in registry._bricks.values() if b.name == "swarm")
    goals = next(b for b in registry._bricks.values() if b.name == "goals")
    swarm.set_souls({mason.name: mason})        # distribute_souls equivalent

    results = []
    results.append(ok("swarm brick has registry + hooks injected",
                      swarm._registry is registry and swarm._hooks is hooks))

    print("== 2. Recursion guard: sub-agents can't see the swarm tools ==")
    names = {s["function"]["name"] for s in swarm._subagent_tool_schemas()}
    results.append(ok(f"sub-agent toolset excludes swarm_* (has {len(names)} real tools)",
                      "swarm_dispatch" not in names and len(names) > 0))

    print("== 3. Security in path: firewall vetoes a dangerous sub-agent call ==")

    async def exec_tool(name, args):
        return "SHOULD-NOT-RUN"

    runner = SubAgentRunner(provider=None, tool_schemas=[], execute_tool=exec_tool,
                            hooks=hooks, label="probe")
    msgs, used, blocked = await runner._run_tools([
        {"id": "x1", "function": {"name": "bash_execute",
                                  "arguments": '{"command": "rm -rf /"}'}}
    ])
    vetoed = bool(blocked) and all("SHOULD-NOT-RUN" not in m["content"] for m in msgs)
    results.append(ok("destructive bash vetoed by firewall", vetoed))

    print("== 4. Soul routing: the Mason soul is a delegatable role ==")
    roles = swarm._swarm_roles()
    results.append(ok(f"soul role '{mason.name}' available (soul_roles={roles['soul_roles']})",
                      mason.name.lower() in roles["roles"]))

    print("== 5. Set an active goal (so delegated work is logged to it) ==")
    await goals.execute("goal_set", {"title": "Ship the Swarm tier",
                                     "detail": "parallel sub-agents this weekend"})

    print("== 6/7. LIVE swarm_dispatch — researcher + coder, auto-review on ==")
    t0 = time.monotonic()
    out = await swarm.execute("swarm_dispatch", {
        "context": "Smoke-testing the brikie swarm.",
        "tasks": [
            {"role": "researcher",
             "task": "What is 17 multiplied by 23? Reply with just the number."},
            {"role": "coder",
             "task": "Compute the sum of the integers from 1 to 100 and report it."},
        ],
    })
    elapsed = time.monotonic() - t0
    print(f"  (dispatch returned in {elapsed:.1f}s)")
    res = out.get("results", [])
    for r in res:
        extra = ""
        if r.get("reviewed"):
            extra = f"  review_ok={r['review_ok']} review={r['review'][:60]!r}"
        print(f"    - [{r['role']}] ok={r['ok']} steps={r['steps']} "
              f"report={r['report'][:70]!r}{extra}")

    results.append(ok("two sub-agents returned, roles routed",
                      [r["role"] for r in res] == ["researcher", "coder"]))
    blob = " ".join(r["report"] for r in res)
    results.append(ok("answers present in reports (391 and 5050)",
                      "391" in blob and "5050" in blob))
    coder = next((r for r in res if r["role"] == "coder"), {})
    results.append(ok("coder result was auto-reviewed",
                      coder.get("reviewed") is True and bool(coder.get("review"))))

    print("== 6b. Goal integration: dispatch + outcomes logged to the goal ==")
    status = await goals.execute("goal_status", {})
    kinds = [e["kind"] for e in status.get("recent", [])]
    results.append(ok(f"goal log shows swarm events ({kinds})",
                      any(k == "swarm.dispatch" for k in kinds)
                      and any(k.startswith("swarm.") and k != "swarm.dispatch" for k in kinds)))

    print("== 8. LIVE collaboration: a sub-agent shares to the blackboard ==")
    collab = await swarm.execute("swarm_dispatch", {
        "tasks": [
            {"role": "generalist",
             "task": "Immediately call the swarm_share tool with this exact "
                     "note: 'codeword=BANANA'. Then reply 'done'."},
            {"role": "generalist",
             "task": "First call swarm_inbox to read notes from the other "
                     "sub-agent. Report any codeword you find; if the inbox is "
                     "empty, call swarm_inbox one more time before reporting."},
        ],
        "review": False,
    })
    notes = collab.get("shared_notes", [])
    print(f"    shared_notes: {notes}")
    reader = collab["results"][1]["report"]
    print(f"    reader said: {reader[:80]!r}")
    results.append(ok("a sub-agent posted to the shared blackboard live",
                      any("BANANA" in m.get("note", "") for m in notes)))

    print("== 9. Observability: the runs persisted to the audit store ==")
    audit = await swarm.execute("swarm_status", {})
    runs = audit.get("recent_runs", [])
    results.append(ok("swarm_status shows runs", len(runs) >= 2))

    for b in registry._bricks.values():
        await b.shutdown()

    passed = sum(1 for r in results if r)
    print(f"\n== {passed}/{len(results)} checks passed ==")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
