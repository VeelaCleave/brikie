"""The brikie.co HTTP server — brick registry + installer generator.

Stdlib-only (``http.server``), so running a registry needs nothing beyond
brikie itself. Routes match exactly what ``RegistryClient`` expects:

    GET  /bricks/index.json                       all bricks (latest versions)
    GET  /bricks/search?q=...                     free-text search
    GET  /bricks/{name}/manifest.json             latest manifest
    GET  /bricks/{name}/{version}/manifest.json   pinned manifest
    GET  /bricks/{name}/{version}/source.py       brick source (download_url)
    POST /bricks/publish                          {"manifest": ..., "source_code": ...}

plus the Ninite-style installer generator:

    GET  /                                        brick-picker web page
    GET  /buildset.json?bricks=BRK-..,..&name=..  Build Set JSON
    GET  /install.sh?bricks=BRK-..,..&name=..     shell installer
"""

from __future__ import annotations

import hmac
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from brikie.server.store import RegistryStore, StoreError
from brikie.server.website import (
    GenerationError,
    generate_buildset,
    generate_install_sh,
    render_index_html,
)

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8321


class _RegistryHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer carrying the store, base URL, and publish token."""

    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        store: RegistryStore,
        base_url: str | None,
        publish_token: str | None,
    ) -> None:
        super().__init__(address, _Handler)
        self.store = store
        self.base_url = base_url
        self.publish_token = publish_token


class _Handler(BaseHTTPRequestHandler):
    """Request handler for the registry and installer-generator routes."""

    server: _RegistryHTTPServer  # narrowed for type checkers

    # ------------------------------------------------------------------
    # HTTP entry points
    # ------------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        parsed = urlparse(self.path)
        segments = [s for s in parsed.path.split("/") if s]
        query = parse_qs(parsed.query)
        try:
            self._route_get(segments, query)
        except StoreError as exc:
            self._send_json({"error": str(exc)}, status=exc.status)
        except GenerationError as exc:
            self._send_json({"error": str(exc)}, status=400)
        except Exception:
            logger.exception("Unhandled error for GET %s", self.path)
            self._send_json({"error": "internal server error"}, status=500)

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        parsed = urlparse(self.path)
        segments = [s for s in parsed.path.split("/") if s]
        try:
            if segments == ["bricks", "publish"]:
                self._handle_publish()
            else:
                self._send_json({"error": "not found"}, status=404)
        except StoreError as exc:
            self._send_json({"error": str(exc)}, status=exc.status)
        except Exception:
            logger.exception("Unhandled error for POST %s", self.path)
            self._send_json({"error": "internal server error"}, status=500)

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def _route_get(self, segments: list[str], query: dict[str, list[str]]) -> None:
        store = self.server.store

        if not segments or segments == ["index.html"]:
            html = render_index_html(store.list_manifests())
            self._send_body(html.encode("utf-8"), "text/html; charset=utf-8")
        elif segments == ["buildset.json"]:
            buildset = self._buildset_from_query(query)
            self._send_json(buildset)
        elif segments == ["install.sh"]:
            buildset = self._buildset_from_query(query)
            script = generate_install_sh(buildset)
            self._send_body(script.encode("utf-8"), "text/x-shellscript; charset=utf-8")
        elif segments == ["bricks", "index.json"]:
            self._send_json([self._absolutize(m) for m in store.list_manifests()])
        elif segments == ["bricks", "search"]:
            q = query.get("q", [""])[0]
            self._send_json([self._absolutize(m) for m in store.search(q)])
        elif len(segments) == 3 and segments[0] == "bricks" and segments[2] == "manifest.json":
            self._send_json(self._absolutize(store.get_manifest(segments[1])))
        elif len(segments) == 4 and segments[0] == "bricks" and segments[3] == "manifest.json":
            manifest = store.get_manifest(segments[1], segments[2])
            self._send_json(self._absolutize(manifest))
        elif len(segments) == 4 and segments[0] == "bricks" and segments[3] == "source.py":
            content = store.get_source(segments[1], segments[2])
            self._send_body(content, "text/x-python; charset=utf-8")
        else:
            self._send_json({"error": "not found"}, status=404)

    def _handle_publish(self) -> None:
        if not self._publish_authorized():
            self._send_json(
                {"error": "unauthorized — publishing requires a valid token "
                          "(Authorization: Bearer <token>)"},
                status=401,
            )
            return
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            raise StoreError("Empty request body")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise StoreError(f"Request body is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("manifest"), dict):
            raise StoreError("Body must be {'manifest': {...}, 'source_code': '...'}")

        stored = self.server.store.publish(
            payload["manifest"], payload.get("source_code", "")
        )
        logger.info("Published %s v%s", stored["name"], stored["version"])
        self._send_json(self._absolutize(stored), status=201)

    def _publish_authorized(self) -> bool:
        """True when no token is configured (dev mode) or the header matches."""
        token = self.server.publish_token
        if not token:
            return True
        auth = self.headers.get("Authorization", "")
        supplied = auth.removeprefix("Bearer ").strip()
        return hmac.compare_digest(supplied, token)

    @staticmethod
    def _buildset_from_query(query: dict[str, list[str]]) -> dict[str, Any]:
        raw = query.get("bricks", [""])[0]
        brks = [b.strip() for b in raw.split(",") if b.strip()]
        name = query.get("name", ["custom"])[0]
        return generate_buildset(brks, name)

    # ------------------------------------------------------------------
    # Response helpers
    # ------------------------------------------------------------------

    def _absolutize(self, manifest: dict[str, Any]) -> dict[str, Any]:
        """Prefix a stored relative download_url with this server's base URL."""
        url = manifest.get("download_url", "")
        if url.startswith("/"):
            base = self.server.base_url or f"http://{self.headers.get('Host', 'localhost')}"
            manifest = {**manifest, "download_url": base.rstrip("/") + url}
        return manifest

    def _send_json(self, obj: Any, status: int = 200) -> None:
        self._send_body(
            json.dumps(obj, indent=2).encode("utf-8"),
            "application/json; charset=utf-8",
            status=status,
        )

    def _send_body(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        logger.debug("%s — %s", self.address_string(), format % args)


class RegistryServer:
    """Embeddable brikie.co server.

    Args:
        data_dir: Directory holding published bricks.
        host: Bind address (default loopback).
        port: Bind port; 0 picks an ephemeral port (useful in tests).
        base_url: Public base URL for download links. When None it is
            derived from each request's Host header.
        publish_token: When set, POST /bricks/publish requires
            ``Authorization: Bearer <token>``. Unset means open publishing
            — acceptable only for local development.
    """

    def __init__(
        self,
        data_dir: str | Path,
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        base_url: str | None = None,
        publish_token: str | None = None,
    ) -> None:
        self._store = RegistryStore(data_dir)
        self._http = _RegistryHTTPServer(
            (host, port), self._store, base_url, publish_token
        )
        self._thread: threading.Thread | None = None

    @property
    def store(self) -> RegistryStore:
        """The underlying registry store."""
        return self._store

    @property
    def port(self) -> int:
        """The actual bound port (resolved when port=0 was requested)."""
        return self._http.server_address[1]

    @property
    def url(self) -> str:
        """Base URL of the registry endpoints (what RegistryClient takes)."""
        host = self._http.server_address[0]
        return f"http://{host}:{self.port}/bricks"

    def start(self) -> None:
        """Serve in a background thread (returns immediately)."""
        self._thread = threading.Thread(
            target=self._http.serve_forever, name="brikie-registry", daemon=True
        )
        self._thread.start()
        logger.info("brikie.co registry serving at %s", self.url)

    def serve_forever(self) -> None:
        """Serve on the calling thread (blocks until shutdown)."""
        logger.info("brikie.co registry serving at %s", self.url)
        self._http.serve_forever()

    def shutdown(self) -> None:
        """Stop the server and release the socket."""
        self._http.shutdown()
        self._http.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
