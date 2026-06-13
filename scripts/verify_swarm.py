"""Live verification for the Swarm tier (BRK-470 + SubAgentRunner, goals #4).

Boots the REAL BuildLoader + kernel warm-up (provider, shell tools, goals,
command firewall, swarm) against the LOCAL deepseek model, then proves the
swarm end to end:

  1. swarm_dispatch fans 2 role-specialized sub-agents out CONCURRENTLY
     against the live model and aggregates their reports.
  2. Role routing — the researcher and coder come back tagged by role.
  3. Recursion guard — a sub-agent's toolset excludes the swarm tools, so
     it cannot dispatch its own swarm.
  4. Security stays in path — a dangerous tool call inside a sub-agent is
     vetoed by the REAL CommandFirewall through the shared hook dispatcher,
     exactly as it would be for the coordinator.
  5. Observability — the run + per-sub-agent outcomes persist to the audit
     store and surface via swarm_status.

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
                                "max_steps": 4, "max_parallel": 2}}),
    ]
    for brk, cfg in specs:
        b = loader._instantiate(brk, cfg)
        registry.register(b)
        print(f"  loaded {brk} -> {type(b).__name__} (name={b.name})")

    # Warm up + register hook callbacks (this is what puts the firewall in path).
    for brick in list(registry._bricks.values()):
        await brick.init()
    for brick in registry._bricks.values():
        getter = getattr(brick, "get_hook_callbacks", None)
        if getter is None:
            continue
        cbs = getter()
        for ht, cb_list in cbs.items():
            for cb in cb_list:
                hooks.register(ht, cb)

    swarm = next(b for b in registry._bricks.values() if b.name == "swarm")
    results = []
    results.append(ok("swarm brick has registry + hooks injected",
                      swarm._registry is registry and swarm._hooks is hooks))

    print("== 2. Recursion guard: sub-agents can't see the swarm tools ==")
    schemas = swarm._subagent_tool_schemas()
    names = {s["function"]["name"] for s in schemas}
    results.append(ok(f"sub-agent toolset excludes swarm_* (has {len(names)} real tools)",
                      "swarm_dispatch" not in names and len(names) > 0))

    print("== 3. Security in path: firewall vetoes a dangerous sub-agent call ==")
    # Drive the runner's tool path directly with a crafted destructive call,
    # through the SAME hook dispatcher — deterministic, no reliance on the
    # model choosing to do something dangerous.
    async def exec_tool(name, args):
        return "SHOULD-NOT-RUN"

    runner = SubAgentRunner(provider=None, tool_schemas=schemas,
                            execute_tool=exec_tool, hooks=hooks, label="probe")
    msgs, used, blocked = await runner._run_tools([
        {"id": "x1", "function": {"name": "bash_execute",
                                  "arguments": '{"command": "rm -rf /"}'}}
    ])
    vetoed = bool(blocked) and all("SHOULD-NOT-RUN" not in m["content"] for m in msgs)
    results.append(ok(f"destructive bash vetoed by firewall ({blocked[:1]})", vetoed))

    print("== 4. LIVE swarm_dispatch — 2 role-based sub-agents, concurrent ==")
    t0 = time.monotonic()
    out = await swarm.execute("swarm_dispatch", {
        "context": "We are smoke-testing the brikie swarm.",
        "tasks": [
            {"role": "researcher",
             "task": "What is 17 multiplied by 23? Reply with just the number, "
                     "then a one-line note."},
            {"role": "coder",
             "task": "Compute the sum of the integers from 1 to 100 and report "
                     "the result with a one-line explanation."},
        ],
    })
    elapsed = time.monotonic() - t0
    print(f"  (dispatch returned in {elapsed:.1f}s)")
    res = out.get("results", [])
    for r in res:
        print(f"    - [{r['role']}] ok={r['ok']} steps={r['steps']} "
              f"report={r['report'][:90]!r}")

    results.append(ok("two sub-agents returned", len(res) == 2))
    results.append(ok("roles routed correctly (researcher, coder)",
                      [r["role"] for r in res] == ["researcher", "coder"]))
    results.append(ok("both produced a non-empty report",
                      all(r["report"].strip() for r in res)))
    blob = " ".join(r["report"] for r in res)
    results.append(ok("answers present in reports (391 and 5050)",
                      "391" in blob and "5050" in blob))

    print("== 5. Observability: the run persisted to the audit store ==")
    status = await swarm.execute("swarm_status", {})
    runs = status.get("recent_runs", [])
    results.append(ok("swarm_status shows the run with task_count=2",
                      bool(runs) and runs[0]["task_count"] == 2))

    for b in registry._bricks.values():
        await b.shutdown()

    passed = sum(1 for r in results if r)
    print(f"\n== {passed}/{len(results)} checks passed ==")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
