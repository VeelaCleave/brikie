"""Filesystem-backed storage for the brikie.co brick registry.

Layout under the data directory::

    {data_dir}/{name}/{version}/manifest.json
    {data_dir}/{name}/{version}/source.py

Manifests are stored with a *relative* ``download_url``
(``/bricks/{name}/{version}/source.py``); the serving layer absolutizes
it against the request host so the same data directory works behind any
hostname (localhost in dev, brikie.co in production).
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path
from typing import Any

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_VERSION_RE = re.compile(r"^\d+(\.\d+){0,3}$")

_VALID_BRICK_TYPES = {
    "soul", "tool", "provider", "interface", "memory",
    "logging", "security", "improvement",
}


class StoreError(Exception):
    """Raised when a registry store operation fails.

    Attributes:
        status: Suggested HTTP status code for the failure.
    """

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _version_key(version: str) -> tuple[int, ...]:
    """Sortable tuple for a dotted numeric version string."""
    return tuple(int(part) for part in version.split("."))


class RegistryStore:
    """Filesystem-backed brick registry storage.

    Args:
        data_dir: Directory holding published bricks. Created on demand.
    """

    def __init__(self, data_dir: str | Path) -> None:
        self._data_dir = Path(data_dir).expanduser().resolve()
        self._lock = threading.Lock()

    @property
    def data_dir(self) -> Path:
        """The resolved data directory this store reads and writes."""
        return self._data_dir

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    def list_manifests(self) -> list[dict[str, Any]]:
        """Return the latest manifest of every published brick, sorted by name."""
        manifests: list[dict[str, Any]] = []
        if not self._data_dir.is_dir():
            return manifests
        for brick_dir in sorted(self._data_dir.iterdir()):
            if not brick_dir.is_dir() or not _NAME_RE.match(brick_dir.name):
                continue
            latest = self._latest_version(brick_dir.name)
            if latest is not None:
                manifests.append(self._read_manifest(brick_dir.name, latest))
        return manifests

    def get_manifest(self, name: str, version: str | None = None) -> dict[str, Any]:
        """Return one brick's manifest.

        Args:
            name: Brick name.
            version: Specific version, or None for the latest.

        Raises:
            StoreError: 404 when the brick or version is unknown.
        """
        self._validate_name(name)
        if version is None:
            version = self._latest_version(name)
            if version is None:
                raise StoreError(f"Unknown brick: '{name}'", status=404)
        else:
            self._validate_version(version)
        if not (self._brick_dir(name, version) / "manifest.json").is_file():
            raise StoreError(f"Unknown brick: '{name}' v{version}", status=404)
        return self._read_manifest(name, version)

    def get_source(self, name: str, version: str) -> bytes:
        """Return the stored source bytes for one brick version.

        Raises:
            StoreError: 404 when the brick or version is unknown.
        """
        self._validate_name(name)
        self._validate_version(version)
        source = self._brick_dir(name, version) / "source.py"
        if not source.is_file():
            raise StoreError(f"Unknown brick: '{name}' v{version}", status=404)
        return source.read_bytes()

    def search(self, query: str) -> list[dict[str, Any]]:
        """Case-insensitive substring search over name, description, and type."""
        needle = query.strip().lower()
        if not needle:
            return []
        return [
            m for m in self.list_manifests()
            if needle in m.get("name", "").lower()
            or needle in m.get("description", "").lower()
            or needle == m.get("type", "").lower()
        ]

    # ------------------------------------------------------------------
    # Write API
    # ------------------------------------------------------------------

    def publish(self, manifest: dict[str, Any], source_code: str) -> dict[str, Any]:
        """Validate and store a brick, returning its canonical manifest.

        The server is the authority on ``checksum`` and ``download_url``:
        whatever the client sent for those fields is replaced by the
        SHA-256 of the stored source and the canonical registry path.

        Args:
            manifest: Client-supplied manifest fields (name, version, type,
                description, and optionally author/homepage/dependencies/
                tool_schemas/config_schema).
            source_code: Complete Python source of the brick module.

        Raises:
            StoreError: 400 for invalid fields or source, 409 when the
                name+version already exists.
        """
        name = str(manifest.get("name", "")).strip()
        version = str(manifest.get("version", "")).strip()
        brick_type = str(manifest.get("type", "")).strip()

        self._validate_name(name)
        self._validate_version(version)
        if brick_type not in _VALID_BRICK_TYPES:
            raise StoreError(
                f"Invalid brick type {brick_type!r} — must be one of "
                f"{sorted(_VALID_BRICK_TYPES)}"
            )
        if not isinstance(source_code, str) or not source_code.strip():
            raise StoreError("'source_code' must be a non-empty Python module")
        try:
            compile(source_code, f"<{name}>", "exec")
        except SyntaxError as exc:
            raise StoreError(
                f"Source has a syntax error at line {exc.lineno}: {exc.msg}"
            ) from exc

        source_bytes = source_code.encode("utf-8")
        stored = {
            "name": name,
            "version": version,
            "type": brick_type,
            "description": str(manifest.get("description", "")),
            "author": manifest.get("author") or "unknown",
            "homepage": manifest.get("homepage"),
            "download_url": f"/bricks/{name}/{version}/source.py",
            "checksum": f"sha256:{hashlib.sha256(source_bytes).hexdigest()}",
            "dependencies": list(manifest.get("dependencies") or []),
            "tool_schemas": list(manifest.get("tool_schemas") or []),
            "config_schema": dict(manifest.get("config_schema") or {}),
        }

        with self._lock:
            brick_dir = self._brick_dir(name, version)
            if (brick_dir / "manifest.json").exists():
                raise StoreError(
                    f"'{name}' v{version} is already published — bump the version",
                    status=409,
                )
            brick_dir.mkdir(parents=True, exist_ok=True)
            (brick_dir / "source.py").write_bytes(source_bytes)
            (brick_dir / "manifest.json").write_text(
                json.dumps(stored, indent=2) + "\n"
            )
        return stored

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _brick_dir(self, name: str, version: str) -> Path:
        return self._data_dir / name / version

    def _latest_version(self, name: str) -> str | None:
        """Highest published version of *name*, or None when unpublished."""
        brick_dir = self._data_dir / name
        if not brick_dir.is_dir():
            return None
        versions = [
            d.name for d in brick_dir.iterdir()
            if d.is_dir() and _VERSION_RE.match(d.name)
            and (d / "manifest.json").is_file()
        ]
        if not versions:
            return None
        return max(versions, key=_version_key)

    def _read_manifest(self, name: str, version: str) -> dict[str, Any]:
        raw = (self._brick_dir(name, version) / "manifest.json").read_text()
        manifest: dict[str, Any] = json.loads(raw)
        return manifest

    @staticmethod
    def _validate_name(name: str) -> None:
        if not _NAME_RE.match(name):
            raise StoreError(
                f"Invalid brick name {name!r} — lowercase letters, digits, "
                "and underscores only"
            )

    @staticmethod
    def _validate_version(version: str) -> None:
        if not _VERSION_RE.match(version):
            raise StoreError(
                f"Invalid version {version!r} — dotted numbers only (e.g. '0.1.0')"
            )
