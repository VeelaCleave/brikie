# Memory Subsystem Triage — June 12, 2026

## Symptom

```
sqlite3.OperationalError: no such table: dag_nodes
```

This error occurs when code tries to query a `dag_nodes` table in a SQLite database
that doesn't have it. The `dag_nodes` table belongs exclusively to the **LCM store**
(`lcm.db`). **MemPalace** (`mempalace.db`) and **Wiki** (`wiki.db`) should never
contain it.

---

## Root Cause Analysis

### 1. The `dag_nodes` table is ONLY created by `lcm/schema.sql`

```
lcm/schema.sql    → sessions, messages, dag_nodes, token_budgets, compaction_log
mempalace/schema.sql → entities, triples, spatial_map, spatial_regions
wiki/schema.sql   → pages, links, tags
```

Each brick's `VersionedConnectionPool` subclass overrides `_get_schema_path()` to
point to its own `schema.sql`:

- `LcmConnectionPool`      → `lcm/schema.sql`      ✅
- `MempalaceConnectionPool` → `mempalace/schema.sql` ✅
- `WikiConnectionPool`      → `wiki/schema.sql`      ✅

### 2. How the corruption happens

The `VersionedConnectionPool.initialize()` method does:

```python
async def initialize(self) -> None:
    schema_path = self._get_schema_path()
    conn = await aiosqlite.connect(self._db_path)
    if schema_path.exists():
        schema_sql = schema_path.read_text()
        await conn.executescript(schema_sql)  # ← writes ALL tables into DB
```

**The schema is applied blindly to whatever file `self._db_path` points to.**
If an `LcmStore` is instantiated with `db_path="mempalace.db"`, its `initialize()`
will write `sessions`, `messages`, `dag_nodes`, `token_budgets`, `compaction_log`
into `mempalace.db` — silently corrupting it.

This is a **defense-in-depth weakness**: the schema application trusts the caller
to pass the correct path. There's no guard against writing the wrong schema into
the wrong file.

### 3. How could the wrong path be passed?

Three scenarios:

**A) Direct instantiation with wrong path (most likely during testing/dev):**
```python
store = LcmStore("mempalace.db")  # Bug: wrong file
await store.initialize()          # Writes dag_nodes into mempalace.db!
```

**B) Module-level or import-time execution:**
Nothing in the current codebase does this at import time, but if a test or
debugging session ran LcmStore against the wrong file, the damage is done
immediately.

**C) Shared `db_path` configuration:**
The build sets (`default.json`) don't pass `config` to memory bricks, so they
each use their default paths — which are all different and correct. But if
someone adds a shared `db_path` config key across bricks, that would cause this.

---

## Verification (all passes)

| Test Suite | Tests | Status |
|---|---|---|
| `test_lcm.py` | 23 | ✅ All pass |
| `test_mempalace.py` | 28 | ✅ All pass |
| `test_wiki.py` | ~130 | ✅ All pass |
| `test_memory_integration.py` | 16 | ✅ All pass |
| **Full suite** | **387** | ✅ All pass |

**Database state (as restored):**

| File | Tables | `dag_nodes`? |
|---|---|---|
| `lcm.db` | sessions, messages, **dag_nodes**, token_budgets, compaction_log | ✅ Yes |
| `mempalace.db` | entities, triples, spatial_map, spatial_regions | ✅ No |
| `wiki.db` | pages, links, tags | ✅ No |

---

## Uncommitted Changes in This Session (working tree vs HEAD~1)

All changes are correct — no new bug introduced:

| File | Change | Safe? |
|---|---|---|
| `diagnostics.py` | Added HookEvent unwrap | ✅ |
| `token_logger.py` | Added HookEvent unwrap | ✅ |
| `mempalace_brick.py` | Added `recent_entities` to `build_context` | ✅ |
| `mempalace_store.py` | Added `get_recent_entities()` method | ✅ |
| `event_loop.py` | `_unwrap_hook_data()`, `_intercept_user_message()`, `_build_memory_blob()` normalization | ✅ |

### Key new feature: `_build_memory_blob` now normalizes all three brick shapes

```
LCM      → {"summaries": [...], "tail": [...]}
MemPalace → {"mempalace": {entity_count, triple_count, recent_entities}}
Wiki     → {"wiki": {page_count, recent_pages}}
```

Each brick type gets its own section in the injected context, with the brick's
name as a label. This lets the LLM distinguish session memory from persistent
knowledge.

---

## Current Database Health

**Restored** — the `mempalace.db` was accidentally contaminated with LCM tables
during debugging. It has been cleaned back to its correct state (entities, triples,
spatial_map, spatial_regions only).

---

## Defensive Fixes (Recommended)

| Priority | Fix | Location | Effort |
|---|---|---|---|
| **HIGH** | Add schema isolation — make `VersionedConnectionPool.initialize()` refuse to write LCM schema into files named `mempalace.db` or `wiki.db` | `sqlite_pool.py` | Small |
| **MED** | Add a `validate_schema()` check to each store that verifies the expected tables exist after init | Each store class | Small |
| **LOW** | Consider wrapping DB files in a `databases/` subdirectory instead of using CWD-relative paths | Brick constructors | Medium |

---
*Triage performed by Brikie — 387 tests green, all three databases healthy.*
