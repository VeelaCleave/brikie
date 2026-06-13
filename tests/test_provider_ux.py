"""Tests for the provider-usability layer: presets, env-key resolution,
friendly connection errors, and the first-run onboarding logic."""

from __future__ import annotations

import argparse
import json

import httpx
import pytest

from brikie.bricks.provider.http_provider import HTTPProvider, ProviderConnectionError
from brikie.config.provider_presets import (
    PRESETS,
    _extract_model_ids,
    detect_env_keys,
    detect_local_servers,
    preset_config,
)
from brikie import onboard


# ──────────────────────────────────────────────────────────────────────
# Presets
# ──────────────────────────────────────────────────────────────────────


class TestPresets:
    def test_every_preset_is_complete(self):
        for preset in PRESETS.values():
            assert preset.base_url.startswith("http")
            assert preset.api_format in ("openai", "claude")
            assert preset.label and preset.blurb
            # Exactly one auth mechanism: a key env var (hosted static key),
            # a probe URL (local, no key), or OAuth (hosted ChatGPT sign-in).
            mechanisms = [
                preset.key_env is not None,
                preset.probe_url is not None,
                preset.auth == "oauth",
            ]
            assert sum(mechanisms) == 1, f"{preset.name}: need exactly one auth mechanism"

    def test_expected_presets_exist(self):
        assert {"anthropic", "openai", "openrouter", "groq",
                "ollama", "lmstudio", "vllm"} <= set(PRESETS)

    def test_preset_config_uses_env_reference_for_hosted(self):
        config = preset_config(PRESETS["anthropic"])
        assert config["api_key"] == "env:ANTHROPIC_API_KEY"
        assert config["api_format"] == "claude"

    def test_preset_config_local_needs_no_key(self):
        config = preset_config(PRESETS["ollama"], model="qwen3")
        assert config["api_key"] == "not-needed"
        assert config["model"] == "qwen3"

    def test_detect_env_keys(self, monkeypatch):
        for preset in PRESETS.values():
            if preset.key_env:
                monkeypatch.delenv(preset.key_env, raising=False)
        monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
        assert detect_env_keys() == ["groq"]

    def test_detect_local_servers(self, monkeypatch):
        class FakeResponse:
            def raise_for_status(self):
                pass
            def json(self):
                return {"models": [{"name": "llama3.2:latest"}]}

        def fake_get(url, timeout):
            if "11434" in url:
                return FakeResponse()
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(httpx, "get", fake_get)
        found = detect_local_servers()
        assert found == {"ollama": ["llama3.2:latest"]}

    def test_extract_model_ids_both_shapes(self):
        assert _extract_model_ids({"models": [{"name": "a"}]}) == ["a"]
        assert _extract_model_ids({"data": [{"id": "b"}]}) == ["b"]
        assert _extract_model_ids("garbage") == []


# ──────────────────────────────────────────────────────────────────────
# HTTPProvider — env keys + friendly errors
# ──────────────────────────────────────────────────────────────────────


class TestEnvKeyResolution:
    async def test_env_reference_resolved_at_init(self, monkeypatch):
        monkeypatch.setenv("TEST_BRIKIE_KEY", "sk-real-key")
        provider = HTTPProvider(api_key="env:TEST_BRIKIE_KEY")
        await provider.init()
        try:
            assert provider._client.headers["Authorization"] == "Bearer sk-real-key"
        finally:
            await provider.shutdown()

    async def test_literal_key_passes_through(self):
        provider = HTTPProvider(api_key="sk-literal")
        await provider.init()
        try:
            assert provider._client.headers["Authorization"] == "Bearer sk-literal"
        finally:
            await provider.shutdown()

    def test_missing_env_var_resolves_empty(self, monkeypatch):
        monkeypatch.delenv("TEST_BRIKIE_MISSING", raising=False)
        provider = HTTPProvider(api_key="env:TEST_BRIKIE_MISSING")
        assert provider._resolve_api_key() == ""


class TestFriendlyErrors:
    async def test_unreachable_server_has_human_message(self):
        provider = HTTPProvider(
            model="some-model", base_url="http://127.0.0.1:1/v1", timeout=2.0
        )
        await provider.init()
        try:
            with pytest.raises(ProviderConnectionError, match="is it running"):
                await provider.get_completion([{"role": "user", "content": "hi"}], [])
        finally:
            await provider.shutdown()

    @pytest.mark.parametrize("status,needle", [
        (401, "API key was rejected"),
        (404, "may not exist on this server"),
        (429, "rate-limited"),
        (500, "the server said"),
    ])
    async def test_http_errors_translated(self, status, needle):
        provider = HTTPProvider(model="m", base_url="http://test/v1")
        await provider.init()

        def handler(request):
            return httpx.Response(status, text="boom")

        # Swap the transport for a canned-response one — no real network.
        await provider._client.aclose()
        provider._client = httpx.AsyncClient(
            base_url="http://test/v1", transport=httpx.MockTransport(handler)
        )
        try:
            with pytest.raises(ProviderConnectionError, match=needle):
                await provider.get_completion([{"role": "user", "content": "hi"}], [])
        finally:
            await provider.shutdown()


# ──────────────────────────────────────────────────────────────────────
# Onboarding triggers + set writing
# ──────────────────────────────────────────────────────────────────────


def _args(**overrides) -> argparse.Namespace:
    base = {"set": "default", "model": None, "base_url": None,
            "api_key": None, "onboard": False}
    base.update(overrides)
    return argparse.Namespace(**base)


class TestShouldOnboard:
    @pytest.fixture(autouse=True)
    def interactive_no_marker(self, monkeypatch, tmp_path):
        monkeypatch.setattr(onboard, "MARKER", tmp_path / "onboarded")
        monkeypatch.setattr(onboard.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(onboard.sys.stdout, "isatty", lambda: True)

    def test_first_interactive_default_run_onboards(self):
        assert onboard.should_onboard(_args()) is True

    def test_marker_suppresses(self, tmp_path):
        (tmp_path / "onboarded").write_text("done")
        assert onboard.should_onboard(_args()) is False

    def test_non_default_set_skips(self):
        assert onboard.should_onboard(_args(set="full")) is False

    def test_cli_overrides_skip(self):
        assert onboard.should_onboard(_args(model="x")) is False
        assert onboard.should_onboard(_args(base_url="http://x")) is False

    def test_piped_stdin_skips(self, monkeypatch):
        monkeypatch.setattr(onboard.sys.stdin, "isatty", lambda: False)
        assert onboard.should_onboard(_args()) is False

    def test_onboard_flag_forces(self, tmp_path):
        (tmp_path / "onboarded").write_text("done")
        assert onboard.should_onboard(_args(onboard=True, set="full")) is True


class TestWriteDefaultSet:
    def test_replaces_provider_config(self, tmp_path):
        sets_dir = tmp_path
        (sets_dir / "default.json").write_text(json.dumps({
            "name": "default",
            "bricks": [
                {"brk": "BRK-200", "config": {"model": "old"}},
                {"brk": "BRK-300"},
            ],
        }))
        onboard._write_default_set(sets_dir, {"model": "new", "base_url": "u",
                                              "api_format": "openai",
                                              "api_key": "env:X"})
        data = json.loads((sets_dir / "default.json").read_text())
        assert data["bricks"][0]["config"]["model"] == "new"
        assert data["bricks"][1] == {"brk": "BRK-300"}

    def test_inserts_provider_when_absent(self, tmp_path):
        (tmp_path / "default.json").write_text(json.dumps({
            "name": "default", "bricks": [{"brk": "BRK-300"}],
        }))
        onboard._write_default_set(tmp_path, {"model": "m", "base_url": "u",
                                              "api_format": "openai",
                                              "api_key": "k"})
        data = json.loads((tmp_path / "default.json").read_text())
        assert data["bricks"][0]["brk"] == "BRK-200"


class TestEnvRefWithFallback:
    def test_fallback_used_when_var_unset(self, monkeypatch):
        monkeypatch.delenv("TEST_BASE_OVERRIDE", raising=False)
        assert HTTPProvider._resolve_ref(
            "env:TEST_BASE_OVERRIDE|https://api.example.com"
        ) == "https://api.example.com"

    def test_env_wins_when_set(self, monkeypatch):
        monkeypatch.setenv("TEST_BASE_OVERRIDE", "http://reroute:9999/v1")
        assert HTTPProvider._resolve_ref(
            "env:TEST_BASE_OVERRIDE|https://api.example.com"
        ) == "http://reroute:9999/v1"

    async def test_base_url_resolved_at_init(self, monkeypatch):
        monkeypatch.setenv("TEST_BASE_OVERRIDE", "http://managed:1234/v1")
        provider = HTTPProvider(
            base_url="env:TEST_BASE_OVERRIDE|https://api.example.com"
        )
        await provider.init()
        try:
            assert provider.base_url == "http://managed:1234/v1"
        finally:
            await provider.shutdown()
