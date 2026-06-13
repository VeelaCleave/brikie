"""Provider presets — named connection recipes for HTTPProvider (BRK-200).

A preset bundles everything a non-expert needs to connect a model:
base URL, wire format, a sensible default model, and which environment
variable usually holds the API key. Presets are **installer/config-layer
data only**: they feed Build Set generation (the install wizard, the
brikie.co picker, ``brikie/install.py``) and are never read by the
kernel or by HTTPProvider at runtime — the Build Set JSON remains the
single runtime source of provider configuration (architecture rule 3).

API keys are referenced as ``env:VAR_NAME`` wherever possible so keys
never land in Build Set files or URLs; HTTPProvider resolves the
reference from the environment at init().
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

import httpx


@dataclass(frozen=True)
class ProviderPreset:
    """One named provider recipe.

    Attributes:
        name: Preset id (e.g. "anthropic").
        label: Human-friendly name for pickers.
        base_url: API base URL.
        api_format: "openai" or "claude" (HTTPProvider wire format).
        default_model: Model to suggest; the user can always override.
        key_env: Environment variable that conventionally holds the API
            key, or None when the endpoint needs no key (local servers).
        probe_url: URL that answers when a *local* server is running, or
            None for hosted APIs.
        blurb: One-liner for pickers.
    """

    name: str
    label: str
    base_url: str
    api_format: str
    default_model: str
    key_env: Optional[str]
    probe_url: Optional[str]
    blurb: str
    base_url_env: Optional[str] = None  # runtime base-URL override env var
    auth: Optional[str] = None  # "oauth" ⇒ ChatGPT sign-in, not a static key


PRESETS: dict[str, ProviderPreset] = {
    p.name: p for p in [
        ProviderPreset(
            name="anthropic", label="Anthropic (Claude)",
            base_url="https://api.anthropic.com",
            api_format="claude", default_model="claude-sonnet-4-6",
            key_env="ANTHROPIC_API_KEY", probe_url=None,
            blurb="Claude models — needs an Anthropic API key",
            base_url_env="ANTHROPIC_BASE_URL",
        ),
        ProviderPreset(
            name="openai", label="OpenAI",
            base_url="https://api.openai.com/v1",
            api_format="openai", default_model="gpt-4o",
            key_env="OPENAI_API_KEY", probe_url=None,
            blurb="GPT models — needs an OpenAI API key",
            base_url_env="OPENAI_BASE_URL",
        ),
        ProviderPreset(
            name="openai-oauth", label="OpenAI (ChatGPT login)",
            base_url="https://api.openai.com/v1",
            api_format="openai", default_model="gpt-5.5",
            key_env=None, probe_url=None,
            blurb="Sign in with your ChatGPT account — run `brikie login openai`",
            base_url_env="OPENAI_BASE_URL", auth="oauth",
        ),
        ProviderPreset(
            name="openrouter", label="OpenRouter",
            base_url="https://openrouter.ai/api/v1",
            api_format="openai", default_model="openrouter/auto",
            key_env="OPENROUTER_API_KEY", probe_url=None,
            blurb="Hundreds of models behind one key",
        ),
        ProviderPreset(
            name="groq", label="Groq",
            base_url="https://api.groq.com/openai/v1",
            api_format="openai", default_model="llama-3.3-70b-versatile",
            key_env="GROQ_API_KEY", probe_url=None,
            blurb="Very fast open models — needs a Groq API key",
        ),
        ProviderPreset(
            name="ollama", label="Ollama (local)",
            base_url="http://localhost:11434/v1",
            api_format="openai", default_model="llama3.2",
            key_env=None, probe_url="http://localhost:11434/api/tags",
            blurb="Local models via Ollama — no API key",
        ),
        ProviderPreset(
            name="lmstudio", label="LM Studio (local)",
            base_url="http://localhost:1234/v1",
            api_format="openai", default_model="local-model",
            key_env=None, probe_url="http://localhost:1234/v1/models",
            blurb="Local models via LM Studio — no API key",
        ),
        ProviderPreset(
            name="vllm", label="vLLM / custom (local)",
            base_url="http://localhost:8000/v1",
            api_format="openai", default_model="",
            key_env=None, probe_url="http://localhost:8000/v1/models",
            blurb="Any OpenAI-compatible server you run yourself",
        ),
    ]
}


def preset_config(preset: ProviderPreset, model: str | None = None) -> dict[str, str]:
    """Build a BRK-200 config dict from a preset.

    The API key is written as an ``env:`` reference (never the key
    itself) for hosted providers, or the literal "not-needed" for local
    servers.

    Args:
        preset: The provider preset.
        model: Optional model override; falls back to the preset default.
    """
    base_url = preset.base_url
    if preset.base_url_env:
        # Resolved by HTTPProvider at init() — sandboxed/managed runtimes
        # (e.g. OpenShell) reroute inference by setting this variable.
        base_url = f"env:{preset.base_url_env}|{preset.base_url}"
    if preset.auth == "oauth":
        api_key = "oauth:openai"      # dynamic refreshable bearer, not a key
    elif preset.key_env:
        api_key = f"env:{preset.key_env}"
    else:
        api_key = "not-needed"
    return {
        "model": model or preset.default_model,
        "base_url": base_url,
        "api_format": preset.api_format,
        "api_key": api_key,
    }


# ──────────────────────────────────────────────────────────────────────
# Detection — what does this machine already have?
# ──────────────────────────────────────────────────────────────────────


def detect_env_keys() -> list[str]:
    """Preset names whose conventional API-key env var is set and non-empty."""
    return [
        p.name for p in PRESETS.values()
        if p.key_env and os.environ.get(p.key_env, "").strip()
    ]


def detect_local_servers(timeout: float = 0.6) -> dict[str, list[str]]:
    """Probe known local server ports; return preset name → model ids.

    A preset appears in the result only when its probe URL answered.
    The model list may be empty when the server is up but reports no
    models (or the response shape is unrecognized).
    """
    found: dict[str, list[str]] = {}
    for preset in PRESETS.values():
        if not preset.probe_url:
            continue
        try:
            response = httpx.get(preset.probe_url, timeout=timeout)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError):
            continue
        found[preset.name] = _extract_model_ids(data)
    return found


def _extract_model_ids(data: object) -> list[str]:
    """Pull model ids out of an Ollama /api/tags or OpenAI /models payload."""
    models: list[str] = []
    if isinstance(data, dict):
        for item in data.get("models") or []:      # Ollama: {"models": [{"name": ...}]}
            if isinstance(item, dict) and item.get("name"):
                models.append(str(item["name"]))
        for item in data.get("data") or []:        # OpenAI: {"data": [{"id": ...}]}
            if isinstance(item, dict) and item.get("id"):
                models.append(str(item["id"]))
    return models
