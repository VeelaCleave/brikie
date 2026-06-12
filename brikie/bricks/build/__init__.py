"""Build Sets — composable brick manifests for the Brikie harness.

A Build Set is a JSON manifest declaring which bricks to load by their
BRK number. The loader dynamically imports and registers them.

Usage:
    from brikie.bricks.build.loader import BuildLoader
    loader = BuildLoader(registry)
    loader.load("brikie/bricks/build/sets/minimal.json")
"""

from brikie.bricks.build.loader import BuildLoader, BuildSet, BuildSetError

__all__ = ["BuildLoader", "BuildSet", "BuildSetError"]
