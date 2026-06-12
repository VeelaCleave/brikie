"""Soul/Identity Bricks — persona manifests for the Brikie multi-head system.

ABCs only — no concrete souls are exported here.
Import concrete souls directly from their modules:

    from brikie.bricks.soul.foreman import Foreman
    from brikie.bricks.soul.dreamer import Dreamer
    from brikie.bricks.soul.crypto_trading_agent import CryptoTradingAgent
    from brikie.bricks.soul.web_design_agent import WebDesignAgent
"""

from brikie.bricks.soul.base import SoulBrick

__all__ = ["SoulBrick"]
