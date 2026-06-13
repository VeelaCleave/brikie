# Brikie — AGENTS.md

Read this before touching anything. It is the working contract for every
developer and agent on this codebase.

## What is this project

Brikie is a modular agentic harness. **Every capability is an optional,
hot-swappable Brick** seated on a minimal Baseplate kernel. A user installs
brikie by picking bricks (at minimum: one Interface Brick + one Provider
Brick) — the eventual brikie.co website generates custom installers from
brick selections, Ninite-style. Out of the box, the default Brick Set is:
CLI + local deepseek provider + file tools + CloakBrowser web browsing.

An internal historical blueprint (`design.md`, untracked) predates the
build; where any older document and this file disagree, this file wins.

## Current roadmap

| Phase | Scope | Status |
|-------|-------|--------|
| A | Working transcript-style CLI, agent loop, soul prompt injection | ✅ done |
| B | Kernel purity, souls-as-config, build sets, installer prototype | ✅ done |
| C | Real LLM-driven Dreamer ⇄ Foreman AFK negotiation, Mason executor sub-agents, soul prompt wiring | ✅ done |
| D | brikie.co registry (kadeia renamed), real dynamic brick install (download + checksum + seat), agent-authored bricks (`registry_create_brick`), uninstall | ✅ done |
| E | brikie.co server side (`brikie/server/`): registry index + publish endpoint, `registry_publish` tool, Ninite installer generation (web page → buildset.json → `curl \| sh`) | ✅ done |
| F | Hardening & depth: Mason hard sandboxing, persistent wiki docs dir, LLM-based entity extraction brick, brikie.co deployment | next |

## Architecture — the rules that are not negotiable

1. **The kernel imports nothing from `brikie.bricks`.** `brikie/kernel/`
   may import brick types under `if TYPE_CHECKING:` only. At runtime the
   kernel discovers capabilities structurally: any registered brick with
   `get_hook_callbacks()` gets its middleware hooks wired; anything with
   `build_context()` + `intercept_message()` is treated as memory. If your
   new brick category needs kernel support, add a duck-typed capability,
   not an import.
2. **Every brick is optional.** The only minimum is ≥1 Provider + ≥1
   Interface (enforced by `BuildLoader.validate_minimum_stack()`). Never
   write code that assumes a memory, logging, security, or tool brick
   exists. Probe with `hasattr` and degrade gracefully.
3. **No provider defaults in code.** No hardcoded model names, API URLs,
   or keys anywhere except Build Set JSONs and explicit CLI flag
   overrides. The user's brick choice decides the provider — full stop.
4. **Souls are configuration, not runtime bricks.** The BRK-500 block is
   dataclass persona manifests loaded into `BuildSet.souls`. They are
   never registered with the BrickRegistry and have no `init()`.
5. **Build Set JSON is the product contract.** `brikie/bricks/build/sets/`
   is what `brikie/install.py` writes locally and what brikie.co will
   generate server-side. Schema changes there are breaking changes —
   treat them like a public API.
6. **Expensive resources initialize lazily.** `init()` must be fast and
   must not fail the boot for an optional capability (see CloakBrowser:
   the browser launches on first tool use, not at warm-up).

## Adding a brick — the checklist

1. Implement the category ABC (`brikie/bricks/<category>/base.py`).
2. Set a class-level `BRICK_NUMBER` in the right 100-block
   (`brikie/config/brick_numbers.py` documents the blocks).
3. Register it in **both** `BRICK_NUMBERS` (config) and `BRICK_INDEX`
   (build loader), and update the pinned count in
   `tests/test_brick_numbers.py::test_registry_count`.
4. Add it to the installer catalog in `brikie/install.py` with a
   one-line blurb.
5. Tool bricks: expose OpenAI-format schemas via a `tools` class
   attribute and dispatch in `execute(name, args)`. Tool failures return
   structured error payloads — never let one tool call crash the loop.
   The kernel backstops this (`process_tool_calls` settles any exception
   as a `Tool error (...)` result), but don't rely on it: agent-authored
   bricks go through the same path, so keep your own errors descriptive.
6. Write tests, then **verify it live** (see Definition of Done).

## Interface & provider contracts

- Providers return `(content, tool_calls, meta)`; `meta` carries
  `reasoning`, `usage` ({prompt,completion}_tokens), `finish_reason`.
  A plain `(content, tool_calls)` 2-tuple is also accepted.
- The event loop renders through **one** path: it prefers an interface's
  optional `render_*` methods (`render_assistant_response`,
  `render_thinking`, `render_tool_calls`, `render_tool_result`,
  `render_startup`, `render_info`, `render_error`, `set_busy`,
  `update_usage`) and falls back to `output()`. Never call both for the
  same content — double rendering was a real shipped bug.
- The CLI is transcript-style (scrollback-friendly, pipe-safe). All model
  thinking and every tool call/result must be visible to the user.
  Interfaces must work non-interactively: piped stdin in, plain text out.

## Naming

- **Foreman** (BRK-500) — site-boss orchestrator. **Dreamer** (BRK-510) —
  proposal generator. **Mason** — executor sub-agents. The name
  *Sisyphus* is retired; do not reintroduce it.
- The central registry is **brikie.co** — live at https://brikie.co
  (the old *kadeia* naming is fully retired).

## Definition of Done

Unit tests passing is **not** done. This project once had 346 green tests
while the app showed a blank screen — the gap was integration. Before
committing:

1. `python3 -m pytest tests/ -q` — all green, no skips you added.
2. `python3 -m ruff check` on every file you touched — clean.
3. **Run the real thing**: `echo "<prompt>" | python3 -m brikie --set
   default` (a local vLLM serves `deepseek-v4-flash-spark` at
   `localhost:8000/v1`). Watch your feature actually work end to end.
   For interactive-only behavior, use `script -qec "python3 -m brikie
   --set <set>" /dev/null` to fake a TTY.
4. `git status` — confirm everything you created is actually staged.
   (An unanchored gitignore once silently dropped the entire Build Set
   system from the repo. Anchor ignore patterns to the root: `/build/`.)

## Operating discipline (what the model actually reads)

This file is the full contract, but it is long and is **not** injected into
the model's context. The distilled, always-on slice the local model reads
on every turn lives in `brikie/config/operating_discipline.py` and is
appended to the system prompt by `_build_provider_messages`. It is a
**living artifact**: when the reviewer corrects a recurring local-model
mistake (e.g. "tests pass ≠ done", shipping a lossy shortcut, dropping a
guardrail), encode the correction *there* — not only in goals.md — so it
persists into the next run instead of being re-explained forever. Keep it
tight (it costs tokens every turn) and keep every rule anchored to the real
failure that earned it. Toggle off per-persona with `BRIKIE_DISCIPLINE=0`.

## Code standards

- Python 3.11+, asyncio, full type hints. Match the existing style:
  module docstring stating purpose, section-divider comments, Google-style
  arg docs on public methods.
- No placeholders, no stubs, no "rest of code here", no simulated
  results presented as real ones. If a feature can't be real yet (e.g.
  nothing answers the Foreman's queue until Phase C), make the limitation
  explicit in behavior — time out honestly, log it, surface it — never
  fake success.
- Catch narrow exceptions. A brick failure degrades that brick, not the
  Baseplate.
- Commit messages: conventional prefix (`feat:`, `fix:`, `chore:`),
  body explains *why* and what was verified.

## Gotchas

- `*.db` files (lcm/mempalace/wiki) are written to the cwd and
  gitignored — never commit them, never assume they exist.
- `brikie.egg-info/` and `.omo/` are local artifacts; ignore them.
- The `afk` set's `/afk` currently runs bounded heuristic cycles with an
  evaluation timeout — by design, until Phase C lands the real actors.
