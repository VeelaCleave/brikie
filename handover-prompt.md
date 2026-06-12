# 🧱 Brikie — Handover Prompt

## Session Context (June 12, 2026 — Session 2)

You are the next Brikie session. Here's what happened before you woke up:

### Phase A, B, C ✅ All Done
- Ground-up CLI rewrite, working agent loop, soul prompt injection
- Kernel purity — kernel imports nothing from `brikie.bricks`
- Ninite-style installer with Build Set system
- BRK-NNN numbering for all 30 bricks
- Foreman (BRK-500), Dreamer (BRK-510), Mason (BRK-540) with full AFK loop
- AFKProtocolEngine — Dreamer proposes → Foreman approves/rejects → Mason builds
- 387 tests all green (up from 371)

### 🔥 What Happened This Session (Session 2)

The entire session was a **memory subsystem triage and repair** effort:

1. **You reported** `sqlite3.OperationalError: no such table: dag_nodes`
2. **Root cause found:** `VersionedConnectionPool.initialize()` would write its schema blindly into whatever file it was given. If any code path pointed `LcmStore` at `mempalace.db`, it would silently create `dag_nodes`, `sessions`, `messages` etc. there — corrupting the database.
3. **Defensive fix applied:** Added a `__init_subclass__`-based `_KNOWN_DB_FILENAMES` registry to `VersionedConnectionPool`. On `initialize()`, if the target file's basename matches another pool's `DB_FILENAME`, it raises `SchemaIsolationError`. Temp files for tests are allowed freely.
4. **Other fixes this session:**
   - `_unwrap_hook_data()` helper — peels HookEvent envelopes in callbacks
   - `_intercept_user_message()` — user messages now persist into all 3 memory bricks
   - `_build_memory_blob()` normalized across all 3 brick shapes (LCM summaries/tail, MemPalace entities/triples, Wiki pages)
   - `get_recent_entities()` added to MemPalace for context injection
   - HookEvent unwrapping in `diagnostics.py` and `token_logger.py`
   - `edit_file` tool added to file_tools — surgical oldString→newString replacement
   - MAX_AGENT_STEPS raised from 25 to 500
5. **Comprehensive triage doc saved:** `docs/memory-triage-2026-06-12.md`
6. **All 387 tests pass** — verified clean after all fixes
7. **Commit:** `b01f391` — `fix: memory schema isolation guard + tripartite memory blob normalization`

### 🧠 Known Issues / Technical Debt

**High Priority:**
- **Entity extractor is too aggressive** — 252 entities, ~80% noise (stop words like "let", "the", "your" classified as `person` type). Needs a stop-word filter, minimum length check, and proper `entity_type` classification instead of the current regex heuristics. The MemPalace knowledge graph is currently polluted with garbage entities.
- **11 relationships extracted** — very low. The triple extractor is barely firing because it requires explicit `subject-predicate-object` patterns. Most useful triples aren't being captured.
- **No entity deduplication** — "Redis" appears 3+ times as separate entities (`person`, `tool`, `concept`) with different IDs. `upsert_entity` doesn't actually upsert — it always creates a new row.
- **Wikipedia database filename mismatches** — some tests reference `test_wiki.db` while others reference `wiki.db`. Not a bug currently but a naming inconsistency.
- **`_ALLOWED_FILENAMES` was the first approach for isolation, then swapped for `__init_subclass__`.** The old attribute name still appears in commit history if anyone searches for it.

**Medium:**
- `WikiBrick` auto-extract requires ≥200 chars with Markdown headings — too aggressive for normal conversation
- MemPalace entity types are limited to 6 hardcoded types (`person`, `project`, `tool`, `concept`, `decision`, `milestone`)
- Spatial hierarchy (wing → room → hall → tunnel → drawer) is fully implemented in schema but no code creates spatial mappings yet
- `WikiBrick` docs directory is a temp dir that doesn't persist between sessions

### 💾 Database State (after this session)
All three databases restored and verified clean:

| DB | Tables | Entity Count |
|---|---|---|
| `lcm.db` | sessions, messages, **dag_nodes**, token_budgets, compaction_log | 5 messages in tail |
| `mempalace.db` | entities, triples, spatial_map, spatial_regions | 252 entities (needs cleanup), 11 triples |
| `wiki.db` | pages, links, tags | ~9 auto-extracted pages |

Schema isolation guard is active — you **cannot** accidentally write LCM schema into mempalace.db anymore.

### 📁 Key Files

| File | Purpose |
|---|---|
| `AGENTS.md` | Working contract — read this first |
| `design.md` | Historical architecture blueprint |
| `docs/memory-triage-2026-06-12.md` | Full triage document from this session |
| `brikie/kernel/event_loop.py` | The main agent loop + AFK wiring |
| `brikie/kernel/soul_actor.py` | DreamerActor, ForemanActor classes |
| `brikie/kernel/afk_protocol.py` | AFKProtocolEngine — negotiation loop |
| `brikie/bricks/memory/sqlite_pool.py` | Schema isolation guard lives here |
| `brikie/bricks/memory/mempalace/entity_extractor.py` | The noisy entity extractor — needs a stop-word filter |
| `brikie/bricks/build/sets/default.json` | Current build set (15 bricks + 3 souls) |
| `brikie/bricks/build/sets/afk.json` | AFK build set (with souls) |

### 🧪 Test Command
```bash
python3 -m pytest tests/ -q
```
Expect **387 passing**.

### 🔧 Run Command
```bash
echo "What bricks am I running?" | python3 -m brikie --set default
```

### 🎯 What's Next / Immediate Tasks

1. **🔴 Clean up the entity extractor** — add stop-word list, minimum entity length (≥3 chars), proper type classification. This is the highest-impact fix for MemPalace quality.
2. **🟡 Fix upsert_entity** — currently INSERTs always, never UPDATEs. Should match by normalized name.
3. **🟡 Plugin the spatial hierarchy** — the schema supports `wing → room → hall → tunnel → drawer` but nothing populates it yet.
4. **🟢 Run `/afk 1`** and watch the Dreamer → Foreman → Mason loop live
5. **🟢 Test the CommandFirewall** — try dangerous shell commands
6. **🟢 Fill up LCM context** — trigger compaction, expand back with `lcm_expand`
7. **🟢 Try the `afk` build set** — `python3 -m brikie --set afk`

### 🚀 The Vision
> "Build your agent · brick by brick"
>
> Phase D = brikie.co registry + dynamic brick install + agent-authored bricks
>
> The crown isn't stolen — it's built. 🧱👑
