"""Unit tests for the Kadeia Registry system.

Tests BrickManifest serialization, KadeiaInstallerBrick tool execution,
and error handling. All HTTP interactions are mocked.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brikie.bricks.registry.base import BrickManifest
from brikie.bricks.registry.kadeia_installer import KadeiaInstallerBrick
from brikie.bricks.registry.kadeia_registry import KadeiaRegistry, KadeiaRegistryError
from brikie.bricks.tool.base import ToolBrick


# ---------------------------------------------------------------------------
# BrickManifest
# ---------------------------------------------------------------------------


SAMPLE_MANIFEST = {
    "name": "test_brick",
    "version": "1.0.0",
    "type": "soul",
    "description": "A test brick",
    "author": "brikie",
    "homepage": "https://example.com",
    "download_url": "https://kadeia.co/bricks/test_brick/1.0.0/archive.tar.gz",
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


class TestBrickManifest:
    """Verify BrickManifest creation, serialization, and defaults."""

    def test_create_full_manifest(self):
        manifest = BrickManifest(**SAMPLE_MANIFEST)
        assert manifest.name == "test_brick"
        assert manifest.version == "1.0.0"
        assert manifest.type == "soul"
        assert manifest.description == "A test brick"
        assert manifest.author == "brikie"
        assert manifest.homepage == "https://example.com"
        assert manifest.download_url == "https://kadeia.co/bricks/test_brick/1.0.0/archive.tar.gz"
        assert manifest.checksum == "abc123"
        assert manifest.dependencies == ["dep1", "dep2"]
        assert len(manifest.tool_schemas) == 1
        assert manifest.config_schema == {"type": "object", "properties": {}}

    def test_create_minimal_manifest(self):
        """Only required fields (name, version, type, description, download_url)."""
        manifest = BrickManifest(
            name="minimal",
            version="0.0.1",
            type="tool",
            description="Minimal brick",
            download_url="https://example.com/minimal.tar.gz",
        )
        assert manifest.name == "minimal"
        assert manifest.version == "0.0.1"
        assert manifest.type == "tool"
        assert manifest.description == "Minimal brick"
        assert manifest.download_url == "https://example.com/minimal.tar.gz"
        # Optionals
        assert manifest.author is None
        assert manifest.homepage is None
        assert manifest.checksum is None
        assert manifest.dependencies == []
        assert manifest.tool_schemas == []
        assert manifest.config_schema == {}

    def test_to_dict(self):
        manifest = BrickManifest(**SAMPLE_MANIFEST)
        d = manifest.to_dict()
        assert d["name"] == "test_brick"
        assert d["version"] == "1.0.0"
        assert d["type"] == "soul"
        assert d["dependencies"] == ["dep1", "dep2"]
        assert d["tool_schemas"] == SAMPLE_MANIFEST["tool_schemas"]

    def test_from_dict(self):
        manifest = BrickManifest.from_dict(SAMPLE_MANIFEST)
        assert manifest.name == "test_brick"
        assert manifest.version == "1.0.0"
        assert manifest.type == "soul"

    def test_to_dict_from_dict_round_trip(self):
        original = BrickManifest(**SAMPLE_MANIFEST)
        d = original.to_dict()
        restored = BrickManifest.from_dict(d)
        assert restored.name == original.name
        assert restored.version == original.version
        assert restored.type == original.type
        assert restored.description == original.description
        assert restored.author == original.author
        assert restored.homepage == original.homepage
        assert restored.download_url == original.download_url
        assert restored.checksum == original.checksum
        assert restored.dependencies == original.dependencies
        assert restored.tool_schemas == original.tool_schemas
        assert restored.config_schema == original.config_schema

    def test_dependencies_defaults_to_empty_list(self):
        manifest = BrickManifest(
            name="n", version="1", type="tool", description="d", download_url="u"
        )
        assert manifest.dependencies == []
        assert isinstance(manifest.dependencies, list)

    def test_tool_schemas_defaults_to_empty_list(self):
        manifest = BrickManifest(
            name="n", version="1", type="tool", description="d", download_url="u"
        )
        assert manifest.tool_schemas == []
        assert isinstance(manifest.tool_schemas, list)

    def test_empty_dependencies_round_trips_correctly(self):
        manifest = BrickManifest(
            name="n", version="1", type="tool", description="d", download_url="u"
        )
        d = manifest.to_dict()
        assert d["dependencies"] == []
        restored = BrickManifest.from_dict(d)
        assert restored.dependencies == []

    def test_config_schema_defaults_to_empty_dict(self):
        manifest = BrickManifest(
            name="n", version="1", type="tool", description="d", download_url="u"
        )
        assert manifest.config_schema == {}


# ---------------------------------------------------------------------------
# KadeiaInstallerBrick — structural tests
# ---------------------------------------------------------------------------


class TestKadeiaInstallerBrickStructure:
    """Verify KadeiaInstallerBrick inherits from ToolBrick and has correct structure."""

    def test_inherits_from_toolbrick(self):
        brick = KadeiaInstallerBrick()
        assert isinstance(brick, ToolBrick)

    def test_name_property(self):
        brick = KadeiaInstallerBrick()
        assert brick.name == "kadeia_installer"

    def test_class_tools_has_three_schemas(self):
        assert len(KadeiaInstallerBrick.tools) == 3

    def test_tool_schema_kadeia_search(self):
        schema = KadeiaInstallerBrick.tools[0]
        assert schema["function"]["name"] == "kadeia_search"
        assert "query" in schema["function"]["parameters"]["required"]

    def test_tool_schema_kadeia_install(self):
        schema = KadeiaInstallerBrick.tools[1]
        assert schema["function"]["name"] == "kadeia_install"
        assert "name" in schema["function"]["parameters"]["required"]

    def test_tool_schema_kadeia_list(self):
        schema = KadeiaInstallerBrick.tools[2]
        assert schema["function"]["name"] == "kadeia_list"
        # kadeia_list has no required params
        assert schema["function"]["parameters"]["required"] == []

    def test_installed_empty_initially(self):
        brick = KadeiaInstallerBrick()
        assert brick.installed == {}

    def test_installed_returns_copy(self):
        brick = KadeiaInstallerBrick()
        brick._installed["foo"] = BrickManifest(
            name="foo", version="1", type="tool", description="", download_url="u"
        )
        view = brick.installed
        assert "foo" in view
        # Mutating the returned dict should not affect internal state
        view.clear()
        assert "foo" in brick._installed


# ---------------------------------------------------------------------------
# KadeiaInstallerBrick — execution tests (mocked registry)
# ---------------------------------------------------------------------------


class TestKadeiaInstallerBrickExecute:
    """Verify execute() dispatches to the correct tool handler."""

    @pytest.mark.asyncio
    async def test_search(self):
        brick = KadeiaInstallerBrick()
        mock_results = [
            BrickManifest(
                name="found_brick",
                version="1.0",
                type="soul",
                description="Found",
                download_url="https://example.com/found.tar.gz",
            )
        ]
        brick._kadeia.search = AsyncMock(return_value=mock_results)

        result = await brick.execute("kadeia_search", {"query": "test"})
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["name"] == "found_brick"
        assert result[0]["version"] == "1.0"
        assert result[0]["type"] == "soul"
        brick._kadeia.search.assert_awaited_once_with("test")

    @pytest.mark.asyncio
    async def test_search_with_type_filter(self):
        brick = KadeiaInstallerBrick()
        brick._kadeia.search = AsyncMock(
            return_value=[
                BrickManifest(name="a", version="1", type="soul", description="", download_url="u"),
                BrickManifest(name="b", version="1", type="tool", description="", download_url="u"),
            ]
        )

        result = await brick.execute("kadeia_search", {"query": "test", "type_filter": "tool"})
        assert len(result) == 1
        assert result[0]["name"] == "b"

    @pytest.mark.asyncio
    async def test_search_empty_query_raises(self):
        brick = KadeiaInstallerBrick()
        with pytest.raises(ValueError, match="non-empty"):
            await brick.execute("kadeia_search", {"query": ""})

    @pytest.mark.asyncio
    async def test_install(self):
        brick = KadeiaInstallerBrick()
        manifest = BrickManifest(
            name="test_brick",
            version="2.0.0",
            type="soul",
            description="Test",
            download_url="https://example.com/test.tar.gz",
            dependencies=["dep_a"],
        )
        brick._kadeia.fetch_manifest = AsyncMock(return_value=manifest)
        brick._kadeia.download_brick = AsyncMock(return_value="/tmp/brikie/installed/test_brick-2.0.0.receipt.json")

        result = await brick.execute("kadeia_install", {"name": "test_brick"})
        assert result["name"] == "test_brick"
        assert result["version"] == "2.0.0"
        assert result["type"] == "soul"
        assert "installed_path" in result
        assert "dependencies" in result

        # Verify it was tracked internally
        assert "test_brick" in brick._installed

    @pytest.mark.asyncio
    async def test_install_with_version(self):
        brick = KadeiaInstallerBrick()
        manifest = BrickManifest(
            name="test_brick", version="3.0.0", type="tool", description="", download_url="u"
        )
        brick._kadeia.fetch_manifest = AsyncMock(return_value=manifest)
        brick._kadeia.download_brick = AsyncMock(return_value="/tmp/path")

        result = await brick.execute("kadeia_install", {"name": "test_brick", "version": "3.0.0"})
        assert result["version"] == "3.0.0"
        brick._kadeia.fetch_manifest.assert_awaited_once_with("test_brick", "3.0.0")

    @pytest.mark.asyncio
    async def test_install_empty_name_raises(self):
        brick = KadeiaInstallerBrick()
        with pytest.raises(ValueError, match="non-empty"):
            await brick.execute("kadeia_install", {"name": ""})

    @pytest.mark.asyncio
    async def test_list(self):
        brick = KadeiaInstallerBrick()
        brick._kadeia.list_available = AsyncMock(
            return_value=[
                BrickManifest(name="a", version="1", type="soul", description="", download_url="u"),
                BrickManifest(name="b", version="1", type="tool", description="", download_url="u"),
                BrickManifest(name="c", version="1", type="provider", description="", download_url="u"),
            ]
        )

        result = await brick.execute("kadeia_list", {})
        assert len(result) == 3
        names = [r["name"] for r in result]
        assert "a" in names and "b" in names and "c" in names

    @pytest.mark.asyncio
    async def test_list_with_type_filter(self):
        brick = KadeiaInstallerBrick()
        brick._kadeia.list_available = AsyncMock(
            return_value=[
                BrickManifest(name="a", version="1", type="soul", description="", download_url="u"),
                BrickManifest(name="b", version="1", type="tool", description="", download_url="u"),
            ]
        )

        result = await brick.execute("kadeia_list", {"type_filter": "soul"})
        assert len(result) == 1
        assert result[0]["name"] == "a"

    @pytest.mark.asyncio
    async def test_unknown_tool_raises_key_error(self):
        brick = KadeiaInstallerBrick()
        with pytest.raises(KeyError, match="unknown"):
            await brick.execute("unknown_tool", {})

    @pytest.mark.asyncio
    async def test_installed_populates_after_install(self):
        brick = KadeiaInstallerBrick()
        assert brick.installed == {}

        manifest = BrickManifest(
            name="new_brick", version="1.0", type="soul", description="", download_url="u"
        )
        brick._kadeia.fetch_manifest = AsyncMock(return_value=manifest)
        brick._kadeia.download_brick = AsyncMock(return_value="/tmp/path")

        await brick.execute("kadeia_install", {"name": "new_brick"})
        assert "new_brick" in brick.installed
        assert brick.installed["new_brick"].name == "new_brick"


# ---------------------------------------------------------------------------
# KadeiaRegistry — unit tests (mocked httpx)
# ---------------------------------------------------------------------------


class TestKadeiaRegistry:
    """Verify KadeiaRegistry HTTP methods are called correctly."""

    @pytest.mark.asyncio
    async def test_search_correct_url(self):
        registry = KadeiaRegistry("https://kadeia.co/bricks")
        mock_response_data = [
            {"name": "r1", "version": "1.0", "type": "soul", "description": "", "download_url": "u"}
        ]

        with patch.object(registry, "_get_json", new=AsyncMock(return_value=mock_response_data)):
            results = await registry.search("query")
            registry._get_json.assert_awaited_once_with("https://kadeia.co/bricks/search?q=query")
            assert len(results) == 1
            assert results[0].name == "r1"

    @pytest.mark.asyncio
    async def test_fetch_manifest_without_version(self):
        registry = KadeiaRegistry("https://kadeia.co/bricks")
        mock_data = {
            "name": "b", "version": "1.0", "type": "tool", "description": "d", "download_url": "u"
        }

        with patch.object(registry, "_get_json", new=AsyncMock(return_value=mock_data)):
            manifest = await registry.fetch_manifest("test_brick")
            registry._get_json.assert_awaited_once_with(
                "https://kadeia.co/bricks/test_brick/manifest.json"
            )
            assert manifest.name == "b"

    @pytest.mark.asyncio
    async def test_fetch_manifest_with_version(self):
        registry = KadeiaRegistry("https://kadeia.co/bricks")
        mock_data = {
            "name": "b", "version": "2.0", "type": "soul", "description": "d", "download_url": "u"
        }

        with patch.object(registry, "_get_json", new=AsyncMock(return_value=mock_data)):
            _ = await registry.fetch_manifest("test_brick", "2.0")
            registry._get_json.assert_awaited_once_with(
                "https://kadeia.co/bricks/test_brick/2.0/manifest.json"
            )

    @pytest.mark.asyncio
    async def test_list_available(self):
        registry = KadeiaRegistry("https://kadeia.co/bricks")
        mock_data = [
            {"name": "a", "version": "1", "type": "soul", "description": "", "download_url": "u"}
        ]

        with patch.object(registry, "_get_json", new=AsyncMock(return_value=mock_data)):
            results = await registry.list_available()
            registry._get_json.assert_awaited_once_with("https://kadeia.co/bricks/index.json")
            assert len(results) == 1

    @pytest.mark.asyncio
    async def test_download_brick_creates_receipt(self, tmp_path):
        registry = KadeiaRegistry()
        manifest = BrickManifest(
            name="test_brick",
            version="1.0.0",
            type="soul",
            description="A test",
            download_url="https://example.com/test.tar.gz",
            checksum="sha256:abc",
            dependencies=["dep1"],
        )

        receipt_path = await registry.download_brick(manifest, str(tmp_path))
        assert receipt_path.endswith("test_brick-1.0.0.receipt.json")

        import json
        receipt = json.loads((tmp_path / "test_brick-1.0.0.receipt.json").read_text())
        assert receipt["name"] == "test_brick"
        assert receipt["version"] == "1.0.0"
        assert receipt["checksum"] == "sha256:abc"
        assert receipt["dependencies"] == ["dep1"]

    @pytest.mark.asyncio
    async def test_register_brick_invalid_module_raises_error(self):
        registry = KadeiaRegistry()
        manifest = BrickManifest(
            name="x", version="1", type="tool",
            description="", download_url="https://kadeia.co/bricks/no.such.module",
        )
        from brikie.bricks.registry.kadeia_registry import KadeiaRegistryError
        with pytest.raises(KadeiaRegistryError, match="Cannot import"):
            await registry.register_brick(manifest, "fake_registry")

    @pytest.mark.asyncio
    async def test_get_json_http_error_raises_kadeia_error(self):
        registry = KadeiaRegistry("https://kadeia.co/bricks")
        import httpx

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__aenter__.return_value
            mock_response = MagicMock()
            mock_response.status_code = 404
            mock_request = MagicMock()
            mock_response.request = mock_request

            class _MockAsyncGet:
                async def __call__(self, url):
                    exc = httpx.HTTPStatusError(
                        "404", request=mock_request, response=mock_response
                    )
                    mock_response.raise_for_status.side_effect = exc
                    mock_response.raise_for_status()
                    return mock_response

            mock_client.get = _MockAsyncGet()

            with pytest.raises(KadeiaRegistryError, match="404"):
                await registry._get_json("https://kadeia.co/bricks/foo/manifest.json")

    @pytest.mark.asyncio
    async def test_get_json_request_error_raises_kadeia_error(self):
        registry = KadeiaRegistry("https://kadeia.co/bricks")
        import httpx

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__aenter__.return_value
            mock_client.get = AsyncMock(
                side_effect=httpx.RequestError("timeout")
            )

            with pytest.raises(KadeiaRegistryError, match="timeout|request failed"):
                await registry._get_json("https://kadeia.co/bricks/index.json")
