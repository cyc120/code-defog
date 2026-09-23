#!/usr/bin/env python3
"""Read-only prerequisites check for the local CLI agent runtime.

Mirrors :mod:`agent_runtime.agentteams_preflight`: no network calls, no
process spawning, injectable dependencies.  A caller that requests
``--runtime-mode cli`` must call :func:`require_ready` and stop on failure;
it must never silently fall back to the in-process mock or AgentScope.

The check proves only that the configured CLI executable is discoverable.
It does not claim the CLI is authenticated, that a model is reachable, or
that anything resembling an AgentTeams deployment exists — authentication
is the CLI's own concern and surfaces later as a non-zero CLI exit code,
which the adapter records as a failed Agent run.
"""

from __future__ import annotations

import os
import shlex
import shutil
from dataclasses import dataclass
from typing import Callable, Mapping

Which = Callable[[str], "str | None"]

# Informational only — never gates readiness.  The CLIs also support their
# own interactive login, which this module deliberately does not probe.
_AUTH_ENV_HINTS: dict[str, tuple[str, ...]] = {
    "codex": ("OPENAI_API_KEY",),
    "claude": ("ANTHROPIC_API_KEY",),
}


@dataclass(frozen=True)
class CLIPreflightCheck:
    """One observable prerequisite for the local CLI agent runtime."""

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


class CLITeamsPreflightError(RuntimeError):
    """Raised when the local CLI runtime is requested but not runnable."""

    def __init__(self, report: "CLITeamsPreflightReport") -> None:
        self.report = report
        super().__init__(
            "local CLI agent runtime is not ready: "
            + "; ".join(check.detail for check in report.failures)
        )


@dataclass(frozen=True)
class CLITeamsPreflightReport:
    """Immutable result of a local, non-mutating CLI preflight."""

    checks: tuple[CLIPreflightCheck, ...]

    @property
    def ready(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def failures(self) -> tuple[CLIPreflightCheck, ...]:
        return tuple(check for check in self.checks if not check.passed)

    def to_dict(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "checks": [check.to_dict() for check in self.checks],
        }

    def format_text(self) -> str:
        status = "READY" if self.ready else "BLOCKED"
        lines = [f"CLI agent preflight: {status}"]
        for check in self.checks:
            marker = "ok" if check.passed else "missing"
            lines.append(f"- [{marker}] {check.name}: {check.detail}")
            if not check.passed:
                lines.append(f"  Remedy: {check.remediation}")
        return "\n".join(lines)

    def require_ready(self) -> None:
        """Raise instead of allowing a caller to fall back to another runtime."""
        if not self.ready:
            raise CLITeamsPreflightError(self)


def _provider_binary(provider: str, command: str) -> str:
    """Return the executable name a provider invocation starts with."""
    if provider == "custom":
        parts = shlex.split(command or "")
        return parts[0] if parts else ""
    return provider


def inspect_cli_teams_preflight(
    *,
    provider: str = "codex",
    command: str = "",
    environment: Mapping[str, str] | None = None,
    which: Which = shutil.which,
) -> CLITeamsPreflightReport:
    """Inspect local prerequisites for ``--runtime-mode cli``.

    Only executable discovery is checked.  Authentication environment
    variables are reported as an informational, always-passing check so the
    operator sees the hint without the preflight claiming to verify login.
    """
    env = os.environ if environment is None else environment
    binary = _provider_binary(provider, command)

    if not binary:
        binary_check = CLIPreflightCheck(
            name="cli_binary",
            passed=False,
            detail=f"provider '{provider}' has no resolvable executable",
            remediation=(
                "Use --cli-provider codex|claude, or pass --cli-command "
                "(CODE_DEFOG_CLI_COMMAND) with a full command template for custom."
            ),
        )
    else:
        found = which(binary)
        binary_check = CLIPreflightCheck(
            name=f"cli_binary:{binary}",
            passed=found is not None,
            detail=(f"found at {found}" if found else f"'{binary}' not found on PATH"),
            remediation=(
                f"Install the '{binary}' CLI and make sure it is on PATH, "
                "or select a different --cli-provider."
            ),
        )

    auth_env = _AUTH_ENV_HINTS.get(provider, ())
    if auth_env:
        present = [name for name in auth_env if env.get(name)]
        auth_detail = (
            f"auth env present: {', '.join(present)}"
            if present
            else f"no auth env among {', '.join(auth_env)}; the CLI may still be logged in"
        )
    else:
        auth_detail = "custom provider: authentication is the command's own concern"
    auth_check = CLIPreflightCheck(
        name="cli_auth_hint",
        passed=True,
        detail=auth_detail,
        remediation="Authenticate with the CLI itself (e.g. 'codex login'); "
        "a missing login surfaces later as a non-zero CLI exit code.",
    )

    return CLITeamsPreflightReport(checks=(binary_check, auth_check))
