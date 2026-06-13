"""Security Bricks — sandboxing, command firewalling, and execution isolation.

Security Bricks intercept PRE_TOOL and POST_TOOL hooks to validate,
isolate, and audit tool invocations.

ABCs only — no concrete bricks are exported here.
Import concrete bricks directly from their modules:

    from brikie.bricks.security.firewall import CommandFirewallBrick
    from brikie.bricks.security.sandbox import SandboxSecurityBrick
    from brikie.bricks.security.watchdog import WatchdogSecurityBrick
"""

from brikie.bricks.security.base import SecurityBrick, SecurityDecision, BlockedCommand

__all__ = [
    "SecurityBrick",
    "SecurityDecision",
    "BlockedCommand",
]
