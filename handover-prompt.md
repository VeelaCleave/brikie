# 🧱 Brikie — Handover Prompt

## Session Context (June 12, 2026 — Session 4)

You are the next Brikie session. Here's the state of the world:

### Phases A–E ✅ All Done
- Ground-up CLI rewrite, working agent loop, soul prompt injection (A)
- Kernel purity, souls-as-config, Build Sets, Ninite installer prototype (B)
- Foreman (BRK-500) / Dreamer (BRK-510) / Mason (BRK-540) full AFK loop (C)
- brikie.co registry client — real dynamic install, agent-authored bricks (D)
- **brikie.co server side** — registry server, publish, web installer (E)
- 442 tests all green, ruff clean repo-wide

### 🔥 What Happened This Session (Session 4 — Phase E)

**1. The brikie.co server exists (`brikie/server/`, stdlib-only)**
Run with `python3 -m brikie.server --port 8321 --data-dir ~/.brikie/registry`
(optional `--base-url https://brikie.co` for production; otherwise download
URLs derive from the request Host header).
- `store.py` — `RegistryStore`: filesystem layout
  `{data_dir}/{name}/{version}/{manifest.json,source.py}`; validates
  name/version/type, syntax-checks source, computes sha256, 409 on
  re-publish of an existing version; numeric (not lexicographic) latest-
  version resolution. Manifests store *relative* download_urls; the
  serving layer absolutizes them.
- `registry_server.py` — `RegistryServer` (ThreadingHTTPServer): routes
  match `RegistryClient` exactly — `/bricks/index.json`,
  `/bricks/search?q=`, `/bricks/{name}[/{ver}]/manifest.json`,
  `/bricks/{name}/{ver}/source.py`, `POST /bricks/publish`. Embeddable
  (`start()`/`shutdown()`, `port=0` for tests) or blocking
  (`serve_forever()`).
- `website.py` — the Ninite part: brick-picker HTML at `/` (brick-orange
  theme, reuses `CATALOG` from `brikie/install.py` so web and local
  installer can't drift), `GET /buildset.json?bricks=BRK-..&name=..`,
  `GET /install.sh?...` (POSIX script: pip-installs brikie from GitHub if
  missing, writes the Build Set into the package sets dir). Minimum-stack
  validation mirrors `BuildLoader.validate_minimum_stack`.

**2. `registry_publish` — the sixth BRK-450 tool**
- `RegistryClient.publish(manifest, source_code)` POSTs to
  `{registry}/publish`; server is the authority on checksum/download_url.
- The installer brick reads the manifest sidecar + source that
  `registry_create_brick` wrote, publishes, then rewrites the local
  sidecar with the canonical manifest. Version defaults to the highest
  local one.

**3. Two kernel bugs found by live testing (the loop, not unit tests)**
- `process_tool_calls` only caught `(KeyError, ValueError)` — a
  RegistryError (or anything an agent-authored brick raises) **crashed
  the whole event loop**. Now: KeyError still falls through to the next
  brick (schema advertised but not dispatched); any other exception
  settles the call as `Tool error (Type): msg` so the model can react.
  Live-verified: duplicate publish → model received the 409 and explained
  the fix (bump the version).
- Tool schemas were collected once per *user turn*, so a brick installed
  mid-turn wasn't callable until the next prompt — the model then
  **fabricated** the tool result. Now `_collect_tool_schemas()` runs every
  model round: search → install → call the new tool works in one turn.

**4. Live verification (all real, local vLLM deepseek-v4-flash)**
- `curl -fsSL "http://127.0.0.1:8321/install.sh?bricks=BRK-300,BRK-200,BRK-410&name=webmini" | sh`
  → build set written → `brikie --set webmini` boots and answers.
- Agent A authored `compliment` v0.1.0 via registry_create_brick, then
  registry_publish → checksum + registry download_url returned.
- Fresh agent B (empty install dir): registry_search → registry_install
  (checksum-verified download) → called `get_compliment(name='Veela')`
  in the same turn → "your mortar consistency is absolutely legendary!"

### 🧠 Known Issues / Technical Debt
- ~~brikie.co not deployed~~ → LIVE as of this session (see Deployment)
- ~~No publish auth~~ → shipped (Bearer token, commit 145ba98)
- Published `tool_schemas` in manifests are empty for agent-authored
  bricks (the sidecar doesn't capture the class-level `tools` attr)
- Sentence-initial real names ("Veela asked...") skipped by the person
  heuristic; triple extractor recall still modest (regex-based)
- WikiBrick docs dir doesn't persist between sessions
- Mason has no hard sandboxing yet (SandboxSecurityBrick exists, unused
  by Masons)

### 🧪 Test / Run Commands
```bash
python3 -m pytest tests/ -q        # expect 442 passing
ruff check brikie/ tests/          # expect clean
python3 -m brikie.server --port 8321 --data-dir /tmp/reg   # the registry
echo "What bricks am I running?" | python3 -m brikie --set full
```
To point BRK-450 at a local registry, give it config in a build set:
`{"brk": "BRK-450", "config": {"registry_url": "http://127.0.0.1:8321/bricks"}}`

### 🎯 What's Next — Phase F (hardening & depth)

**Deployment — DONE. brikie.co is LIVE** (OVH VPS 54.38.78.229,
Ubuntu 26.04; ssh -i ~/.ssh/id_ed25519_shared ubuntu@54.38.78.229).
Publish auth shipped (Bearer token; server token in
/etc/brikie/registry.env on the VPS). Landing page with falling-brick
hero deployed. First community brick `compliment` v0.1.0 published and
verified by a local agent installing it from the DEFAULT registry URL.
Redeploy: `git archive HEAD | ssh … 'tar -x -C /opt/brikie/app'` then
pip install -e + `sudo systemctl restart brikie-registry`.
Remaining deployment niggles:
1. The GitHub repo is PRIVATE — the generated install.sh's
   `pip install git+https://github.com/VeelaCleave/brikie` fails for
   the public. Make the repo public (Veela's call) or publish to PyPI.
2. www.brikie.co cert may still be pending (LE cached the old parking
   IP; Caddy auto-retries — check `journalctl -u caddy`). Apex is fine.

**Steerable Dreamer + GitHub flywheel (design agreed with Veela this
session — build in this order):**
3. Operator focus: `focus` field on the Dreamer soul manifest (build set
   config) + a `/focus <text>` runtime command (StateManager); prepended
   to `DreamerActor.propose()`'s prompt
4. Dream Sources as a duck-typed capability: any registered brick with
   `dream_context() -> str` contributes a section to
   `_build_dream_context()` in afk_protocol.py (currently hardwired to
   DiagnosticsCollector only). Diagnostics becomes the first source.
5. GitHubBrick (400-block tool brick): issue/PR tools via `gh` or
   httpx+token, AND a dream source mining open issues. Add `source`
   field to the Proposal dataclass (provenance: "diagnostics" |
   "github#42" | "operator-focus") so Foreman can prioritize and Mason
   PRs can reference issues.
   ⚠ SAFETY (agreed): only mine issues carrying a maintainer-applied
   label (e.g. `dreamer-approved`) — raw public issue text is a prompt-
   injection surface. Masons branch + PR only, never push master.
   Mason hard sandboxing (6) is a prerequisite for PR-creating Masons.

**Backlog:**
6. Mason hard sandboxing via SandboxSecurityBrick
7. Persistent wiki docs dir
8. LLM-based entity extraction as an optional memory brick
9. Capture `tool_schemas` into create_brick's manifest sidecar

### 🚀 The Vision
> "Build your agent · brick by brick"
>
> The crown isn't stolen — it's built. 🧱👑
