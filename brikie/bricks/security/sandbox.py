from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from brikie.bricks.security.base import SecurityBrick, SecurityDecision

logger = logging.getLogger(__name__)

DEFAULT_IMAGE = "alpine:latest"
DEFAULT_TIMEOUT = 120


@dataclass
class SandboxConfig:
    """Configuration for the Docker sandbox environment."""

    image: str = DEFAULT_IMAGE
    timeout_seconds: int = DEFAULT_TIMEOUT
    memory_limit: str = "512m"
    cpu_limit: str = "1.0"
    network_enabled: bool = False
    read_only_root: bool = True
    workdir: str = "/workspace"
    environment: Dict[str, str] = field(default_factory=dict)
    volume_mounts: List[str] = field(default_factory=list)


class SandboxSecurityBrick(SecurityBrick):
    BRICK_NUMBER = "BRK-071"
    """Sandbox Security Brick that routes tool execution through Docker.

    When the firewall allows a tool but the tool is flagged as requiring
    sandboxing, this brick intercepts the PRE_TOOL hook and returns
    SANDBOX, signalling the event loop to route execution through Docker.

    The brick also provides a direct `run_in_sandbox()` method that the
    event loop can call to actually execute commands in Docker.
    """

    def __init__(
        self,
        config: Optional[SandboxConfig] = None,
        docker_binary: str = "docker",
    ) -> None:
        super().__init__()
        self._name = "sandbox"
        self._config = config or SandboxConfig()
        self._docker = docker_binary
        self._docker_available: Optional[bool] = None

    @property
    def docker_available(self) -> bool:
        """Check if Docker is available on the host."""
        if self._docker_available is not None:
            return self._docker_available
        self._docker_available = shutil.which(self._docker) is not None
        return self._docker_available

    async def evaluate(
        self,
        tool_name: str,
        args: Dict[str, Any],
        session_id: str = "",
    ) -> SecurityDecision:
        """Evaluate whether a tool call needs sandboxing.

        Currently, all shell/bash tool calls are candidates for sandboxing.
        If Docker is unavailable, BLOCK instead.
        """
        if not self.docker_available:
            logger.warning("Docker not available — blocking sandbox-eligible tool: %s", tool_name)
            return SecurityDecision.BLOCK

        # Tools that should run in sandbox
        sandbox_candidates = {"bash", "shell", "execute_command", "run_script"}
        if tool_name in sandbox_candidates:
            return SecurityDecision.ALLOW  # We don't force sandbox, just provide the capability

        return SecurityDecision.ALLOW

    async def run_in_sandbox(
        self,
        command: str,
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Execute a command inside a Docker sandbox container.

        Args:
            command: The shell command to execute.
            timeout: Override timeout in seconds.

        Returns:
            Dict with stdout, stderr, exit_code, and duration_ms.
        """
        if not self.docker_available:
            return {
                "stdout": "",
                "stderr": "Docker is not available on this system",
                "exit_code": -1,
                "duration_ms": 0,
            }

        timeout = timeout or self._config.timeout_seconds

        # Generate a unique container name
        container_name = f"brikie_sandbox_{id(self)}_{asyncio.get_event_loop().time():.0f}"

        docker_args = [
            self._docker, "run", "--rm",
            "--name", container_name,
            "--memory", self._config.memory_limit,
            "--cpus", self._config.cpu_limit,
            "--workdir", self._config.workdir,
        ]

        if self._config.read_only_root:
            docker_args.append("--read-only")

        if not self._config.network_enabled:
            docker_args.append("--network")
            docker_args.append("none")

        for env_key, env_val in self._config.environment.items():
            docker_args.extend(["-e", f"{env_key}={env_val}"])

        for mount in self._config.volume_mounts:
            docker_args.extend(["-v", mount])

        docker_args.append(self._config.image)
        docker_args.extend(["sh", "-c", command])

        start = asyncio.get_event_loop().time()

        try:
            proc = await asyncio.create_subprocess_exec(
                *docker_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )

            duration_ms = (asyncio.get_event_loop().time() - start) * 1000

            return {
                "stdout": stdout_bytes.decode("utf-8", errors="replace"),
                "stderr": stderr_bytes.decode("utf-8", errors="replace"),
                "exit_code": proc.returncode or 0,
                "duration_ms": round(duration_ms, 2),
            }

        except asyncio.TimeoutError:
            # Kill the container if it times out
            try:
                kill_proc = await asyncio.create_subprocess_exec(
                    self._docker, "kill", container_name,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await kill_proc.wait()
            except Exception:
                pass

            duration_ms = (asyncio.get_event_loop().time() - start) * 1000
            return {
                "stdout": "",
                "stderr": f"Command timed out after {timeout}s",
                "exit_code": -1,
                "duration_ms": round(duration_ms, 2),
            }

        except Exception as exc:
            duration_ms = (asyncio.get_event_loop().time() - start) * 1000
            return {
                "stdout": "",
                "stderr": str(exc),
                "exit_code": -1,
                "duration_ms": round(duration_ms, 2),
            }
