"""Unit tests for the brikie.co registry system.

Tests BrickManifest serialization, RegistryClient download/load behavior,
and RegistryInstallerBrick tool execution including agent-authored bricks.
All HTTP interactions are mocked; dynamic loading is exercised for real
against temp files.
"""

import hashlib
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brikie.bricks.registry.base import BrickManifest
from brikie.bricks.registry.installer import RegistryInstallerBrick
from brikie.bricks.registry.registry_client import RegistryClient, RegistryError
from brikie.bricks.tool.base import ToolBrick
from brikie.kernel.registry import BrickRegistry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


SAMPLE_MANIFEST = {
    "name": "test_brick",
    "version": "1.0.0",
    "type": "soul",
    "description": "A test brick",
    "author": "brikie",
    "homepage": "https://example.com",
    "download_url": "https://brikie.co/bricks/test_brick/1.0.0/brick.py",
    "checksum": "abc123",
    "dependencies": ["dep1", "dep2"],
    "tool_schemas": [
        {
            "type": "function",
            "function": {"name": "test_tool", "parameters": {"type": "object", "properties": {}}},
        }
    ],
    "config_schema": {"type": "object", "properties": {}},
}

# A complete, minimal brick module for dynamic-load tests.
GREETER_SOURCE = '''\
"""A tiny test brick."""


class GreeterBrick:
    BRICK_NUMBER = "BRK-9999"

    def __init__(self):
        self._name = "greeter"
        self.initialized = False

    @property
    def name(self):
        return self._name

    async def init(self):
        self.initialized = True

    async def shutdown(self):
        self.initialized = False
'''


@pytest.fixture
def kernel_registry():
    return BrickRegistry()


@pytest.fixture
def installer(kernel_registry, tmp_path):
    return RegistryInstallerBrick(
        registry=kernel_registry, install_dir=str(tmp_path)
    )


# ---------------------------------------------------------------------------
# BrickManifest
# ---------------------------------------------------------------------------


class TestBrickManifest:
    """Verify BrickManifest creation, serialization, and defaults."""

    def test_create_full_manifest(self):
        manifest = BrickManifest(**SAMPLE_MANIFEST)
        assert manifest.name == "test_brick"
        assert manifest.version == "1.0.0"
        assert manifest.type == "soul"
        assert manifest.checksum == "abc123"
        assert manifest.dependencies == ["dep1", "dep2"]
        assert len(manifest.tool_schemas) == 1

    def test_create_minimal_manifest(self):
        manifest = BrickManifest(
            name="minimal",
            version="0.0.1",
            type="tool",
            description="Minimal brick",
            download_url="https://example.com/minimal.py",
        )
        assert manifest.author is None
        assert manifest.homepage is None
        assert manifest.checksum is None
        assert manifest.dependencies == []
        assert manifest.tool_schemas == []
        assert manifest.config_schema == {}

    def test_to_dict_from_dict_round_trip(self):
        original = BrickManifest(**SAMPLE_MANIFEST)
        restored = BrickManifest.from_dict(original.to_dict())
        assert restored == original


# ---------------------------------------------------------------------------
# RegistryInstallerBrick — structure
# ---------------------------------------------------------------------------


class TestRegistryInstallerBrickStructure:
    def test_inherits_from_toolbrick(self):
        assert isinstance(RegistryInstallerBrick(), ToolBrick)

    def test_name_property(self):
        assert RegistryInstallerBrick().name == "registry_installer"

    def test_class_tools_has_five_schemas(self):
        names = [t["function"]["name"] for t in RegistryInstallerBrick.tools]
        assert names == [
            "registry_search",
            "registry_install",
            "registry_list",
            "registry_create_brick",
            "registry_uninstall",
        ]

    def test_create_brick_schema_requires_source(self):
        schema = next(
            t for t in RegistryInstallerBrick.tools
            if t["function"]["name"] == "registry_create_brick"
        )
        required = schema["function"]["parameters"]["required"]
        assert "name" in required
        assert "type" in required
        assert "source_code" in required

    def test_installed_empty_initially(self):
        assert RegistryInstallerBrick().installed == {}

    def test_installed_returns_copy(self):
        brick = RegistryInstallerBrick()
        brick._installed["foo"] = BrickManifest(
            name="foo", version="1", type="tool", description="", download_url="u"
        )
        view = brick.installed
        view.clear()
        assert "foo" in brick._installed


# ---------------------------------------------------------------------------
# RegistryInstallerBrick — search / list (mocked client)
# ---------------------------------------------------------------------------


class TestRegistryInstallerSearchList:
    @pytest.mark.asyncio
    async def test_search(self):
        brick = RegistryInstallerBrick()
        brick._client.search = AsyncMock(return_value=[
            BrickManifest(name="found", version="1.0", type="soul",
                          description="", download_url="u"),
        ])
        result = await brick.execute("registry_search", {"query": "test"})
        assert len(result) == 1
        assert result[0]["name"] == "found"
        brick._client.search.assert_awaited_once_with("test")

    @pytest.mark.asyncio
    async def test_search_with_type_filter(self):
        brick = RegistryInstallerBrick()
        brick._client.search = AsyncMock(return_value=[
            BrickManifest(name="a", version="1", type="soul", description="", download_url="u"),
            BrickManifest(name="b", version="1", type="tool", description="", download_url="u"),
        ])
        result = await brick.execute(
            "registry_search", {"query": "test", "type_filter": "tool"}
        )
        assert [r["name"] for r in result] == ["b"]

    @pytest.mark.asyncio
    async def test_search_empty_query_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            await RegistryInstallerBrick().execute("registry_search", {"query": ""})

    @pytest.mark.asyncio
    async def test_list_with_type_filter(self):
        brick = RegistryInstallerBrick()
        brick._client.list_available = AsyncMock(return_value=[
            BrickManifest(name="a", version="1", type="soul", description="", download_url="u"),
            BrickManifest(name="b", version="1", type="tool", description="", download_url="u"),
        ])
        result = await brick.execute("registry_list", {"type_filter": "soul"})
        assert [r["name"] for r in result] == ["a"]

    @pytest.mark.asyncio
    async def test_unknown_tool_raises_key_error(self):
        with pytest.raises(KeyError, match="Unknown tool"):
            await RegistryInstallerBrick().execute("unknown_tool", {})


# ---------------------------------------------------------------------------
# RegistryInstallerBrick — install (download mocked, load real)
# ---------------------------------------------------------------------------


class TestRegistryInstall:
    @pytest.mark.asyncio
    async def test_install_seats_brick_into_registry(self, installer, kernel_registry, tmp_path):
        """Full install path: manifest → download (mocked) → real dynamic load."""
        manifest = BrickManifest(
            name="greeter", version="1.0.0", type="tool",
            description="Greets", download_url="https://brikie.co/bricks/greeter/1.0.0/brick.py",
        )
        source_file = tmp_path / "greeter-1.0.0.py"

        async def fake_download(m, target_dir):
            source_file.write_text(GREETER_SOURCE)
            return str(source_file)

        installer._client.fetch_manifest = AsyncMock(return_value=manifest)
        installer._client.download_brick = AsyncMock(side_effect=fake_download)

        result = await installer.execute("registry_install", {"name": "greeter"})
        assert result["seated"] is True
        assert "greeter" in installer.installed

        seated = kernel_registry.get("greeter")
        assert seated.initialized is True  # init() was awaited

    @pytest.mark.asyncio
    async def test_install_without_registry_does_not_seat(self, tmp_path):
        brick = RegistryInstallerBrick(install_dir=str(tmp_path))
        manifest = BrickManifest(
            name="x", version="1", type="tool", description="", download_url="u"
        )
        brick._client.fetch_manifest = AsyncMock(return_value=manifest)
        brick._client.download_brick = AsyncMock(return_value=str(tmp_path / "x-1.py"))

        result = await brick.execute("registry_install", {"name": "x"})
        assert result["seated"] is False
        assert "x" in brick.installed

    @pytest.mark.asyncio
    async def test_install_empty_name_raises(self, installer):
        with pytest.raises(ValueError, match="non-empty"):
            await installer.execute("registry_install", {"name": ""})


# ---------------------------------------------------------------------------
# RegistryInstallerBrick — agent-authored bricks
# ---------------------------------------------------------------------------


class TestRegistryCreateBrick:
    @pytest.mark.asyncio
    async def test_create_brick_seats_and_writes_manifest(self, installer, kernel_registry, tmp_path):
        result = await installer.execute("registry_create_brick", {
            "name": "greeter",
            "type": "tool",
            "description": "Greets people",
            "source_code": GREETER_SOURCE,
        })
        assert result["seated"] is True

        seated = kernel_registry.get("greeter")
        assert seated.initialized is True

        manifest_data = json.loads((tmp_path / "greeter-0.1.0.manifest.json").read_text())
        assert manifest_data["name"] == "greeter"
        assert manifest_data["author"] == "agent"
        assert (tmp_path / "greeter-0.1.0.py").read_text() == GREETER_SOURCE

    @pytest.mark.asyncio
    async def test_create_brick_syntax_error_rejected_before_disk(self, installer, tmp_path):
        with pytest.raises(ValueError, match="syntax error"):
            await installer.execute("registry_create_brick", {
                "name": "broken", "type": "tool",
                "source_code": "def oops(:\n  pass",
            })
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_create_brick_without_brick_class_cleans_up(self, installer, tmp_path):
        with pytest.raises(RegistryError, match="No brick class"):
            await installer.execute("registry_create_brick", {
                "name": "empty", "type": "tool",
                "source_code": "x = 1\n",
            })
        assert list(tmp_path.glob("*.py")) == []
        assert list(tmp_path.glob("*.json")) == []

    @pytest.mark.asyncio
    async def test_create_brick_invalid_type_rejected(self, installer):
        with pytest.raises(ValueError, match="'type' must be one of"):
            await installer.execute("registry_create_brick", {
                "name": "x", "type": "nonsense", "source_code": GREETER_SOURCE,
            })


# ---------------------------------------------------------------------------
# RegistryInstallerBrick — uninstall
# ---------------------------------------------------------------------------


class TestRegistryUninstall:
    @pytest.mark.asyncio
    async def test_uninstall_unseats_and_deletes(self, installer, kernel_registry, tmp_path):
        await installer.execute("registry_create_brick", {
            "name": "greeter", "type": "tool", "source_code": GREETER_SOURCE,
        })
        assert kernel_registry.get("greeter") is not None

        result = await installer.execute("registry_uninstall", {
            "name": "greeter", "delete_files": True,
        })
        assert result["uninstalled"] is True
        assert result["was_seated"] is True
        with pytest.raises(KeyError):
            kernel_registry.get("greeter")
        assert "greeter" not in installer.installed
        assert list(tmp_path.glob("*.py")) == []
        assert list(tmp_path.glob("*.json")) == []

    @pytest.mark.asyncio
    async def test_uninstall_unknown_brick(self, installer):
        result = await installer.execute("registry_uninstall", {"name": "ghost"})
        assert result["uninstalled"] is False
        assert "error" in result


# ---------------------------------------------------------------------------
# RegistryClient — download / load / checksum
# ---------------------------------------------------------------------------


class TestRegistryClientDownload:
    @pytest.mark.asyncio
    async def test_download_writes_source_and_receipt(self, tmp_path):
        client = RegistryClient()
        content = GREETER_SOURCE.encode()
        manifest = BrickManifest(
            name="greeter", version="1.0.0", type="tool", description="d",
            download_url="https://brikie.co/bricks/greeter/1.0.0/brick.py",
            checksum=f"sha256:{hashlib.sha256(content).hexdigest()}",
        )
        with patch.object(client, "_get_bytes", new=AsyncMock(return_value=content)):
            path = await client.download_brick(manifest, str(tmp_path))

        assert path.endswith("greeter-1.0.0.py")
        assert (tmp_path / "greeter-1.0.0.py").read_bytes() == content
        receipt = json.loads((tmp_path / "greeter-1.0.0.receipt.json").read_text())
        assert receipt["name"] == "greeter"
        assert receipt["source_file"] == path

    @pytest.mark.asyncio
    async def test_download_checksum_mismatch_raises(self, tmp_path):
        client = RegistryClient()
        manifest = BrickManifest(
            name="evil", version="1.0.0", type="tool", description="d",
            download_url="u", checksum="sha256:" + "0" * 64,
        )
        with patch.object(client, "_get_bytes", new=AsyncMock(return_value=b"tampered")):
            with pytest.raises(RegistryError, match="Checksum mismatch"):
                await client.download_brick(manifest, str(tmp_path))
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_download_without_checksum_skips_verification(self, tmp_path):
        client = RegistryClient()
        manifest = BrickManifest(
            name="open", version="1.0.0", type="tool", description="d",
            download_url="u",
        )
        with patch.object(client, "_get_bytes", new=AsyncMock(return_value=b"data")):
            path = await client.download_brick(manifest, str(tmp_path))
        assert path.endswith("open-1.0.0.py")


class TestRegistryClientLoad:
    def test_load_brick_from_file(self, tmp_path):
        source = tmp_path / "greeter-1.0.0.py"
        source.write_text(GREETER_SOURCE)
        registry = BrickRegistry()

        brick = RegistryClient().load_brick_from_file(str(source), registry)
        assert brick.name == "greeter"
        assert registry.get("greeter") is brick

    def test_load_file_without_brick_class_raises(self, tmp_path):
        source = tmp_path / "nothing.py"
        source.write_text("x = 1\n")
        with pytest.raises(RegistryError, match="No brick class"):
            RegistryClient().load_brick_from_file(str(source), BrickRegistry())

    def test_load_broken_file_raises(self, tmp_path):
        source = tmp_path / "broken.py"
        source.write_text("raise RuntimeError('boom')\n")
        with pytest.raises(RegistryError, match="failed to import"):
            RegistryClient().load_brick_from_file(str(source), BrickRegistry())

    @pytest.mark.asyncio
    async def test_register_brick_invalid_module_raises_error(self):
        manifest = BrickManifest(
            name="x", version="1", type="tool",
            description="", download_url="https://brikie.co/bricks/no.such.module",
        )
        with pytest.raises(RegistryError, match="Cannot import"):
            await RegistryClient().register_brick(manifest, BrickRegistry())


# ---------------------------------------------------------------------------
# RegistryClient — HTTP plumbing (mocked httpx)
# ---------------------------------------------------------------------------


class TestRegistryClientHTTP:
    @pytest.mark.asyncio
    async def test_search_correct_url(self):
        client = RegistryClient("https://brikie.co/bricks")
        data = [{"name": "r1", "version": "1.0", "type": "soul",
                 "description": "", "download_url": "u"}]
        with patch.object(client, "_get_json", new=AsyncMock(return_value=data)):
            results = await client.search("query")
            client._get_json.assert_awaited_once_with(
                "https://brikie.co/bricks/search?q=query"
            )
            assert results[0].name == "r1"

    @pytest.mark.asyncio
    async def test_fetch_manifest_with_version(self):
        client = RegistryClient("https://brikie.co/bricks")
        data = {"name": "b", "version": "2.0", "type": "soul",
                "description": "d", "download_url": "u"}
        with patch.object(client, "_get_json", new=AsyncMock(return_value=data)):
            await client.fetch_manifest("test_brick", "2.0")
            client._get_json.assert_awaited_once_with(
                "https://brikie.co/bricks/test_brick/2.0/manifest.json"
            )

    @pytest.mark.asyncio
    async def test_list_available(self):
        client = RegistryClient("https://brikie.co/bricks")
        data = [{"name": "a", "version": "1", "type": "soul",
                 "description": "", "download_url": "u"}]
        with patch.object(client, "_get_json", new=AsyncMock(return_value=data)):
            results = await client.list_available()
            client._get_json.assert_awaited_once_with(
                "https://brikie.co/bricks/index.json"
            )
            assert len(results) == 1

    @pytest.mark.asyncio
    async def test_get_json_http_error_raises_registry_error(self):
        import httpx
        client = RegistryClient()
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__aenter__.return_value
            mock_response = MagicMock()
            mock_response.status_code = 404

            async def fake_get(url):
                raise httpx.HTTPStatusError(
                    "404", request=MagicMock(), response=mock_response
                )

            mock_client.get = fake_get
            with pytest.raises(RegistryError, match="404"):
                await client._get_json("https://brikie.co/bricks/foo/manifest.json")

    @pytest.mark.asyncio
    async def test_get_json_request_error_raises_registry_error(self):
        import httpx
        client = RegistryClient()
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__aenter__.return_value
            mock_client.get = AsyncMock(side_effect=httpx.RequestError("timeout"))
            with pytest.raises(RegistryError, match="request failed"):
                await client._get_json("https://brikie.co/bricks/index.json")
