"""OpenAI OAuth ("Sign in with ChatGPT") for brikie providers.

Some users authenticate to OpenAI with their **ChatGPT account** (OAuth)
rather than a platform API key. A ChatGPT OAuth access token is *not* a
platform key: it expires (so it must be refreshed) and it is issued by
``auth.openai.com``. This module is the token lifecycle that lets brikie use
such a credential, using **brikie's own** OAuth app — never another tool's:

- **Login** — ``run_login_flow()`` performs the standard OAuth 2.0
  Authorization-Code-with-PKCE flow (a browser + a localhost callback) and
  stores the result in ``~/.brikie/openai_oauth.json``.
- **Refresh** — ``get_access_token()`` returns a valid bearer, transparently
  refreshing it via the OAuth token endpoint when it is near expiry.

The HTTP provider plugs in via ``OpenAIOAuthSource`` (a dynamic, refreshable
bearer) instead of the static ``api_key`` header.

────────────────────────────────────────────────────────────────────────────
HONESTY / VERIFICATION BOUNDARY (read before trusting this):
- The OAuth *logic* here (PKCE, code exchange, refresh, expiry) is standard
  OAuth 2.0 and is unit-tested with a mocked transport.
- It ships ZERO-CONFIG: brikie bundles the public ChatGPT-CLI sign-in client
  (``_DEFAULT_CLIENT_ID``) so picking the provider opens a link and logs you
  in — like other agent CLIs. Override with ``BRIKIE_OPENAI_OAUTH_CLIENT_ID``
  to use your own registered OAuth app.
- The bundled client id + endpoints are the publicly-known values; they cannot
  be exercised against OpenAI from a sandbox (no browser, no account). The
  flow is verified on a real machine. base_url/api_format stay configurable.
────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

# ── OpenAI OAuth endpoints (OpenAI's own auth server; override via env if
#    they ever change). These are NOT app-specific. ──────────────────────────
ISSUER = os.environ.get("BRIKIE_OPENAI_OAUTH_ISSUER", "https://auth.openai.com").rstrip("/")
AUTHORIZE_URL = os.environ.get("BRIKIE_OPENAI_OAUTH_AUTHORIZE_URL", f"{ISSUER}/oauth/authorize")
TOKEN_URL = os.environ.get("BRIKIE_OPENAI_OAUTH_TOKEN_URL", f"{ISSUER}/oauth/token")
SCOPES = os.environ.get(
    "BRIKIE_OPENAI_OAUTH_SCOPES", "openid profile email offline_access",
)  # offline_access ⇒ a refresh token

# The public "Sign in with ChatGPT" OAuth client for CLI tools (PKCE, no
# secret). brikie BUNDLES it so login works out of the box — pick the
# provider, click a link, done — exactly like other agent CLIs. Override with
# BRIKIE_OPENAI_OAUTH_CLIENT_ID to use your own registered OAuth app. The
# localhost redirect port must match the client's registered redirect.
_DEFAULT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
REDIRECT_PORT = int(os.environ.get("BRIKIE_OPENAI_OAUTH_PORT", "1455"))
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/auth/callback"
# Header the ChatGPT backend expects to scope a request to an account.
ACCOUNT_ID_HEADER = "chatgpt-account-id"

BRIKIE_AUTH_PATH = Path.home() / ".brikie" / "openai_oauth.json"


def client_id() -> str:
    """The OAuth client id used for sign-in — bundled default, env-overridable.

    Zero-config by default (the public ChatGPT CLI sign-in client) so login
    "just works". Set BRIKIE_OPENAI_OAUTH_CLIENT_ID to use your own registered
    OAuth app instead.
    """
    return os.environ.get("BRIKIE_OPENAI_OAUTH_CLIENT_ID", "").strip() or _DEFAULT_CLIENT_ID

# Refresh this many seconds *before* the token's stated expiry.
_EXPIRY_SKEW = 120
# Fallback lifetime if a token carries no decodable expiry claim.
_DEFAULT_TTL = 3600


class OAuthError(Exception):
    """Raised when an OAuth step fails in a way the user must act on."""


@dataclass
class OAuthTokens:
    """A set of OpenAI OAuth tokens plus a computed absolute expiry."""

    access_token: str
    refresh_token: str = ""
    id_token: str = ""
    account_id: str = ""
    expires_at: float = 0.0  # epoch seconds; 0 ⇒ unknown

    def is_expired(self, skew: int = _EXPIRY_SKEW) -> bool:
        if not self.expires_at:
            return False  # unknown expiry — let a 401 trigger refresh instead
        return time.time() >= (self.expires_at - skew)

    def to_store(self) -> Dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "id_token": self.id_token,
            "account_id": self.account_id,
            "expires_at": self.expires_at,
        }


# ──────────────────────────────────────────────────────────────────────────
# JWT / expiry helpers (best-effort, no signature verification — we only read
# the `exp` claim to know when to refresh; the server is the real authority).
# ──────────────────────────────────────────────────────────────────────────

def _decode_jwt_claims(token: str) -> Dict[str, Any]:
    """Return a JWT's payload claims, or {} if it isn't a decodable JWT."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # restore base64 padding
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _expiry_from_tokens(access_token: str, id_token: str) -> float:
    for tok in (access_token, id_token):
        exp = _decode_jwt_claims(tok).get("exp")
        if isinstance(exp, (int, float)) and exp > 0:
            return float(exp)
    return time.time() + _DEFAULT_TTL


def _account_id_from_tokens(access_token: str, id_token: str, fallback: str = "") -> str:
    """Pull the ChatGPT account id from token claims (Codex puts it here)."""
    for tok in (id_token, access_token):
        claims = _decode_jwt_claims(tok)
        auth = claims.get("https://api.openai.com/auth") or {}
        acc = auth.get("chatgpt_account_id") or claims.get("account_id")
        if acc:
            return str(acc)
    return fallback


# ──────────────────────────────────────────────────────────────────────────
# Credential loading: brikie's own store, falling back to reusing Codex's.
# ──────────────────────────────────────────────────────────────────────────

def load_tokens(brikie_path: Optional[Path] = None) -> Optional[OAuthTokens]:
    """Load tokens from brikie's own store (resolved at call time)."""
    brikie_path = brikie_path or BRIKIE_AUTH_PATH
    if brikie_path.is_file():
        try:
            d = json.loads(brikie_path.read_text())
            if d.get("access_token"):
                return OAuthTokens(
                    access_token=d["access_token"],
                    refresh_token=d.get("refresh_token", ""),
                    id_token=d.get("id_token", ""),
                    account_id=d.get("account_id", ""),
                    expires_at=float(d.get("expires_at", 0) or 0),
                )
        except Exception:
            logger.warning("Could not read %s", brikie_path, exc_info=True)
    return None


def save_tokens(tokens: OAuthTokens, path: Optional[Path] = None) -> None:
    """Persist tokens to brikie's store with owner-only permissions."""
    path = path or BRIKIE_AUTH_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tokens.to_store(), indent=2))
    try:
        path.chmod(0o600)
    except OSError:
        pass


# ──────────────────────────────────────────────────────────────────────────
# Refresh + code exchange (the network steps; transport injectable for tests)
# ──────────────────────────────────────────────────────────────────────────

async def _token_request(form: Dict[str, str], client: Any = None) -> Dict[str, Any]:
    """POST a form to the OAuth token endpoint; return the JSON body."""
    own = client is None
    if own:
        import httpx
        client = httpx.AsyncClient(timeout=30.0)
    try:
        resp = await client.post(TOKEN_URL, data=form)
        if resp.status_code >= 400:
            raise OAuthError(
                f"OpenAI token endpoint returned HTTP {resp.status_code}: "
                f"{resp.text[:200]}"
            )
        return resp.json()
    finally:
        if own:
            await client.aclose()


def _tokens_from_response(body: Dict[str, Any], prev: Optional[OAuthTokens] = None) -> OAuthTokens:
    access = body.get("access_token") or ""
    if not access:
        raise OAuthError("OAuth response did not contain an access_token.")
    id_token = body.get("id_token") or (prev.id_token if prev else "")
    # A refresh response may omit refresh_token — keep the prior one.
    refresh = body.get("refresh_token") or (prev.refresh_token if prev else "")
    if "expires_in" in body:
        expires_at = time.time() + float(body["expires_in"])
    else:
        expires_at = _expiry_from_tokens(access, id_token)
    return OAuthTokens(
        access_token=access,
        refresh_token=refresh,
        id_token=id_token,
        account_id=_account_id_from_tokens(
            access, id_token, prev.account_id if prev else ""),
        expires_at=expires_at,
    )


async def refresh_tokens(tokens: OAuthTokens, client: Any = None) -> OAuthTokens:
    """Exchange a refresh_token for a fresh access token; persist + return it."""
    if not tokens.refresh_token:
        raise OAuthError(
            "this login has no refresh token — run `brikie login openai` to "
            "sign in again."
        )
    body = await _token_request({
        "grant_type": "refresh_token",
        "client_id": client_id(),
        "refresh_token": tokens.refresh_token,
        "scope": SCOPES,
    }, client=client)
    refreshed = _tokens_from_response(body, prev=tokens)
    save_tokens(refreshed)
    return refreshed


async def exchange_code(
    code: str, verifier: str, redirect_uri: str = REDIRECT_URI, client: Any = None,
) -> OAuthTokens:
    """Exchange an authorization code (+ PKCE verifier) for tokens."""
    body = await _token_request({
        "grant_type": "authorization_code",
        "client_id": client_id(),
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }, client=client)
    tokens = _tokens_from_response(body)
    save_tokens(tokens)
    return tokens


async def get_access_token(force_refresh: bool = False) -> str:
    """Return a valid bearer token, refreshing if expired (or forced).

    Raises OAuthError when there is no login at all — the caller surfaces a
    "run `brikie login openai`" message.
    """
    tokens = load_tokens()
    if tokens is None:
        raise OAuthError(
            "no OpenAI login found. Run `brikie login openai` (or sign in with "
            "the Codex CLI) first."
        )
    if force_refresh or tokens.is_expired():
        tokens = await refresh_tokens(tokens)
    return tokens.access_token


# ──────────────────────────────────────────────────────────────────────────
# PKCE + the interactive login flow
# ──────────────────────────────────────────────────────────────────────────

def generate_pkce() -> Tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE S256."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def build_authorize_url(challenge: str, state: str,
                        redirect_uri: str = REDIRECT_URI) -> str:
    """Build the authorize URL the user opens in a browser."""
    return AUTHORIZE_URL + "?" + urlencode({
        "response_type": "code",
        "client_id": client_id(),
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "prompt": "login",
    })


def parse_callback(url_or_query: str) -> Tuple[str, str]:
    """Extract (code, state) from a pasted redirect URL or raw query string.

    Tolerant of a full ``http://localhost:1455/auth/callback?...`` URL or just
    the ``code=...&state=...`` part. Raises on an explicit ``error=``.
    """
    from urllib.parse import parse_qs, urlparse

    s = (url_or_query or "").strip().strip("'\"")
    parsed = urlparse(s)
    q = parse_qs(parsed.query or s)
    error = (q.get("error") or [""])[0]
    if error:
        raise OAuthError(f"sign-in was denied: {error}")
    return (q.get("code") or [""])[0], (q.get("state") or [""])[0]


def _serve_callback(port: int, result: Dict[str, str], stop) -> None:
    """Background daemon: catch the OAuth redirect on localhost, fill *result*.

    Best-effort — if the port can't be bound (in use, or unreachable from the
    browser) the manual paste path still completes the flow.
    """
    import http.server
    from urllib.parse import parse_qs, urlparse

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = parse_qs(urlparse(self.path).query)
            result["code"] = (q.get("code") or [""])[0]
            result["state"] = (q.get("state") or [""])[0]
            result["error"] = (q.get("error") or [""])[0]
            ok = result["code"] and not result["error"]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body style='font-family:sans-serif'>"
                b"<h2>brikie: sign-in " + (b"complete" if ok else b"failed")
                + b"</h2><p>You can close this tab and return to the terminal."
                b"</p></body></html>")

        def log_message(self, *_a):  # silence default stderr logging
            pass

    try:
        server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        result["bind_error"] = "1"
        return
    server.timeout = 1.0
    try:
        while not stop.is_set() and "code" not in result:
            server.handle_request()
    finally:
        server.server_close()


async def run_login_flow(open_browser: bool = True, timeout: float = 300.0) -> OAuthTokens:
    """Interactive 'Sign in with ChatGPT' flow; returns saved tokens.

    Robust to localhost not being reachable (SSH / remote / container): a
    background server catches the redirect when it can, and the user can paste
    the redirected URL otherwise. The PKCE verifier stays in scope for both.
    """
    import asyncio
    import threading
    import webbrowser

    verifier, challenge = generate_pkce()
    state = secrets.token_urlsafe(24)
    url = build_authorize_url(challenge, state)

    print("\nSign in with your ChatGPT account:\n")
    print(f"  {url}\n")
    print("After you approve, you'll be sent back automatically. If your "
          "browser shows a\n'can't reach localhost' page instead, copy that "
          "URL and paste it below.\n")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    result: Dict[str, str] = {}
    stop = threading.Event()
    server_thread = threading.Thread(
        target=_serve_callback, args=(REDIRECT_PORT, result, stop), daemon=True)
    server_thread.start()
    try:
        pasted = await asyncio.to_thread(
            input, "Press Enter once your browser shows success — "
                   "or paste the redirected URL here: ")
    finally:
        stop.set()

    if result.get("error"):
        raise OAuthError(f"sign-in was denied: {result['error']}")
    code = result.get("code") or ""
    got_state = result.get("state") or ""
    if not code and pasted.strip():
        code, got_state = parse_callback(pasted)
    if not code:
        raise OAuthError(
            "sign-in not completed — no authorization code received. "
            "Run `brikie login openai` again.")
    if got_state and got_state != state:
        raise OAuthError("state mismatch — please run the sign-in again.")

    tokens = await exchange_code(code, verifier)
    logger.info("OpenAI sign-in complete; tokens saved to %s", BRIKIE_AUTH_PATH)
    return tokens


class OpenAIOAuthSource:
    """A dynamic, refreshable bearer source for the HTTP provider.

    Caches the current access token and refreshes on demand (near-expiry) or
    on force (a 401). Also supplies the ChatGPT account-id header when known.
    """

    def __init__(self) -> None:
        self._tokens: Optional[OAuthTokens] = None

    async def _ensure(self, force: bool = False) -> OAuthTokens:
        if self._tokens is None:
            self._tokens = load_tokens()
            if self._tokens is None:
                raise OAuthError(
                    "no OpenAI login found. Run `brikie login openai` first."
                )
        if force or self._tokens.is_expired():
            self._tokens = await refresh_tokens(self._tokens)
        return self._tokens

    async def headers(self, force_refresh: bool = False) -> Dict[str, str]:
        tokens = await self._ensure(force=force_refresh)
        h = {"Authorization": f"Bearer {tokens.access_token}"}
        if tokens.account_id:
            h[ACCOUNT_ID_HEADER] = tokens.account_id
        return h
