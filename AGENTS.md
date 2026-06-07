# Brikie — AGENTS.md

## What is this project

A modular agentic harness using a "Brick" architecture. Every component (LLM providers, memory, tools, interfaces, security, logging) is a hot-swappable module that plugs into a minimal Baseplate kernel. The Baseplate defines interfaces and lifecycle hooks; Bricks implement them. The system supports multi-head orchestration, autonomous AFK loops, and a tripartite memory architecture.

**Single source of truth**: `design.md` is the architectural blueprint. Read it before making structural decisions. The development plan in `design.md` (Phases 1–5) is the execution roadmap — follow phase order, do not skip ahead.

## Development Plan (from `design.md`)

The project builds in 5 strict phases. Do not jump ahead — each phase depends on the previous.

| Phase | Scope | Status |
|-------|-------|--------|
| **Phase 1** | Baseplate kernel, CLI Interface Brick, Provider Brick, dummy tool, middleware hooks | Build first |
| **Phase 2** | CloakBrowser Tool Brick (stealth web autonomy via Playwright) | After Phase 1 |
| **Phase 3** | Tripartite memory: LCM (lossless context), MemPalace (spatial/temporal graph), LLM Wiki (persistent synthesis) | After Phase 2 |
| **Phase 4** | Kadeia ecosystem integration, Soul/Identity Bricks | After Phase 3 |
| **Phase 5** | Multi-head orchestration, Security/Logging/Improvement Bricks, infinite AFK loop | Final |

## Architecture Principles

- **Baseplate is the kernel**: Minimal event loop, state manager, Brick registry, middleware hooks. Knows nothing about LLMs, memory, or identity.
- **Bricks are modules**: Provider, Interface, Soul, Memory, Security, Context, Logging, Improvement, Tool. Each implements a defined ABC/interface.
- **Do not confuse bricks**: Interface Bricks are strictly for human-to-system or external-system-to-system communication (CLI, Web UI). Tool Bricks are strictly for the agent to take action on the environment (executing bash, searching files).
- **Middleware hook lifecycle**: pre-parse → pre-llm → post-llm → pre-tool → post-tool → post-tool-call. Bricks intercept at their hook stage.
- **Memory is auto-extracted**: The LLM should never manually "save a memory." Memory Bricks intercept the event bus and extract automatically.
- **Multi-head orchestration**: Multiple Soul Bricks can occupy the "Head" simultaneously, communicating over an internal event bus during AFK mode.

## Key Technical Decisions

- **Language**: Python 3.11+. The Baseplate and all core Bricks must be written in strict, type-hinted Python using asyncio for the event loop.
- **CloakBrowser**: Uses a patched Chromium binary for stealth web browsing (not just Playwright/Puppeteer plugins). Integration is via `AGENT_BROWSER_EXECUTABLE_PATH` env var.
- **MemPalace**: Uses ChromaDB (spatial vectors) + SQLite (temporal graph). All SQLite ops must be wrapped in try/finally to prevent connection leaks.
- **LLM Wiki**: Markdown directory treated as a codebase. Page caps: 400 lines soft, 800 hard. Directory shards at 150 pages.

## Gotchas

- This is a **greenfield project** — there is no code yet. `design.md` is the only spec.
- No build system, tests, or configs exist until Phase 1 creates them.
- The design references external projects (oh-my-openagent, lossless-claw, MemPalace, CloakBrowser, Karpathy LLM Wiki). Use them as inspiration, not drop-in copies.

## Important
- No lazy implementations. When writing or modifying files, you must output the complete, fully functional code. Do not use placeholders like // ... rest of code here.

