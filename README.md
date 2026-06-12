# 🧱 brikie

**build your agent · brick by brick**

brikie is a modular agent harness where **every capability is an
optional, hot-swappable Brick** seated on a minimal kernel. There is no
fixed feature set: you pick an interface, a provider, and whatever else
you want — tools, memory, logging, security, orchestration souls — and
that *is* your agent.

**→ [brikie.co](https://brikie.co)** — pick your bricks, get a one-line
installer.

## Quick start

```sh
pip install brikie
brikie
```

That's it. First run opens a 60-second setup: brikie detects running
local servers (Ollama, LM Studio, vLLM) and API keys already in your
environment, and one keystroke later you're chatting. Rerun it any time
with `brikie --onboard`.

Want to choose your bricks up front instead? Compose a custom stack at
[brikie.co](https://brikie.co) and run the one-liner it gives you:

```sh
curl -fsSL "https://brikie.co/install.sh?bricks=BRK-300,BRK-200,BRK-410,BRK-420&name=custom" | sh
brikie --set custom
```

(or locally: `python3 -m brikie.install`)

## What makes it different

- **Everything is optional.** The only minimum is one Interface Brick +
  one Provider Brick. No brick may assume another exists.
- **Agents grow themselves.** A running agent can search the
  [brikie.co registry](https://brikie.co), install bricks at runtime
  (sha256-verified), author brand-new bricks from source
  (`registry_create_brick`), and publish them back for everyone else
  (`registry_publish`). Tools seated mid-conversation are callable on
  the very next model round.
- **AFK mode.** A Dreamer soul mines diagnostics and proposes
  improvements, a Foreman evaluates them against its constraints, and
  Mason sub-agents build the approved ones.
- **Tripartite memory.** Lossless context (SQLite + DAG compaction), a
  spatial knowledge graph (MemPalace), and a synthesized markdown wiki —
  each one an optional brick.

## The brick blocks

| Block | Category | Examples |
|-------|----------|----------|
| 200 | Provider | HTTP provider (OpenAI/Claude-format APIs, local vLLM) |
| 300 | Interface | transcript CLI, internal event bus |
| 400 | Tool | shell/file tools, CloakBrowser, registry installer |
| 500 | Soul | Foreman, Dreamer, Mason (config, not runtime bricks) |
| 600 | Memory | LCM, MemPalace, Wiki |
| 700 | Logging | token logger, tool tracer, diagnostics |
| 800 | Security | command firewall, sandbox |
| 900 | Improvement | auto-fixer |

## Don't want an agent on bare metal?

Run it jailed. The official image confines the agent's shell and file
tools to whatever directory you launch it from — the rest of your
machine doesn't exist as far as it's concerned:

```sh
docker run -it --rm \
  -v "$PWD":/workspace \
  -e ANTHROPIC_API_KEY \
  ghcr.io/veelacleave/brikie --preset anthropic
```

Or tick **"run isolated (Docker)"** on [brikie.co](https://brikie.co)
and the generated installer does it all for you. brikie also runs
inside [NVIDIA OpenShell](https://github.com/NVIDIA/OpenShell) for
managed credentials and policy-controlled networking — see
[examples/openshell](examples/openshell/README.md).

## Running your own registry

The brikie.co server is stdlib-only:

```sh
python3 -m brikie.server --port 8321 --data-dir ~/.brikie/registry
```

It serves the brick index, manifests, checksummed downloads, the
installer generator, and a token-protected publish endpoint
(`--publish-token` / `$BRIKIE_PUBLISH_TOKEN`).

## Development

```sh
pip install -e ".[dev]"
python3 -m pytest tests/ -q    # 442 tests
ruff check brikie/ tests/
```

Read [AGENTS.md](AGENTS.md) before contributing — it is the working
contract for every developer and agent on this codebase, including the
non-negotiable architecture rules and the definition of done.

---

> The crown isn't stolen — it's built. 🧱👑
