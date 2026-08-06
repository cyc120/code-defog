"""Read-only prerequisites check for an external AgentTeams runtime.

This module deliberately has no dependency on the local AgentScope adapter.
It neither invokes ``agt``/Docker nor contacts an AgentTeams endpoint: it only
inspects executable discovery, environment values, and local Unix socket
metadata.  A caller that requests AgentTeams must call :func:`require_ready`
and stop on failure; it must never silently run AgentScope instead.

The check proves only local prerequisites.  It does not claim a deployment,
TeamHarness workflow, Matrix run, or AgentTeams trace exists.
"""

from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping


Which = Callable[[str], str | None]
SocketAvailable = Callable[[Path], bool]


@dataclass(frozen=True)
class PreflightCheck:
    """One observable prerequisite for an AgentTeams execution request."""

    name: str
    passed: bool
    detail: str
    remediation: str

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
            "remediation": self.remediation,
        }


@dataclass(frozen=True)
class AgentTeamsPreflightReport:
    """Immutable result of a local, non-mutating AgentTeams preflight."""

    checks: tuple[PreflightCheck, ...]

    @property
    def ready(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def failures(self) -> tuple[PreflightCheck, ...]:
        return tuple(check for check in self.checks if not check.passed)

    def to_dict(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "checks": [check.to_dict() for check in self.checks],
        }

    def format_text(self) -> str:
        status = "READY" if self.ready else "BLOCKED"
        lines = [f"AgentTeams preflight: {status}"]
        for check in self.checks:
            marker = "ok" if check.passed else "missing"
            lines.append(f"- [{marker}] {check.name}: {check.detail}")
            if not check.passed:
                lines.append(f"  Remedy: {check.remediation}")
        return "\n".join(lines)

    def require_ready(self) -> None:
        """Raise instead of allowing a caller to fall back to AgentScope."""
        if not self.ready:
            raise AgentTeamsPreflightError(self)


class AgentTeamsPreflightError(RuntimeError):
    """Raised when an AgentTeams request cannot safely leave the local runtime."""

    def __init__(self, report: AgentTeamsPreflightReport) -> None:
        self.report = report
        super().__init__(report.format_text())


def _is_unix_socket(path: Path) -> bool:
    try:
        return stat.S_ISSOCK(path.stat().st_mode)
    except OSError:
        return False


def _docker_socket_candidates(environment: Mapping[str, str], home: Path) -> tuple[Path, ...]:
    """Return local Docker socket candidates without talking to Docker."""
    candidates: list[Path] = []
    docker_host = environment.get("DOCKER_HOST", "").strip()
    if docker_host.startswith("unix://"):
        socket_path = docker_host.removeprefix("unix://")
        if socket_path:
            candidates.append(Path(socket_path))

    # Docker Desktop on macOS commonly uses the first path; Linux commonly
    # uses the second.  Checking metadata is safe even when neither exists.
    candidates.extend((home / ".docker" / "run" / "docker.sock", Path("/var/run/docker.sock")))

    unique: list[Path] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return tuple(unique)


def inspect_agentteams_preflight(
    *,
    environment: Mapping[str, str] | None = None,
    which: Which = shutil.which,
    socket_available: SocketAvailable = _is_unix_socket,
    home: Path | None = None,
) -> AgentTeamsPreflightReport:
    """Inspect prerequisites for an AgentTeams request without side effects.

    ``agt`` and Docker are checked through PATH only.  Docker daemon status is
    inferred from a local Unix socket, never by invoking Docker or sending a
    network request.  A TCP ``DOCKER_HOST`` is intentionally rejected because
    this preflight cannot verify it without creating a network dependency.
    """
    env = dict(os.environ) if environment is None else dict(environment)
    user_home = Path.home() if home is None else Path(home)

    agt_path = which("agt")
    docker_path = which("docker")
    checks: list[PreflightCheck] = [
        PreflightCheck(
            name="agt CLI",
            passed=bool(agt_path),
            detail=(
                f"found at {agt_path}"
                if agt_path
                else "AgentTeams CLI `agt` was not found on PATH"
            ),
            remediation=(
                "Install the externally managed AgentTeams CLI and make `agt` available on PATH. "
                "Code CCTV will not substitute AgentScope."
            ),
        ),
        PreflightCheck(
            name="Docker CLI",
            passed=bool(docker_path),
            detail=(
                f"found at {docker_path}"
                if docker_path
                else "Docker executable `docker` was not found on PATH"
            ),
            remediation="Install Docker and expose its `docker` executable on PATH.",
        ),
    ]

    docker_host = env.get("DOCKER_HOST", "").strip()
    if not docker_path:
        checks.append(PreflightCheck(
            name="Docker daemon",
            passed=False,
            detail="not checked because the Docker CLI is unavailable",
            remediation="Install Docker, start its daemon, then rerun the preflight.",
        ))
    elif docker_host and not docker_host.startswith("unix://"):
        checks.append(PreflightCheck(
            name="Docker daemon",
            passed=False,
            detail=(
                f"DOCKER_HOST={docker_host!r} is not a local Unix socket; "
                "it was intentionally not contacted"
            ),
            remediation=(
                "Use a running local Docker daemon over unix:// for this offline preflight, "
                "or verify the remote daemon through the approved deployment procedure."
            ),
        ))
    else:
        sockets = _docker_socket_candidates(env, user_home)
        active_socket = next((path for path in sockets if socket_available(path)), None)
        searched = ", ".join(str(path) for path in sockets)
        checks.append(PreflightCheck(
            name="Docker daemon",
            passed=active_socket is not None,
            detail=(
                f"local Unix socket detected at {active_socket}; connectivity was not probed"
                if active_socket is not None
                else f"no local Docker Unix socket found ({searched})"
            ),
            remediation="Start Docker Desktop or the Docker daemon, then rerun the preflight.",
        ))

    return AgentTeamsPreflightReport(checks=tuple(checks))


def require_ready(
    *,
    environment: Mapping[str, str] | None = None,
    which: Which = shutil.which,
    socket_available: SocketAvailable = _is_unix_socket,
    home: Path | None = None,
) -> AgentTeamsPreflightReport:
    """Return a passing report or raise a clear, fail-closed error."""
    report = inspect_agentteams_preflight(
        environment=environment,
        which=which,
        socket_available=socket_available,
        home=home,
    )
    report.require_ready()
    return report
