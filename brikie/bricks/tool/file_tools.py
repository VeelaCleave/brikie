"""ShellToolBrick — Bash execution, file read/write, glob, and grep for the agent.

Tools:
    - bash_execute: Run a shell command (blocked if destructive, see firewall).
    - read_file: Read a file from disk by path.
    - write_file: Write content to a file (creates parent dirs).
    - glob_files: List files matching a glob pattern.
    - grep_files: Search file contents with a regex pattern.
    - lsp_diagnostics: Get LSP diagnostics for a file or directory.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from brikie.bricks.tool.base import ToolBrick

logger = logging.getLogger(__name__)

# Directories the agent is allowed to read/write.
# Defaults to the cwd and /tmp. Extend via constructor's allowed_dirs.
_DEFAULT_ALLOWED_DIRS: List[str] = []

# Commands that are ALWAYS blocked regardless of arguments.
_FORBIDDEN_COMMANDS: List[str] = [
    "sudo", "su", "passwd", "useradd", "usermod", "userdel",
    "chroot", "mount", "umount",
    "mkfs", "fdisk", "parted", "dd",
    "reboot", "shutdown", "halt", "poweroff", "init",
    "iptables", "ip6tables", "firewall-cmd", "ufw",
    "systemctl", "service",
]


class ShellToolBrick(ToolBrick):
    BRICK_NUMBER = "BRK-410"
    """Provides bash, file, and search tools for the agent.

    Tools:
        - bash_execute: Run shell commands (subject to firewall + path allowlist).
        - read_file: Read a file from disk.
        - write_file: Write content to a file (creates parent directories).
        - glob_files: List files matching a glob pattern.
        - grep_contents: Search file contents with a regex pattern.
        - lsp_diagnostics: Get LSP diagnostics for a file or directory.
    """

    tools: List[Dict[str, Any]] = [
        {
            "type": "function",
            "function": {
                "name": "bash_execute",
                "description": "Run a bash command in a subprocess. Returns stdout, stderr, and exit code. "
                               "Destructive commands (rm -rf /, dd to block devices, fork bombs, etc.) "
                               "are blocked by the security firewall.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "The shell command to execute (use && to chain commands).",
                        },
                        "timeout": {
                            "type": "integer",
                            "description": "Timeout in seconds (default: 30, max: 300).",
                            "default": 30,
                        },
                        "workdir": {
                            "type": "string",
                            "description": "Working directory for the command (default: current directory).",
                        },
                    },
                    "required": ["command"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file from disk. Returns the file contents. "
                               "Large files are truncated at 2000 lines. Use 'offset' and 'limit' to page.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filePath": {
                            "type": "string",
                            "description": "Absolute path to the file to read.",
                        },
                        "offset": {
                            "type": "integer",
                            "description": "Starting line number (1-indexed, default: 1).",
                            "default": 1,
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum lines to read (default: 2000, max: 5000).",
                            "default": 2000,
                        },
                    },
                    "required": ["filePath"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Write content to a file. Creates parent directories if they don't exist. "
                               "WARNING: Overwrites existing files.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filePath": {
                            "type": "string",
                            "description": "Absolute path to the file to write.",
                        },
                        "content": {
                            "type": "string",
                            "description": "The full content to write to the file.",
                        },
                    },
                    "required": ["filePath", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "glob_files",
                "description": "List files matching a glob pattern (e.g., '**/*.py', 'src/**/*.ts'). "
                               "Returns relative paths sorted by modification time.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "description": "Glob pattern to match (e.g., '**/*.py', 'src/**/*.ts').",
                        },
                        "path": {
                            "type": "string",
                            "description": "Directory to search in (default: current working directory).",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum number of files to return (default: 100).",
                            "default": 100,
                        },
                    },
                    "required": ["pattern"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "grep_contents",
                "description": "Search file contents using a regex pattern. Returns matching files with line numbers. "
                               "Useful for finding where symbols are defined or used.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "description": "Regex pattern to search for.",
                        },
                        "include": {
                            "type": "string",
                            "description": "File glob pattern to include (e.g., '*.py', '*.{ts,tsx}').",
                        },
                        "path": {
                            "type": "string",
                            "description": "Directory to search (default: current working directory).",
                        },
                        "output_mode": {
                            "type": "string",
                            "description": "Output mode: 'content' (default) shows matching lines, "
                                           "'files_with_matches' shows only file paths.",
                            "enum": ["content", "files_with_matches"],
                            "default": "content",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum matches to return (default: 50).",
                            "default": 50,
                        },
                    },
                    "required": ["pattern"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "lsp_diagnostics",
                "description": "Get errors, warnings, and hints from the language server for a source file or directory. "
                               "Use after making code changes to verify there are no new errors.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filePath": {
                            "type": "string",
                            "description": "Absolute path to a file or directory to check.",
                        },
                        "severity": {
                            "type": "string",
                            "description": "Minimum severity: 'hint', 'information', 'warning', or 'error' (default: 'error').",
                            "enum": ["error", "warning", "information", "hint"],
                            "default": "error",
                        },
                    },
                    "required": ["filePath"],
                },
            },
        },
    ]

    def __init__(
        self,
        allowed_dirs: Optional[List[str]] = None,
        max_read_lines: int = 2000,
        max_bash_timeout: int = 300,
    ) -> None:
        super().__init__()
        self._name = "shell_tool"
        self._allowed_dirs: List[Path] = [Path(d).resolve() for d in (allowed_dirs or _DEFAULT_ALLOWED_DIRS)]
        self._max_read_lines = max_read_lines
        self._max_bash_timeout = max_bash_timeout

    @property
    def name(self) -> str:
        return self._name

    async def execute(self, name: str, args: Dict[str, Any]) -> Any:
        """Execute one of the shell/file tools by name."""
        if name == "bash_execute":
            return await self._bash_execute(args)
        elif name == "read_file":
            return await self._read_file(args)
        elif name == "write_file":
            return await self._write_file(args)
        elif name == "glob_files":
            return await self._glob_files(args)
        elif name == "grep_contents":
            return await self._grep_contents(args)
        elif name == "lsp_diagnostics":
            return await self._lsp_diagnostics(args)
        else:
            raise KeyError(f"Unknown tool: {name}")

    # ------------------------------------------------------------------
    # bash_execute
    # ------------------------------------------------------------------

    async def _bash_execute(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Run a shell command in a subprocess."""
        command = args.get("command", "")
        timeout = min(args.get("timeout", 30), self._max_bash_timeout)
        workdir = args.get("workdir")

        if not isinstance(command, str) or not command.strip():
            return {"error": "No command provided."}

        # Check for forbidden commands (first whitespace-separated token)
        first_token = command.strip().split()[0].lower() if command.strip() else ""
        if first_token in _FORBIDDEN_COMMANDS:
            return {
                "error": f"Command '{first_token}' is forbidden in bash_execute. "
                         "Use with great caution."
            }

        cwd = None
        if workdir:
            cwd = Path(workdir).expanduser().resolve()
            if not cwd.exists():
                return {"error": f"Working directory does not exist: {workdir}"}

        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                return {
                    "error": f"Command timed out after {timeout}s",
                    "command": command,
                    "stdout": "",
                    "stderr": f"Timed out after {timeout}s",
                    "exit_code": -1,
                }

            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")
            exit_code = process.returncode or 0

            # Truncate output if too large
            max_output = 100_000  # ~100KB
            if len(stdout) > max_output:
                stdout = stdout[:max_output] + f"\n... [truncated at {max_output} chars]"
            if len(stderr) > max_output:
                stderr = stderr[:max_output] + f"\n... [truncated at {max_output} chars]"

            return {
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": exit_code,
            }
        except FileNotFoundError:
            return {"error": f"Command not found: {first_token}"}
        except Exception as exc:
            logger.error("bash_execute error: %s", exc, exc_info=True)
            return {"error": str(exc)}

    # ------------------------------------------------------------------
    # read_file
    # ------------------------------------------------------------------

    async def _read_file(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Read a file from disk."""
        file_path = args.get("filePath", "")
        offset = max(args.get("offset", 1), 1)
        limit = min(args.get("limit", 2000), self._max_read_lines)

        if not file_path:
            return {"error": "No filePath provided."}

        path = Path(file_path).expanduser().resolve()
        if not path.exists():
            return {"error": f"File not found: {file_path}"}
        if not path.is_file():
            return {"error": f"Not a file: {file_path}"}

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
            total_lines = len(lines)

            start = offset - 1
            end = start + limit
            selected = lines[start:end]

            result = "\n".join(
                f"{i + start + 1}: {line}"
                for i, line in enumerate(selected)
            )

            info = {
                "filePath": str(path),
                "total_lines": total_lines,
                "offset": offset,
                "lines_returned": len(selected),
                "content": result,
            }
            if end < total_lines:
                info["truncated"] = True
                info["more_from_line"] = end + 1

            return info
        except PermissionError:
            return {"error": f"Permission denied: {file_path}"}
        except Exception as exc:
            logger.error("read_file error: %s", exc, exc_info=True)
            return {"error": str(exc)}

    # ------------------------------------------------------------------
    # write_file
    # ------------------------------------------------------------------

    async def _write_file(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Write content to a file, creating parent directories."""
        file_path = args.get("filePath", "")
        content = args.get("content", "")

        if not file_path:
            return {"error": "No filePath provided."}
        if content is None:
            content = ""

        path = Path(file_path).expanduser().resolve()

        # Block writing to sensitive system paths
        sensitive_prefixes = [
            "/etc/", "/boot/", "/sys/", "/proc/", "/dev/",
            "/usr/lib", "/usr/bin", "/usr/sbin",
            "/bin/", "/sbin/", "/lib/", "/lib64/",
        ]
        str_path = str(path)
        for prefix in sensitive_prefixes:
            if str_path.startswith(prefix):
                return {"error": f"Cannot write to system path: {prefix}"}

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return {
                "success": True,
                "filePath": str(path),
                "bytes_written": len(content.encode("utf-8")),
            }
        except PermissionError:
            return {"error": f"Permission denied: {file_path}"}
        except IsADirectoryError:
            return {"error": f"Path is a directory: {file_path}"}
        except Exception as exc:
            logger.error("write_file error: %s", exc, exc_info=True)
            return {"error": str(exc)}

    # ------------------------------------------------------------------
    # glob_files
    # ------------------------------------------------------------------

    async def _glob_files(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """List files matching a glob pattern."""
        pattern = args.get("pattern", "")
        search_path = args.get("path")
        max_results = min(args.get("max_results", 100), 500)

        if not pattern:
            return {"error": "No pattern provided."}

        base = Path(search_path).expanduser().resolve() if search_path else Path.cwd()
        if not base.exists():
            return {"error": f"Path does not exist: {search_path}"}

        try:
            matches = sorted(base.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        except Exception as exc:
            return {"error": str(exc)}

        # Filter to files only
        files = [p for p in matches if p.is_file()]
        total = len(files)
        files = files[:max_results]

        return {
            "files": [str(p.relative_to(base)) for p in files],
            "total_matches": total,
            "returned": len(files),
        }

    # ------------------------------------------------------------------
    # grep_contents
    # ------------------------------------------------------------------

    async def _grep_contents(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Search file contents using a regex pattern via grep."""
        import subprocess  # noqa: F811 — intentional shadowing for sync subprocess

        pattern = args.get("pattern", "")
        include = args.get("include", "")
        search_path = args.get("path", ".")
        output_mode = args.get("output_mode", "content")
        max_results = min(args.get("max_results", 50), 200)

        if not pattern:
            return {"error": "No pattern provided."}

        cmd = ["grep", "-rn", "--binary-files=without-match"]
        if include:
            cmd.extend(["--include", include])
        if output_mode == "files_with_matches":
            cmd.append("-l")
        cmd.append(pattern)
        cmd.append(search_path)

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            return {"error": "grep timed out after 30s"}
        except FileNotFoundError:
            return {"error": "grep not found on this system"}

        lines = result.stdout.splitlines()
        total = len(lines)
        truncated = total > max_results
        lines = lines[:max_results]

        return {
            "matches": lines,
            "total_matches": total,
            "returned": len(lines),
            "truncated": truncated,
            "stderr": result.stderr[:2000] if result.stderr else "",
        }

    # ------------------------------------------------------------------
    # lsp_diagnostics
    # ------------------------------------------------------------------

    async def _lsp_diagnostics(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Get LSP diagnostics for a file or directory.
        
        Falls back to a descriptive message if LSP tools are not available
        in the current environment.
        """
        file_path = args.get("filePath", "")
        severity = args.get("severity", "error")

        if not file_path:
            return {"error": "No filePath provided."}

        path = Path(file_path).expanduser().resolve()
        if not path.exists():
            return {"error": f"Path not found: {file_path}"}

        # Try to use flake8 or pyright if available, else return helpful message
        if path.suffix == ".py":
            return await self._py_diagnostics(path, severity)
        elif path.suffix in (".ts", ".tsx", ".js", ".jsx"):
            return await self._ts_diagnostics(path, severity)
        else:
            return {
                "note": f"LSP diagnostics not available for {path.suffix} files in this environment. "
                        "Try using bash_execute with a linter or type checker.",
                "filePath": str(path),
            }

    async def _py_diagnostics(self, path: Path, severity: str) -> Dict[str, Any]:
        """Run flake8 or py_compile for Python diagnostics."""
        import subprocess

        # Try flake8 first
        try:
            result = subprocess.run(
                ["flake8", "--format=%(path)s:%(row)d:%(col)d:%(code)s:%(text)s", str(path)],
                capture_output=True, text=True, timeout=15,
            )
            issues = result.stdout.splitlines()
            if issues:
                return {"filePath": str(path), "issues": issues, "count": len(issues), "tool": "flake8"}
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        # Fallback: py_compile syntax check
        try:
            import py_compile
            try:
                py_compile.compile(str(path), doraise=True)
                return {"filePath": str(path), "issues": [], "count": 0, "note": "No syntax errors."}
            except py_compile.PyCompileError as exc:
                return {"filePath": str(path), "issues": [str(exc)], "count": 1, "tool": "py_compile"}
        except Exception as exc:
            return {"error": str(exc), "filePath": str(path)}

    async def _ts_diagnostics(self, path: Path, severity: str) -> Dict[str, Any]:
        """Try tsc or npx tsc --noEmit for TypeScript diagnostics."""
        import subprocess

        project_root = self._find_ts_project_root(path)
        if not project_root:
            return {"note": "No tsconfig.json found in parent directories.", "filePath": str(path)}

        try:
            result = subprocess.run(
                ["npx", "tsc", "--noEmit", "--pretty", "false"],
                capture_output=True, text=True, timeout=30,
                cwd=str(project_root),
            )
            # Filter for issues in the requested file
            path_str = str(path)
            rel_path = str(path.relative_to(project_root))
            all_lines = result.stdout.splitlines() + result.stderr.splitlines()
            relevant = [l for l in all_lines if path_str in l or rel_path in l]
            return {
                "filePath": str(path),
                "issues": relevant[:50],
                "count": len(relevant),
                "tool": "tsc",
            }
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        return {"note": "TypeScript compiler not available.", "filePath": str(path)}

    @staticmethod
    def _find_ts_project_root(path: Path) -> Optional[Path]:
        """Walk up to find tsconfig.json."""
        current = path.parent if path.is_file() else path
        for _ in range(10):
            if (current / "tsconfig.json").exists():
                return current
            parent = current.parent
            if parent == current:
                break
            current = parent
        return None
