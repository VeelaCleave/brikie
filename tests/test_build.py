from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from brikie.bricks.build.loader import (
    BRICK_INDEX,
    BuildLoader,
    BuildSet,
    BuildSetError,
)
from brikie.kernel.registry import BrickRegistry, ToolBrick, InterfaceBrick


class TestBrickIndex:
    def test_all_brk_numbers_are_valid(self):
        """Every entry in BRICK_INDEX should resolve to an importable module."""
        for brk, module_path in BRICK_INDEX.items():
            assert brk.startswith("BRK-")
            mod_path, _, cls_name = module_path.rpartition(".")
            assert mod_path, f"{brk}: no module path in {module_path}"
            assert cls_name, f"{brk}: no class name in {module_path}"

    def test_first_brk_in_each_block(self):
        """Sanity check known numbers."""
        assert BRICK_INDEX["BRK-100"] == "brikie.kernel.registry.ProviderBrick"
        assert BRICK_INDEX["BRK-200"] == "brikie.bricks.provider.http_provider.HTTPProvider"
        assert BRICK_INDEX["BRK-300"] == "brikie.bricks.interface.cli.CLIBrick"
        assert BRICK_INDEX["BRK-400"] == "brikie.bricks.tool.dummy.DummyToolBrick"


class TestBuildLoader:
    def test_loader_accepts_registry(self):
        registry = BrickRegistry()
        loader = BuildLoader(registry)
        assert loader._registry is registry

    def test_load_minimal_set(self):
        registry = BrickRegistry()
        loader = BuildLoader(registry)
        build = loader.load(str(Path(__file__).resolve().parent.parent / "brikie/bricks/build/sets/minimal.json"))
        assert isinstance(build, BuildSet)
        assert build.name == "minimal"

    def test_load_minimal_registers_bricks(self):
        registry = BrickRegistry()
        loader = BuildLoader(registry)
        loader.load(str(Path(__file__).resolve().parent.parent / "brikie/bricks/build/sets/minimal.json"))

        tools = registry.get_all(ToolBrick)
        interfaces = registry.get_all(InterfaceBrick)
        assert len(tools) >= 1
        assert len(interfaces) >= 1

    def test_load_nonexistent_set_raises_error(self):
        registry = BrickRegistry()
        loader = BuildLoader(registry)
        with pytest.raises(BuildSetError, match="not found"):
            loader.load("/nonexistent/path.json")

    def test_load_invalid_json_raises_error(self):
        registry = BrickRegistry()
        loader = BuildLoader(registry)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not json")
            path = f.name
        with pytest.raises(BuildSetError, match="Invalid Build Set"):
            loader.load(path)
        Path(path).unlink()

    def test_load_unknown_brk_raises(self):
        """A set referencing a brick that doesn't exist must fail loudly."""
        registry = BrickRegistry()
        loader = BuildLoader(registry)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"name": "test", "bricks": ["BRK-9999"]}, f)
            path = f.name
        with pytest.raises(BuildSetError, match="failed to load"):
            loader.load(path)
        Path(path).unlink()

    def test_souls_are_config_not_runtime_bricks(self):
        """BRK-500 block entries land in build.souls, not the registry."""
        registry = BrickRegistry()
        loader = BuildLoader(registry)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({
                "name": "test",
                "bricks": ["BRK-400", "BRK-500", "BRK-510"],
            }, f)
            path = f.name
        build = loader.load(path)
        Path(path).unlink()

        assert set(build.souls) == {"foreman", "dreamer"}
        assert "foreman" not in registry._bricks
        assert "dreamer" not in registry._bricks
        assert build.souls["foreman"].system_prompt

    def test_validate_minimum_stack_raises_when_empty(self):
        registry = BrickRegistry()
        loader = BuildLoader(registry)
        with pytest.raises(BuildSetError, match="minimum stack"):
            loader.validate_minimum_stack()

    def test_validate_minimum_stack_passes_for_minimal_set(self):
        registry = BrickRegistry()
        loader = BuildLoader(registry)
        loader.load(str(Path(__file__).resolve().parent.parent / "brikie/bricks/build/sets/minimal.json"))
        loader.validate_minimum_stack()

    def test_afk_set_loads_with_souls(self):
        """The afk set must boot-load cleanly: souls as config, rest registered."""
        registry = BrickRegistry()
        loader = BuildLoader(registry)
        build = loader.load(str(Path(__file__).resolve().parent.parent / "brikie/bricks/build/sets/afk.json"))
        assert set(build.souls) == {"foreman", "dreamer"}
        loader.validate_minimum_stack()

    def test_load_with_config(self):
        registry = BrickRegistry()
        loader = BuildLoader(registry)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({
                "name": "test",
                "bricks": [
                    {
                        "brk": "BRK-400",
                        "config": {},
                    },
                ],
            }, f)
            path = f.name
        build = loader.load(path)
        assert isinstance(build, BuildSet)
        assert build.name == "test"
        Path(path).unlink()

    def test_build_set_dataclass(self):
        build = BuildSet(name="test", description="desc")
        assert build.name == "test"
        assert build.description == "desc"
        assert build.bricks == []
