from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from brikie.bricks.security.base import SecurityBrick, SecurityDecision

logger = logging.getLogger(__name__)

# Default patterns that are always blocked.
_DEFAULT_BLOCK_PATTERNS: List[Tuple[str, str]] = [
    # Destructive shell commands
    (r"rm\s+-rf\s+/", "rm -rf / (destructive filesystem)"),
    (r"rm\s+-rf\s+~", "rm -rf ~ (destructive home)"),
    (r"rm\s+-rf\s+\.", "rm -rf . (destructive cwd)"),
    (r"dd\s+if=.*of=\s*/dev/", "dd to block device (destructive)"),
    (r">\s*/dev/sd", "write to block device"),
    (r":\(\)\s*\{", "fork bomb (function definition)"),
    (r"chmod\s+-R?\s*777\s+/", "chmod 777 on root"),
    (r"chown\s+-R?\s+\d+:\d+\s+/", "chown on root"),
    (r"mkfs\.\w+\s+/dev/", "filesystem creation on device"),
    (r"dd\s+if=/dev/urandom\s+of=", "random overwrite"),

    # Network attacks
    (r"wget\s+.*\|\s*bash", "pipe wget to bash (remote code execution)"),
    (r"curl\s+.*\|\s*bash", "pipe curl to bash (remote code execution)"),
    (r"nc\s+-[eel]", "netcat with shell execution flag"),

    # Credential / secret exposure
    (r"git\s+remote\s+add\s+origin\s+https://.*@github", "credential in git remote URL"),
    (r"AWS_SECRET_ACCESS_KEY=", "AWS secret key exposure"),
    (r"export\s+.*(?:PASSWORD|SECRET|TOKEN|API_KEY)=", "potential secret export"),

    # Package manager abuse
    (r"pip\s+install\s+--user\s+--no-input", "pip install with user flag"),
    (r"npm\s+install\s+-g\s+", "global npm install"),

    # Process manipulation
    (r"kill\s+-9\s+", "SIGKILL -9"),
    (r"pkill\s+-9\s+", "process kill -9"),

    # System modification
    (r"passwd\s+", "password change"),
    (r"useradd\s+", "user creation"),
    (r"usermod\s+", "user modification"),
    (r"apt\s+(?:remove|purge|autoremove)", "package removal"),
    (r"dpkg\s+--(?:remove|purge)", "package removal"),
]


class CommandFirewallBrick(SecurityBrick):
    """Regex-based command firewall that blocks destructive tool calls.

    Evaluates tool names and argument strings against a configurable
    blocklist. Supports allowlist overrides per tool name.

    The firewall checks:
    1. Allowlist — if the tool name is in the allowlist, ALLOW immediately.
    2. Blocklist — if any argument string matches a block pattern, BLOCK.
    3. If neither applies, ALLOW.
    """

    def __init__(
        self,
        block_patterns: Optional[List[Tuple[str, str]]] = None,
        allowlisted_tools: Optional[List[str]] = None,
    ) -> None:
        super().__init__()
        self._name = "command_firewall"
        self._block_patterns = block_patterns or list(_DEFAULT_BLOCK_PATTERNS)
        self._compiled: List[Tuple[re.Pattern[str], str]] = [
            (re.compile(p, re.IGNORECASE), desc)
            for p, desc in self._block_patterns
        ]
        self._allowlisted = set(allowlisted_tools or [])

    @property
    def block_patterns(self) -> List[Tuple[str, str]]:
        return list(self._block_patterns)

    def add_block_pattern(self, pattern: str, description: str) -> None:
        """Add a custom block pattern at runtime."""
        self._block_patterns.append((pattern, description))
        self._compiled.append((re.compile(pattern, re.IGNORECASE), description))

    def add_allowlisted_tool(self, tool_name: str) -> None:
        """Add a tool to the allowlist at runtime."""
        self._allowlisted.add(tool_name)

    def remove_allowlisted_tool(self, tool_name: str) -> None:
        """Remove a tool from the allowlist."""
        self._allowlisted.discard(tool_name)

    async def evaluate(
        self,
        tool_name: str,
        args: Dict[str, Any],
        session_id: str = "",
    ) -> SecurityDecision:
        """Evaluate a tool call against the firewall rules.

        1. If the tool name is allowlisted → ALLOW immediately.
        2. Serialize args to string and check all block patterns.
        3. No match → ALLOW. Match → BLOCK.
        """
        # Allowlist takes precedence
        if tool_name in self._allowlisted:
            return SecurityDecision.ALLOW

        # Check block patterns against tool name
        match, desc = self._match_pattern(tool_name)
        if match:
            self._last_reason = desc or ("Matched block pattern on tool name '%s'" % tool_name)
            self._last_rule = match
            return SecurityDecision.BLOCK

        # Check block patterns against argument strings
        args_text = _serialize_args(args)
        match, desc = self._match_pattern(args_text)
        if match:
            self._last_reason = desc or f"Matched block pattern in arguments"
            self._last_rule = match
            return SecurityDecision.BLOCK

        return SecurityDecision.ALLOW

    def _match_pattern(self, text: str) -> Tuple[Optional[str], Optional[str]]:
        """Check text against all compiled block patterns.

        Returns:
            Tuple of (matched_pattern_string, description) or (None, None).
        """
        for compiled, desc in self._compiled:
            if compiled.search(text):
                return compiled.pattern, desc
        return None, None

    def _block_reason(self) -> str:
        return getattr(self, "_last_reason", "Blocked by command firewall")

    def _rule_matched(self) -> str:
        return getattr(self, "_last_rule", "unknown")


def _serialize_args(args: Dict[str, Any]) -> str:
    """Flatten tool arguments into a single searchable string."""
    parts: List[str] = []
    for key, value in args.items():
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, (list, tuple)):
            parts.extend(str(v) for v in value if isinstance(v, str))
        else:
            parts.append(str(value))
    return " ".join(parts)
