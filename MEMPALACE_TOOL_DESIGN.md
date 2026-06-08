# MemPalace Tool Schema Design — Phase 3.2

## Overview

The MemPalace Brick exposes **5 read-only traversal tools** to the agent. These tools query the spatial hierarchy (ChromaDB vectors) and temporal knowledge graph (SQLite). The write path (entities, triples, drawers) is handled entirely by the middleware's auto-extraction paradigm.

All tool schemas follow the **OpenAI-compatible function calling format** used by the LCM Brick. Implementation lives in `brikie/bricks/memory/mempalace/tools.py`.

### Tool Suite

| Tool | Purpose | Data Layer |
|------|---------|------------|
| `mempalace_query` | Semantic search within wings/rooms/halls | ChromaDB vectors |
| `mempalace_traverse` | Navigate the spatial hierarchy | Spatial index |
| `mempalace_entities` | Query entity table (people, projects, concepts) | SQLite entities |
| `mempalace_triples` | Query RDF-style relationships with temporal constraints | SQLite triples |
| `mempalace_inject` | L1 Essential Story injection for context window | Both layers |

### Architecture Alignment

The tools map directly to the MemPalace spatial and temporal architecture from `design.md`:

```
Wing (top-level: project-alpha, person-milla, domain-security)
├── Room (nested: auth-migration, api-design)
│   ├── Hall (categories: hall_facts, hall_events, hall_discoveries)
│   │   └── Drawer (raw text chunks in ChromaDB)
│   └── Entities (SQLite: decisions, tools, people)
└── Tunnel (cross-wing bridge: shared room names)
```

---

## Return Format Specifications

### Standard Success Response

```json
{
  "action": "tool_name",
  "result": {
    ...
  }
}
```

### Standard Error Response

```json
{
  "action": "tool_name",
  "error": {
    "code": "MEMPALACE_XXX",
    "message": "Human-readable error message",
    "suggestion": "Recommended next tool call or action",
    "cost": {
      "tokens": 12,
      "latency_ms": 45
    }
  }
}
```

### Error Codes

| Code | Description |
|------|-------------|
| `MEMPALACE_200` | Success |
| `MEMPALACE_404` | Wing/Room/Hall not found |
| `MEMPALACE_410` | Temporal window out of range |
| `MEMPALACE_413` | Token budget exceeded |
| `MEMPALACE_429` | ChromaDB/SQLite rate limit hit |
| `MEMPALACE_500` | SQLite try/finally lock acquired |
| `MEMPALACE_502` | Vector embedding service down |
| `MEMPALACE_503` | SQLite connection leak |
| `MEMPALACE_504` | Query returned no results |

---

## AFK Mode Usage Patterns

### Pattern 1: Context Loading at AFK Start
```json
// Agent starts AFK loop - load context
mempalace_inject(wing="project-alpha", max_tokens=1000)
```

### Pattern 2: Targeted Memory Retrieval
```json
// Agent needs to recall a specific decision
mempalace_query(query="rate limiting algorithm choice", wing="project-alpha", hall="hall_facts", top_k=3)
```

### Pattern 3: Relationship Discovery
```json
// Agent wants to understand dependencies before making changes
mempalace_triples(subject="auth-migration", predicate="depends_on", current_only=true)
```

### Pattern 4: Spatial Navigation + Query
```json
// Agent explores structure, then searches
mempalace_traverse(action="list_wings")
mempalace_query(query="session storage", wing="project-alpha")
```

### Pattern 5: Entity-Driven Exploration
```json
// Agent finds entities, then queries their relationships
mempalace_entities(type="decision", wing="project-alpha")
mempalace_triples(subject="auth-migration", predicate="*", current_only=true)
```

---

## Design Decisions

1. **Read-only interface**: The agent only reads from MemPalace. Write operations (entities, triples, drawers) are auto-extracted via middleware hooks.

2. **OpenAI-compatible schemas**: Follows the exact same pattern as LCM tools. Each tool is a `Dict[str, Any]` with `type`, `function.name`, `function.description`, and `function.parameters`.

3. **Standardized return format**: All tools return a consistent structure with `action`, `result`, and `error` keys.

4. **Spatial-temporal cross-reference**: The tools support navigating between the spatial hierarchy (wings/rooms/halls) and the temporal graph (entities/triples).

5. **Token-efficient design**: The `mempalace_inject` tool uses AAAK compression and configurable token budgets for AFK mode efficiency.

6. **Temporal constraints**: `mempalace_triples` and `mempalace_query` support temporal windowing to handle the time-varying nature of relationships.

7. **Error recovery**: Standardized error codes with suggestions enable the agent to recover from errors in AFK mode without human intervention.

8. **SQLite safety**: All database operations use try/finally blocks (per design.md requirement).

## Files Created

- `brikie/bricks/memory/mempalace/tools.py` — Complete tool schema implementations
- `brikie/bricks/memory/mempalace/__init__.py` — Module exports
