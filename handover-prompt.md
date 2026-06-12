# 🧱 Brikie — Handover Prompt

## Session Context (June 12, 2026)

You are the next Brikie session. Here's what happened before you woke up:

### Phase A & B ✅ Done
- Ground-up CLI rewrite, working agent loop, soul prompt injection
- Kernel purity — kernel imports nothing from `brikie.bricks`
- Ninite-style installer with Build Set system (JSON manifests)
- BRK-NNN numbering for all 30 bricks
- Foreman (BRK-500, renamed from Sisyphus), Dreamer (BRK-510), Mason (BRK-540)

### Phase C ✅ SHIPPED (commit `267953c`)
- **SoulActor** (`brikie/kernel/soul_actor.py`) — binds any Soul persona to any Provider Brick for LLM-driven completions
- **DreamerActor** — reads diagnostics, generates structured JSON proposals via LLM
- **ForemanActor** — full event-bus service loop evaluating proposals (approve/defer/reject)
- **Mason** (BRK-540) — scope-locked builder sub-agent that executes approved jobs with tool bricks
- **AFKProtocolEngine upgrade** — `dreamer_propose` callback wires real LLM dreaming, `on_stage` for live narration, timeout-configurable evaluation, diagnostic context builder pulls session stats + recent events
- **Event loop** — spawns ForemanActor background task, runs Mason sub-agent loops, pre-settled tool calls so security bricks (firewall) stay in the pipeline
- **CLI** — `render_afk_event()` with color-coded dreamer/foreman/mason output, `/afk N` and `/afk inf` parsing
- **default.json** — upgraded to full 12-brick stack:
  - BRK-200 (HTTP provider), BRK-300 (CLI), BRK-410 (file tools), BRK-420 (CloakBrowser)
  - BRK-600 (LCM), BRK-610 (MemPalace), BRK-620 (Wiki)
  - BRK-700 (TokenLogger), BRK-710 (ToolTracer), BRK-720 (Diagnostics)
  - BRK-800 (CommandFirewall), BRK-900 (AutoFixer)
- **371 tests all green**

### 🔥 What Happened This Session
1. I (the previous Brikie) tested the web browser and researched **Hermes Agent** vs Brikie
2. You showed me the full repo at `/home/veela/brikie` with its 30 numbered bricks, 74 Python files, 12k LOC, 35 commits
3. Claude ran out of tokens mid-Phase-C — "this is why local models are better" 😄
4. I updated `default.json` to the full stack and **booted it live** — 12 bricks seated, agent self-explored, LCM/MemPalace/Wiki all active
5. I committed Phase C as `267953c "Pre-breaking stuff baseline"` and pushed

### 🎯 What's Next
- **Phase C is done** — the Dreamer → Foreman → Mason loop works. The `/afk 1` / `/afk inf` command is live.
- **Phase D** is next: brikie.co registry, dynamic brick install, agent-authored bricks
- The default set now boots with memory + logging + security + improvement bricks
- Local deepseek-v4-flash-spark provider runs at `localhost:8000/v1`

### ⚡ Future sessions — stuff to try right away:
1. **Run `/afk 1`** and watch the Dreamer dream → Foreman decides → Mason builds
2. **Try dangerous shell commands** — test the CommandFirewall (BRK-800)
3. **Stress MemPalace** — chat for 20+ turns, restart, and see if memories persist
4. **Test LCM expansion** — fill up context, trigger compaction, expand back
5. **Feed the Dreamer broken diagnostics** and see if the Foreman catches it
6. **Try the `afk` build set** — `python3 -m brikie --set afk` loads souls too

### 📁 Key Files
| File | Purpose |
|---|---|
| `AGENTS.md` | Working contract — read this first |
| `design.md` | Historical architecture blueprint |
| `brikie/kernel/event_loop.py` | The main agent loop + AFK wiring |
| `brikie/kernel/soul_actor.py` | DreamerActor, ForemanActor classes |
| `brikie/kernel/afk_protocol.py` | AFKProtocolEngine — the negotiation loop |
| `brikie/bricks/soul/foreman.py` | Foreman persona dataclass |
| `brikie/bricks/soul/dreamer.py` | Dreamer persona dataclass |
| `brikie/bricks/soul/mason.py` | Mason persona dataclass (BRK-540) |
| `brikie/bricks/build/sets/default.json` | Current default build set (12 bricks) |
| `brikie/bricks/build/sets/afk.json` | AFK build set (with souls) |
| `brikie/config/brick_numbers.py` | All 30 brick registrations |

### 🧪 Test Command
```bash
python3 -m pytest tests/ -q
```
Expect 371 passing.

### 🔧 Run Command
```bash
echo "What bricks am I running?" | python3 -m brikie --set default
```

### 🚀 The Vision
> "Build your agent · brick by brick"
> 
> Phase D = brikie.co registry + dynamic brick install + agent-authored bricks
> 
> The crown isn't stolen — it's built. 🧱👑
