"""Tests for OpenAI OAuth (ChatGPT sign-in) — token lifecycle + provider wiring.

Everything network-facing uses a mocked transport. The interactive browser
login and a real OpenAI call cannot run here (no browser, no outbound to
OpenAI, no logged-in account) — those are verified by the user on a real
machine. What IS verified here: PKCE, the env-supplied client id (no borrowed
OAuth app), expiry math, the refresh grant, brikie's own token store, and the
provider's dynamic/refreshing bearer.
"""

from __future__ import annotations

import base64
import json
import time

import pytest

from brikie.auth import openai_oauth as oa

_CID = "BRIKIE_OPENAI_OAUTH_CLIENT_ID"


def _jwt(claims: dict) -> str:
    """A minimal unsigned JWT (header.payload.sig) for claim-decode tests."""
    def seg(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{seg({'alg': 'none'})}.{seg(claims)}.sig"


class FakeResp:
    def __init__(self, status: int, body: dict) -> None:
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self) -> dict:
        return self._body


class FakeHTTP:
    """A stand-in httpx.AsyncClient capturing the last POST."""

    def __init__(self, resp: FakeResp) -> None:
        self._resp = resp
        self.posted: dict = {}

    async def post(self, url, data=None, json=None, headers=None):
        self.posted = {"url": url, "data": data, "json": json, "headers": headers}
        return self._resp

    async def aclose(self):
        pass


class TestClientId:
    def test_bundled_default_when_env_unset(self, monkeypatch):
        # Zero-config: a working client ships by default (no setup required).
        monkeypatch.delenv(_CID, raising=False)
        assert oa.client_id() == oa._DEFAULT_CLIENT_ID
        assert oa.client_id()                       # non-empty

    def test_env_overrides_default(self, monkeypatch):
        monkeypatch.setenv(_CID, "brikie_app_123")
        assert oa.client_id() == "brikie_app_123"


class TestCallbackParsing:
    def test_parses_full_redirect_url(self):
        # The exact shape a real ChatGPT sign-in redirects to.
        url = ("http://localhost:1455/auth/callback?code=ac_ABC123.xyz"
               "&scope=openid+profile+email+offline_access&state=STATE99")
        code, state = oa.parse_callback(url)
        assert code == "ac_ABC123.xyz"
        assert state == "STATE99"

    def test_parses_bare_query_and_quotes(self):
        code, state = oa.parse_callback("'code=c1&state=s1'")
        assert code == "c1" and state == "s1"

    def test_error_in_callback_raises(self):
        with pytest.raises(oa.OAuthError):
            oa.parse_callback("http://localhost:1455/auth/callback?error=access_denied")


class TestPKCE:
    def test_verifier_and_challenge_are_valid_s256(self):
        import hashlib
        verifier, challenge = oa.generate_pkce()
        assert 43 <= len(verifier) <= 128
        expect = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        assert challenge == expect
        assert "=" not in challenge          # url-safe, unpadded

    def test_authorize_url_carries_pkce_state_and_env_client(self, monkeypatch):
        monkeypatch.setenv(_CID, "brikie_app_123")
        url = oa.build_authorize_url("CHAL", "STATE")
        assert oa.AUTHORIZE_URL in url
        assert "code_challenge=CHAL" in url
        assert "code_challenge_method=S256" in url
        assert "state=STATE" in url
        assert "client_id=brikie_app_123" in url


class TestExpiry:
    def test_expiry_decoded_from_jwt(self):
        exp = int(time.time()) + 5000
        at = _jwt({"exp": exp})
        assert abs(oa._expiry_from_tokens(at, "") - exp) < 1

    def test_unknown_expiry_falls_back_to_ttl(self):
        got = oa._expiry_from_tokens("not-a-jwt", "")
        assert got > time.time()             # a sane future fallback

    def test_is_expired_respects_skew(self):
        t = oa.OAuthTokens(access_token="x", expires_at=time.time() + 30)
        assert t.is_expired(skew=120) is True       # within skew → refresh
        assert t.is_expired(skew=0) is False
        # Unknown expiry never reports expired (let a 401 drive refresh).
        assert oa.OAuthTokens(access_token="x", expires_at=0).is_expired() is False


class TestStore:
    def test_load_from_brikie_store(self, tmp_path):
        store = tmp_path / "store.json"
        store.write_text(json.dumps({
            "access_token": "AT", "refresh_token": "RT",
            "account_id": "acc", "expires_at": time.time() + 9000,
        }))
        tokens = oa.load_tokens(brikie_path=store)
        assert tokens.access_token == "AT"
        assert tokens.refresh_token == "RT"
        assert tokens.account_id == "acc"

    def test_no_creds_returns_none(self, tmp_path):
        assert oa.load_tokens(tmp_path / "absent.json") is None

    def test_save_then_load_roundtrip(self, tmp_path, monkeypatch):
        store = tmp_path / "store.json"
        monkeypatch.setattr(oa, "BRIKIE_AUTH_PATH", store)
        oa.save_tokens(oa.OAuthTokens(access_token="A", refresh_token="R"))
        assert (store.stat().st_mode & 0o777) == 0o600   # owner-only
        assert oa.load_tokens().access_token == "A"


class TestRefresh:
    async def test_refresh_uses_grant_and_persists(self, tmp_path, monkeypatch):
        monkeypatch.setattr(oa, "BRIKIE_AUTH_PATH", tmp_path / "store.json")
        monkeypatch.setenv(_CID, "brikie_app_123")
        old = oa.OAuthTokens(access_token="old", refresh_token="rt_keep",
                             account_id="acc", expires_at=1)
        http = FakeHTTP(FakeResp(200, {
            "access_token": "new_access", "expires_in": 3600,
            # note: no refresh_token in response → must keep the old one
        }))
        new = await oa.refresh_tokens(old, client=http)
        assert http.posted["data"]["grant_type"] == "refresh_token"
        assert http.posted["data"]["client_id"] == "brikie_app_123"
        assert http.posted["data"]["refresh_token"] == "rt_keep"
        assert new.access_token == "new_access"
        assert new.refresh_token == "rt_keep"            # carried forward
        assert new.expires_at > time.time()
        saved = json.loads((tmp_path / "store.json").read_text())
        assert saved["access_token"] == "new_access"

    async def test_refresh_without_token_is_actionable_error(self):
        with pytest.raises(oa.OAuthError):
            await oa.refresh_tokens(oa.OAuthTokens(access_token="x"))

    async def test_token_endpoint_error_raises(self, monkeypatch):
        monkeypatch.setenv(_CID, "brikie_app_123")
        http = FakeHTTP(FakeResp(400, {"error": "invalid_grant"}))
        with pytest.raises(oa.OAuthError):
            await oa.refresh_tokens(
                oa.OAuthTokens(access_token="x", refresh_token="r"), client=http)


class TestSource:
    async def test_source_headers_include_bearer_and_account(self, tmp_path, monkeypatch):
        store = tmp_path / "store.json"
        store.write_text(json.dumps({
            "access_token": "AT", "refresh_token": "r",
            "account_id": "acc_1", "expires_at": time.time() + 9000,
        }))
        monkeypatch.setattr(oa, "BRIKIE_AUTH_PATH", store)
        src = oa.OpenAIOAuthSource()
        headers = await src.headers()
        assert headers["Authorization"] == "Bearer AT"
        assert headers[oa.ACCOUNT_ID_HEADER] == "acc_1"

    async def test_missing_login_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(oa, "BRIKIE_AUTH_PATH", tmp_path / "none.json")
        with pytest.raises(oa.OAuthError):
            await oa.OpenAIOAuthSource().headers()


class TestOnboardingOneClick:
    def test_login_launched_when_not_signed_in(self, monkeypatch):
        import brikie.onboard as ob
        from rich.console import Console
        called = {"flow": 0}

        async def fake_flow():
            called["flow"] += 1
        monkeypatch.setattr(oa, "run_login_flow", fake_flow)
        monkeypatch.setattr(oa, "load_tokens", lambda: None)
        ob._do_oauth_login(Console())
        assert called["flow"] == 1                 # picking the provider = sign in

    def test_login_skipped_when_already_signed_in(self, monkeypatch):
        import brikie.onboard as ob
        from rich.console import Console
        called = {"flow": 0}

        async def fake_flow():
            called["flow"] += 1
        monkeypatch.setattr(oa, "run_login_flow", fake_flow)
        monkeypatch.setattr(oa, "load_tokens",
                            lambda: oa.OAuthTokens(access_token="x"))
        ob._do_oauth_login(Console())
        assert called["flow"] == 0                 # no second login

    def test_oauth_preset_selects_oauth_login(self):
        from brikie.config.provider_presets import PRESETS
        assert PRESETS["openai-oauth"].auth == "oauth"   # onboarding branches on this


class TestProviderIntegration:
    def test_oauth_mode_detected(self):
        from brikie.bricks.provider.http_provider import HTTPProvider
        p = HTTPProvider(api_key="oauth:openai", api_format="openai")
        assert p._is_oauth() is True
        assert HTTPProvider(api_key="sk-abc")._is_oauth() is False

    def test_preset_emits_oauth_marker(self):
        from brikie.config.provider_presets import PRESETS, preset_config
        cfg = preset_config(PRESETS["openai-oauth"])
        assert cfg["api_key"] == "oauth:openai"
        assert cfg["api_format"] == "openai"

    async def test_provider_injects_and_refreshes_bearer_on_401(self):
        # First call 401s; the provider force-refreshes the OAuth header and
        # the retry succeeds — without ever using a static key.
        import httpx

        from brikie.bricks.provider.http_provider import HTTPProvider

        class FakeSource:
            def __init__(self):
                self.token = "stale"
                self.refreshed = False

            async def headers(self, force_refresh=False):
                if force_refresh:
                    self.refreshed = True
                    self.token = "fresh"
                return {"Authorization": f"Bearer {self.token}"}

        seen = []

        class FakeClient:
            async def post(self, path, json=None, headers=None):
                seen.append(headers["Authorization"])
                req = httpx.Request("POST", "http://x" + path)
                if headers["Authorization"] == "Bearer stale":
                    resp = httpx.Response(401, request=req)
                    raise httpx.HTTPStatusError("401", request=req, response=resp)
                return httpx.Response(200, json={"ok": True}, request=req)

        p = HTTPProvider(api_key="oauth:openai")
        p._client = FakeClient()
        p._oauth_source = FakeSource()
        resp = await p._post("/chat/completions", {"x": 1})
        assert resp.status_code == 200
        assert p._oauth_source.refreshed is True
        assert seen == ["Bearer stale", "Bearer fresh"]   # refreshed + retried
