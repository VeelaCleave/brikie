"""Tests for the brikie.co server — store, HTTP routes, and the full
author → publish → install round trip over real HTTP (no mocks)."""

from __future__ import annotations

import json

import httpx
import pytest

from brikie.bricks.registry.installer import RegistryInstallerBrick
from brikie.bricks.registry.registry_client import RegistryClient, RegistryError
from brikie.kernel.registry import BrickRegistry
from brikie.server.registry_server import RegistryServer
from brikie.server.store import RegistryStore, StoreError
from brikie.server.website import GenerationError, generate_buildset, generate_install_sh

GREETER_SOURCE = '''\
"""A tiny test brick that greets."""


class GreeterBrick:
    BRICK_NUMBER = "BRK-9999"

    def __init__(self) -> None:
        self.initialized = False

    @property
    def name(self) -> str:
        return "greeter"

    async def init(self) -> None:
        self.initialized = True

    async def shutdown(self) -> None:
        self.initialized = False
'''


@pytest.fixture
def store(tmp_path) -> RegistryStore:
    return RegistryStore(tmp_path / "registry")


@pytest.fixture
def server(tmp_path):
    srv = RegistryServer(data_dir=tmp_path / "registry", port=0)
    srv.start()
    yield srv
    srv.shutdown()


def _manifest(name: str = "greeter", version: str = "0.1.0") -> dict:
    return {
        "name": name,
        "version": version,
        "type": "tool",
        "description": "Says hello",
        "author": "agent",
    }


# ──────────────────────────────────────────────────────────────────────
# RegistryStore
# ──────────────────────────────────────────────────────────────────────


class TestRegistryStore:
    def test_publish_and_get_manifest(self, store):
        stored = store.publish(_manifest(), GREETER_SOURCE)
        assert stored["checksum"].startswith("sha256:")
        assert stored["download_url"] == "/bricks/greeter/0.1.0/source.py"
        assert store.get_manifest("greeter")["version"] == "0.1.0"
        assert store.get_source("greeter", "0.1.0") == GREETER_SOURCE.encode()

    def test_latest_version_is_numeric_not_lexicographic(self, store):
        store.publish(_manifest(version="0.9.0"), GREETER_SOURCE)
        store.publish(_manifest(version="0.10.0"), GREETER_SOURCE)
        assert store.get_manifest("greeter")["version"] == "0.10.0"

    def test_pinned_version_lookup(self, store):
        store.publish(_manifest(version="0.1.0"), GREETER_SOURCE)
        store.publish(_manifest(version="0.2.0"), GREETER_SOURCE)
        assert store.get_manifest("greeter", "0.1.0")["version"] == "0.1.0"

    def test_duplicate_version_conflicts(self, store):
        store.publish(_manifest(), GREETER_SOURCE)
        with pytest.raises(StoreError) as exc:
            store.publish(_manifest(), GREETER_SOURCE)
        assert exc.value.status == 409

    def test_unknown_brick_is_404(self, store):
        with pytest.raises(StoreError) as exc:
            store.get_manifest("ghost")
        assert exc.value.status == 404

    def test_invalid_fields_rejected(self, store):
        with pytest.raises(StoreError):
            store.publish(_manifest(name="../evil"), GREETER_SOURCE)
        with pytest.raises(StoreError):
            store.publish(_manifest(version="latest"), GREETER_SOURCE)
        with pytest.raises(StoreError):
            store.publish({**_manifest(), "type": "virus"}, GREETER_SOURCE)
        with pytest.raises(StoreError):
            store.publish(_manifest(), "def broken(:")
        assert store.list_manifests() == []

    def test_search_matches_name_description_type(self, store):
        store.publish(_manifest(), GREETER_SOURCE)
        assert len(store.search("greet")) == 1
        assert len(store.search("hello")) == 1
        assert len(store.search("tool")) == 1
        assert store.search("nonexistent") == []
        assert store.search("") == []


# ──────────────────────────────────────────────────────────────────────
# Installer generation (pure functions)
# ──────────────────────────────────────────────────────────────────────


class TestBuildSetGeneration:
    def test_generates_minimum_stack_with_config(self):
        build = generate_buildset(["BRK-300", "BRK-200"], "mini")
        assert build["name"] == "mini"
        brks = [b["brk"] for b in build["bricks"]]
        assert brks == ["BRK-300", "BRK-200"]
        # bare BRK-200 resolves to the default provider preset (ollama)
        provider = build["bricks"][1]
        assert provider["config"]["base_url"] == "http://localhost:11434/v1"
        assert provider["config"]["api_key"] == "not-needed"

    def test_provider_preset_selection(self):
        build = generate_buildset(["BRK-300", "BRK-200@anthropic"], "mini")
        provider = build["bricks"][1]
        assert provider["brk"] == "BRK-200"
        assert provider["config"]["base_url"] == "https://api.anthropic.com"
        assert provider["config"]["api_format"] == "claude"
        # keys are env references, never literals
        assert provider["config"]["api_key"] == "env:ANTHROPIC_API_KEY"

    def test_two_provider_presets_first_wins(self):
        build = generate_buildset(
            ["BRK-300", "BRK-200@groq", "BRK-200@openai"], "mini"
        )
        providers = [b for b in build["bricks"] if b["brk"] == "BRK-200"]
        assert len(providers) == 1
        assert "groq" in providers[0]["config"]["base_url"]

    def test_missing_provider_rejected(self):
        with pytest.raises(GenerationError, match="Provider"):
            generate_buildset(["BRK-300", "BRK-410"], "x")

    def test_missing_interface_rejected(self):
        with pytest.raises(GenerationError, match="Interface"):
            generate_buildset(["BRK-200"], "x")

    def test_unknown_brick_rejected(self):
        with pytest.raises(GenerationError, match="Unknown"):
            generate_buildset(["BRK-300", "BRK-200", "BRK-123"], "x")

    def test_bad_name_rejected(self):
        with pytest.raises(GenerationError, match="name"):
            generate_buildset(["BRK-300", "BRK-200"], "../etc")

    def test_duplicates_collapse(self):
        build = generate_buildset(["BRK-300", "BRK-300", "BRK-200"], "x")
        assert len(build["bricks"]) == 2

    def test_install_sh_embeds_buildset(self):
        build = generate_buildset(["BRK-300", "BRK-200", "BRK-410"], "mini")
        script = generate_install_sh(build)
        assert script.startswith("#!/usr/bin/env sh")
        assert "set -eu" in script
        assert "brikie --set mini" in script
        assert "BRK-410" in script


# ──────────────────────────────────────────────────────────────────────
# HTTP routes
# ──────────────────────────────────────────────────────────────────────


class TestHTTPRoutes:
    async def test_empty_index(self, server):
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{server.url}/index.json")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_publish_then_fetch_everything(self, server):
        base = server.url
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{base}/publish",
                json={"manifest": _manifest(), "source_code": GREETER_SOURCE},
            )
            assert resp.status_code == 201
            published = resp.json()
            assert published["download_url"].startswith("http://")

            index = (await client.get(f"{base}/index.json")).json()
            assert [m["name"] for m in index] == ["greeter"]

            found = (await client.get(f"{base}/search?q=greet")).json()
            assert len(found) == 1

            latest = (await client.get(f"{base}/greeter/manifest.json")).json()
            pinned = (await client.get(f"{base}/greeter/0.1.0/manifest.json")).json()
            assert latest == pinned

            source = await client.get(f"{base}/greeter/0.1.0/source.py")
            assert source.content == GREETER_SOURCE.encode()

    async def test_publish_duplicate_is_409(self, server):
        async with httpx.AsyncClient() as client:
            body = {"manifest": _manifest(), "source_code": GREETER_SOURCE}
            assert (await client.post(f"{server.url}/publish", json=body)).status_code == 201
            resp = await client.post(f"{server.url}/publish", json=body)
        assert resp.status_code == 409
        assert "already published" in resp.json()["error"]

    async def test_publish_bad_body_is_400(self, server):
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{server.url}/publish", json={"manifest": []})
        assert resp.status_code == 400

    async def test_path_traversal_rejected(self, server):
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{server.url}/%2e%2e/manifest.json")
        assert resp.status_code in (400, 404)

    async def test_unknown_route_is_404(self, server):
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{server.url}/nope")
        assert resp.status_code == 404

    async def test_website_page(self, server):
        root = server.url.removesuffix("/bricks")
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{root}/")
        assert resp.status_code == 200
        assert "brick by brick" in resp.text
        assert "BRK-410" in resp.text

    async def test_buildset_endpoint(self, server):
        root = server.url.removesuffix("/bricks")
        async with httpx.AsyncClient() as client:
            ok = await client.get(f"{root}/buildset.json?bricks=BRK-300,BRK-200&name=mini")
            bad = await client.get(f"{root}/buildset.json?bricks=BRK-300&name=mini")
        assert ok.status_code == 200
        assert ok.json()["name"] == "mini"
        assert bad.status_code == 400

    async def test_install_sh_endpoint(self, server):
        root = server.url.removesuffix("/bricks")
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{root}/install.sh?bricks=BRK-300,BRK-200&name=mini")
        assert resp.status_code == 200
        assert resp.text.startswith("#!/usr/bin/env sh")
        assert "brikie --set mini" in resp.text


# ──────────────────────────────────────────────────────────────────────
# RegistryClient against the live server
# ──────────────────────────────────────────────────────────────────────


class TestClientAgainstLiveServer:
    async def test_publish_list_fetch_download(self, server, tmp_path):
        from brikie.bricks.registry.base import BrickManifest

        client = RegistryClient(server.url)
        published = await client.publish(
            BrickManifest.from_dict({**_manifest(), "download_url": ""}),
            GREETER_SOURCE,
        )
        assert published.checksum.startswith("sha256:")

        names = [m.name for m in await client.list_available()]
        assert names == ["greeter"]

        manifest = await client.fetch_manifest("greeter")
        path = await client.download_brick(manifest, str(tmp_path / "dl"))
        assert json.loads(
            (tmp_path / "dl" / "greeter-0.1.0.receipt.json").read_text()
        )["checksum"] == published.checksum
        assert open(path).read() == GREETER_SOURCE

    async def test_publish_duplicate_raises(self, server):
        from brikie.bricks.registry.base import BrickManifest

        client = RegistryClient(server.url)
        manifest = BrickManifest.from_dict({**_manifest(), "download_url": ""})
        await client.publish(manifest, GREETER_SOURCE)
        with pytest.raises(RegistryError, match="already published"):
            await client.publish(manifest, GREETER_SOURCE)

    async def test_fetch_unknown_raises(self, server):
        client = RegistryClient(server.url)
        with pytest.raises(RegistryError, match="404"):
            await client.fetch_manifest("ghost")


# ──────────────────────────────────────────────────────────────────────
# The full circle: author → publish → install → use
# ──────────────────────────────────────────────────────────────────────


class TestAuthorPublishInstallRoundTrip:
    async def test_full_circle(self, server, tmp_path):
        # Agent A authors and publishes a brick…
        registry_a = BrickRegistry()
        author = RegistryInstallerBrick(
            registry_url=server.url,
            registry=registry_a,
            install_dir=str(tmp_path / "agent_a"),
        )
        created = await author.execute("registry_create_brick", {
            "name": "greeter",
            "type": "tool",
            "description": "Says hello",
            "source_code": GREETER_SOURCE,
        })
        assert created["seated"] is True

        published = await author.execute("registry_publish", {"name": "greeter"})
        assert published["published"] is True
        assert published["checksum"].startswith("sha256:")

        # …the local sidecar now carries the canonical registry manifest…
        sidecar = json.loads(
            (tmp_path / "agent_a" / "greeter-0.1.0.manifest.json").read_text()
        )
        assert sidecar["download_url"] == published["download_url"]

        # …and a completely fresh agent B installs and runs it.
        registry_b = BrickRegistry()
        consumer = RegistryInstallerBrick(
            registry_url=server.url,
            registry=registry_b,
            install_dir=str(tmp_path / "agent_b"),
        )
        installed = await consumer.execute("registry_install", {"name": "greeter"})
        assert installed["seated"] is True
        brick = registry_b.get("greeter")
        assert brick.initialized is True

    async def test_publish_without_local_brick_fails(self, server, tmp_path):
        installer = RegistryInstallerBrick(
            registry_url=server.url, install_dir=str(tmp_path / "empty")
        )
        with pytest.raises(ValueError, match="author it first"):
            await installer.execute("registry_publish", {"name": "ghost"})

    async def test_republish_same_version_conflicts(self, server, tmp_path):
        installer = RegistryInstallerBrick(
            registry_url=server.url,
            registry=BrickRegistry(),
            install_dir=str(tmp_path / "a"),
        )
        await installer.execute("registry_create_brick", {
            "name": "greeter", "type": "tool", "source_code": GREETER_SOURCE,
        })
        await installer.execute("registry_publish", {"name": "greeter"})
        with pytest.raises(RegistryError, match="already published"):
            await installer.execute("registry_publish", {"name": "greeter"})


# ──────────────────────────────────────────────────────────────────────
# Publish authentication
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def auth_server(tmp_path):
    srv = RegistryServer(
        data_dir=tmp_path / "registry", port=0, publish_token="sekrit-token"
    )
    srv.start()
    yield srv
    srv.shutdown()


class TestPublishAuth:
    async def test_publish_without_token_is_401(self, auth_server):
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{auth_server.url}/publish",
                json={"manifest": _manifest(), "source_code": GREETER_SOURCE},
            )
        assert resp.status_code == 401
        assert "unauthorized" in resp.json()["error"]

    async def test_publish_with_wrong_token_is_401(self, auth_server):
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{auth_server.url}/publish",
                json={"manifest": _manifest(), "source_code": GREETER_SOURCE},
                headers={"Authorization": "Bearer wrong"},
            )
        assert resp.status_code == 401

    async def test_publish_with_token_succeeds(self, auth_server):
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{auth_server.url}/publish",
                json={"manifest": _manifest(), "source_code": GREETER_SOURCE},
                headers={"Authorization": "Bearer sekrit-token"},
            )
        assert resp.status_code == 201

    async def test_reads_stay_open_without_token(self, auth_server):
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{auth_server.url}/index.json")
        assert resp.status_code == 200

    async def test_client_sends_token(self, auth_server):
        from brikie.bricks.registry.base import BrickManifest

        client = RegistryClient(auth_server.url, publish_token="sekrit-token")
        published = await client.publish(
            BrickManifest.from_dict({**_manifest(), "download_url": ""}),
            GREETER_SOURCE,
        )
        assert published.checksum.startswith("sha256:")

    async def test_client_without_token_raises_401(self, auth_server, monkeypatch):
        from brikie.bricks.registry.base import BrickManifest

        monkeypatch.delenv("BRIKIE_PUBLISH_TOKEN", raising=False)
        client = RegistryClient(auth_server.url)
        with pytest.raises(RegistryError, match="401"):
            await client.publish(
                BrickManifest.from_dict({**_manifest(), "download_url": ""}),
                GREETER_SOURCE,
            )

    async def test_installer_brick_passes_token_through(self, auth_server, tmp_path):
        installer = RegistryInstallerBrick(
            registry_url=auth_server.url,
            registry=BrickRegistry(),
            install_dir=str(tmp_path / "a"),
            publish_token="sekrit-token",
        )
        await installer.execute("registry_create_brick", {
            "name": "greeter", "type": "tool", "source_code": GREETER_SOURCE,
        })
        result = await installer.execute("registry_publish", {"name": "greeter"})
        assert result["published"] is True
