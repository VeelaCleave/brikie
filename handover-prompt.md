# 🧱 Brikie — Handover Prompt

## Session Context (June 12, 2026 — Session 3)

You are the next Brikie session. Here's the state of the world:

### Phases A, B, C, D ✅ All Done
- Ground-up CLI rewrite, working agent loop, soul prompt injection (A)
- Kernel purity, souls-as-config, Build Sets, Ninite installer prototype (B)
- Foreman (BRK-500) / Dreamer (BRK-510) / Mason (BRK-540) full AFK loop (C)
- **brikie.co registry** — kadeia fully renamed, real dynamic install,
  agent-authored bricks, uninstall (D)
- 405 tests all green, ruff clean repo-wide

### 🔥 What Happened This Session (Session 3)

**1. Memory extraction quality fixed (commit `cc0c276`)**
The entity extractor was classifying any capitalized word as a person
(42 rows of "let", 28 of "the"). Fixes:
- 200+ word stop list + min length 3 in `entity_extractor.py`
- Sentence-start heuristic: a lone capitalized word starting a sentence
  is not a person (known cost: real names at sentence start are skipped)
- Most-specific-type-first dedup — "Redis" can't be tool AND person
- Bare verbs ("decided", "completed") are no longer entities; they still
  steer hall classification
- Triples: leading articles stripped, pronoun subjects dropped, fixed the
  'creates' pattern storing its own verb as the object
- `upsert_entity` / `upsert_triple` now actually upsert (case-insensitive
  name match; 'concept' upgrades to specific types, never downgrades)
- `search_entities` SQL was broken (string literal never concatenated)
- Spatial hierarchy now populated: `intercept_message` creates wing/room/
  hall regions and maps entities (`ensure_region` + `map_entity`)
- One-time cleanup of polluted `mempalace.db`: 307 → ~10 real entities
  (backup at `mempalace.db.bak-20260612`, gitignored)

**2. Phase D shipped (this commit)**
- `kadeia_registry.py` / `kadeia_installer.py` deleted; replaced by
  `registry_client.py` (`RegistryClient`, `RegistryError`) and
  `installer.py` (`RegistryInstallerBrick`, BRK-450, ctor param is
  `registry` so BuildLoader auto-injects the kernel registry — the old
  `brick_registry` param name meant it was NEVER wired)
- Real downloads: httpx fetch + sha256 checksum verification + source
  written to `~/.brikie/bricks/` with a JSON receipt
- `load_brick_from_file`: importlib spec-from-file dynamic load, finds
  the BRICK_NUMBER class, injects registry if accepted, registers, inits
- **Five tools**: registry_search, registry_install, registry_list,
  `registry_create_brick` (agent authors a brick from source — syntax
  check, manifest sidecar for future publishing, seat + init), and
  `registry_uninstall` (shutdown + unregister + optional file deletion)
- BRK-450 seated in the `full` build set
- Live-verified: agent authored a dice brick via registry_create_brick,
  then called its `roll_dice` tool on the next turn (works because
  `_collect_tool_schemas()` re-queries the registry every turn)

### 🧠 Known Issues / Technical Debt
- Sentence-initial real names ("Veela asked...") are skipped by the
  person heuristic — acceptable noise/recall trade-off, revisit if needed
- Triple extractor still regex-based; recall is modest
- WikiBrick auto-extract requires ≥200 chars with headings; docs dir is a
  temp dir that doesn't persist between sessions
- Entity types limited to 6 hardcoded values (schema CHECK constraint)
- Spatial tunnels/drawers exist in schema but nothing maps that deep yet
- registry_install needs a live registry server — brikie.co doesn't exist
  yet, so only create/uninstall are exercisable end-to-end today

### 🧪 Test / Run Commands
```bash
python3 -m pytest tests/ -q        # expect 405 passing
ruff check brikie/ tests/          # expect clean
echo "What bricks am I running?" | python3 -m brikie --set full
```

### 🎯 What's Next — Phase E (brikie.co server side)
1. Registry server: index.json, per-brick manifests, search endpoint —
   even a static-file prototype unblocks registry_install end-to-end
2. `registry_publish` tool: push an authored brick (manifest sidecar is
   already written for exactly this)
3. Website installer generation (the Ninite vision): brikie.co page that
   emits a custom Build Set JSON + install.sh from chosen bricks
4. Soft ideas: Mason hard sandboxing (SandboxSecurityBrick), persistent
   wiki docs dir, LLM-based entity extraction as an optional brick

### 🚀 The Vision
> "Build your agent · brick by brick"
>
> The crown isn't stolen — it's built. 🧱👑
