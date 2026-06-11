"""Provider Bricks — LLM providers (HTTP, local, WebSocket).

ABCs only — no concrete bricks are exported here.
Import concrete bricks directly from their modules:

    from brikie.bricks.provider.http_provider import HTTPProvider
"""

from brikie.bricks.provider.base import ProviderBrick

__all__ = ["ProviderBrick"]
