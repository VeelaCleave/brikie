"""Tests for Dreamer steering: operator focus, Dream Sources, GitHubBrick,
and proposal provenance."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from brikie.bricks.logging.diagnostics import DiagnosticsCollectorBrick
from brikie.bricks.soul.dreamer import Dreamer
from brikie.bricks.tool.github_tools import GitHubBrick
from brikie.kernel.afk_protocol import AFKProtocolEngine, Proposal
from brikie.kernel.event_loop import EventLoop
from brikie.kernel.hooks import HookDispatcher
from brikie.kernel.registry import BrickRegistry
from brikie.kernel.soul_actor import DreamerActor
from brikie.kernel.state import StateManager


class _FakeBus:
    async def publish(self, event):  # pragma: no cover - not exercised
        pass

    async def consume(self, queue):  # pragma: no cover - not exercised
        raise NotImplementedError


class _FakeSource:
    """Minimal dream source."""

    def __init__(self, name: str, text: str, fail: bool = False) -> None:
        self.name = name
        self._text = text
        self._fail = fail

    async def dream_context(self) -> str:
        if self._fail:
            raise RuntimeError("source exploded")
        return self._text


def _engine(**kwargs) -> AFKProtocolEngine:
    return AFKProtocolEngine(event_bus=_FakeBus(), **kwargs)


# ──────────────────────────────────────────────────────────────────────
# Operator focus
# ──────────────────────────────────────────────────────────────────────


class TestOperatorFocus:
    def test_dreamer_soul_has_focus_field(self):
        assert Dreamer().focus == ""
        assert Dreamer(focus="registry UX").focus == "registry UX"

    async def test_focus_leads_the_dream_context(self):
        engine = _engine(get_focus=lambda: "improve memory recall")
        context = await engine._build_dream_context()
        assert context.startswith("OPERATOR FOCUS: improve memory recall")
        assert "operator-focus" in context

    async def test_async_get_focus_supported(self):
        async def focus():
            return "async focus"

        engine = _engine(get_focus=focus)
        assert "async focus" in await engine._build_dream_context()

    async def test_no_focus_no_sources_is_honest(self):
        engine = _engine()
        context = await engine._build_dream_context()
        assert "No dream sources" in context

    async def test_focus_command_sets_state(self):
        state = StateManager()
        loop = EventLoop(
            registry=BrickRegistry(), state=state, hooks=HookDispatcher()
        )
        handled = await loop._handle_command("/focus Fix The Registry UX")
        assert handled is True
        # Casing of the directive is preserved (only the command word is
        # matched lowercase).
        assert await state.get("dreamer_focus") == "Fix The Registry UX"

        await loop._handle_command("/focus clear")
        assert await state.get("dreamer_focus") == ""


# ──────────────────────────────────────────────────────────────────────
# Dream Sources
# ──────────────────────────────────────────────────────────────────────


class TestDreamSources:
    async def test_sources_contribute_named_sections(self):
        sources = [
            _FakeSource("diagnostics", "5 LLM calls."),
            _FakeSource("github", "github#7: add dark mode"),
        ]
        engine = _engine(dream_sources=lambda: sources)
        context = await engine._build_dream_context()
        assert "[source: diagnostics]\n5 LLM calls." in context
        assert "[source: github]\ngithub#7: add dark mode" in context

    async def test_failing_source_degrades_not_crashes(self):
        engine = _engine(
            dream_sources=lambda: [_FakeSource("boom", "", fail=True)]
        )
        context = await engine._build_dream_context()
        assert "failed to report" in context

    async def test_empty_source_text_is_skipped(self):
        engine = _engine(dream_sources=lambda: [_FakeSource("quiet", "  ")])
        context = await engine._build_dream_context()
        assert "[source: quiet]" not in context

    async def test_legacy_diagnostics_param_still_works(self):
        diagnostics = DiagnosticsCollectorBrick()
        engine = _engine(diagnostics=diagnostics)
        context = await engine._build_dream_context()
        assert "[source: " in context and "Session stats" in context

    async def test_diagnostics_brick_dream_context(self):
        brick = DiagnosticsCollectorBrick()
        text = await brick.dream_context()
        assert "Session stats:" in text


# ──────────────────────────────────────────────────────────────────────
# Proposal provenance
# ──────────────────────────────────────────────────────────────────────


class _CannedProvider:
    def __init__(self, payload: str) -> None:
        self._payload = payload

    async def get_completion(self, messages, tools):
        return self._payload, [], {}


class TestProposalProvenance:
    def test_source_defaults_to_dreamer(self):
        assert Proposal(title="x").source == "dreamer"

    async def test_dreamer_actor_parses_source(self):
        provider = _CannedProvider(
            '[{"title": "Fix dark mode", "description": "see issue", '
            '"impact": "medium", "complexity": "low", "source": "github#7"},'
            ' {"title": "Tidy logs", "description": "d"}]'
        )
        actor = DreamerActor(Dreamer(), provider)
        proposals = await actor.propose("ctx", 5)
        assert proposals[0].source == "github#7"
        assert proposals[1].source == "dreamer"


# ──────────────────────────────────────────────────────────────────────
# GitHubBrick
# ──────────────────────────────────────────────────────────────────────


_ISSUE_PAYLOAD: List[Dict[str, Any]] = [
    {
        "number": 7,
        "title": "Add dark mode",
        "state": "open",
        "labels": [{"name": "dreamer-approved"}],
        "comments": 2,
        "html_url": "https://github.com/o/r/issues/7",
        "body": "Please add dark mode to the picker.",
    },
    {
        "number": 8,
        "title": "Some PR",
        "state": "open",
        "labels": [],
        "comments": 0,
        "html_url": "https://github.com/o/r/pull/8",
        "body": "PR body",
        "pull_request": {"url": "..."},
    },
]


class TestGitHubBrick:
    def test_tool_schemas(self):
        names = [t["function"]["name"] for t in GitHubBrick.tools]
        assert names == ["github_list_issues", "github_get_issue"]

    async def test_requires_repo(self):
        brick = GitHubBrick()
        with pytest.raises(ValueError, match="No repository configured"):
            await brick.execute("github_list_issues", {})

    async def test_list_issues_filters_pull_requests(self, monkeypatch):
        brick = GitHubBrick(repo="o/r")
        captured: Dict[str, Any] = {}

        async def fake_get_json(path, params=None):
            captured["path"], captured["params"] = path, params
            return _ISSUE_PAYLOAD

        monkeypatch.setattr(brick, "_get_json", fake_get_json)
        issues = await brick.execute("github_list_issues", {"limit": 5})
        assert captured["path"] == "/repos/o/r/issues"
        assert [i["number"] for i in issues] == [7]  # the PR is dropped
        assert issues[0]["excerpt"].startswith("Please add dark mode")

    async def test_get_issue_shape(self, monkeypatch):
        brick = GitHubBrick(repo="o/r")

        async def fake_get_json(path, params=None):
            return {**_ISSUE_PAYLOAD[0], "user": {"login": "veela"}}

        monkeypatch.setattr(brick, "_get_json", fake_get_json)
        issue = await brick.execute("github_get_issue", {"number": 7})
        assert issue["author"] == "veela"
        assert issue["labels"] == ["dreamer-approved"]

    async def test_dream_context_silent_without_repo(self):
        assert await GitHubBrick().dream_context() == ""

    async def test_dream_context_is_label_gated_and_framed(self, monkeypatch):
        brick = GitHubBrick(repo="o/r")
        captured: Dict[str, Any] = {}

        async def fake_get_json(path, params=None):
            captured["params"] = params
            return _ISSUE_PAYLOAD

        monkeypatch.setattr(brick, "_get_json", fake_get_json)
        context = await brick.dream_context()
        assert captured["params"]["labels"] == "dreamer-approved"
        assert "UNTRUSTED" in context
        assert "github#7: Add dark mode" in context

    async def test_dream_context_degrades_on_api_failure(self, monkeypatch):
        brick = GitHubBrick(repo="o/r")

        async def fake_get_json(path, params=None):
            raise RuntimeError("GitHub returned HTTP 500")

        monkeypatch.setattr(brick, "_get_json", fake_get_json)
        context = await brick.dream_context()
        assert "unavailable" in context

    def test_token_env_reference(self, monkeypatch):
        monkeypatch.setenv("TEST_GH_TOKEN", "ghp_abc")
        brick = GitHubBrick(repo="o/r", token="env:TEST_GH_TOKEN")
        assert brick._headers()["Authorization"] == "Bearer ghp_abc"

    def test_no_token_is_anonymous(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        brick = GitHubBrick(repo="o/r")
        assert "Authorization" not in brick._headers()
