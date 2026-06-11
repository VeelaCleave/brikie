"""BrickManifest dataclass for the Kadeia Registry.

Defines the serialisable manifest format that describes every brick
published to the Kadeia registry — name, version, type, dependencies,
tool schemas, and config schema.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class BrickManifest:
    """Serialisable manifest describing a brick in the Kadeia registry.

    Attributes:
        name: Canonical brick name (e.g. "sisyphus_orchestrator").
        version: Semantic version string (e.g. "1.0.0").
        type: Brick category — "soul", "tool", "provider", "interface", or "memory".
        description: Human-readable summary of the brick's purpose.
        author: Optional author or organisation name.
        homepage: Optional URL to project homepage or docs.
        download_url: URL from which the brick archive can be fetched.
        checksum: Optional SHA-256 hex digest of the brick archive.
        dependencies: List of brick names this brick depends on.
        tool_schemas: OpenAI-compatible tool schemas exposed by this brick.
        config_schema: JSON Schema describing the brick's configuration options.
    """

    name: str
    version: str
    type: str  # "soul" | "tool" | "provider" | "interface" | "memory"
    description: str
    download_url: str
    author: str | None = None
    homepage: str | None = None
    checksum: str | None = None
    dependencies: list[str] = field(default_factory=list)
    tool_schemas: list[dict[str, Any]] = field(default_factory=list)
    config_schema: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize this manifest to a JSON-compatible dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BrickManifest:
        """Deserialize a dictionary into a BrickManifest.

        Args:
            data: Dictionary containing manifest fields.

        Returns:
            A new BrickManifest instance.
        """
        return cls(**data)
