"""SoulBrick ABC — abstract base for persona manifests.

A Soul is a persona manifest: it defines the agent's name, system prompt,
authorized tools, and behavioral constraints. Souls are NOT running Bricks —
they are dataclass/json-serializable configuration objects that orchestrators
load to give an agent its identity and guardrails.

Subclasses must override the class-level field defaults to define a specific
persona.
"""

from abc import ABC
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Self


@dataclass
class SoulBrick(ABC):
    """Abstract base class for Soul/Identity persona manifests.

    Attributes:
        name: Canonical soul name (e.g., "sisyphus_orchestrator").
        system_prompt: The system prompt defining this persona.
        allowed_tools: Tool names this soul is authorized to use. ["*"] = all.
        behavioral_constraints: Constraints dict (e.g., strict_mode, max_subagents).
        description: Human-readable summary of this soul's purpose.
        version: Semver string (e.g., "1.0.0").
    """

    name: str = field(default="base_soul")
    system_prompt: str = field(default="")
    allowed_tools: List[str] = field(default_factory=lambda: ["*"])
    behavioral_constraints: Dict[str, Any] = field(default_factory=dict)
    description: str = field(default="")
    version: str = field(default="0.1.0")

    def to_manifest(self) -> Dict[str, Any]:
        """Serialize this soul to a JSON-compatible dictionary."""
        return asdict(self)

    @classmethod
    def from_manifest(cls, data: Dict[str, Any]) -> Self:
        """Deserialize a manifest dictionary into a soul instance.

        Args:
            data: A dictionary matching the soul's dataclass fields.

        Returns:
            A new soul instance with fields populated from *data*.
        """
        return cls(**data)
