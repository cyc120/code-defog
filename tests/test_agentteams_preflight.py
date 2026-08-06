"""Tests for the fail-closed boundary around external AgentTeams requests."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_runtime.agentteams_preflight import (
    AgentTeamsPreflightError,
    inspect_agentteams_preflight,
)
from agent_runtime.teams_adapter import AgentScopeExecutionAdapter, AgentTeamsAdapter


class AgentTeamsPreflightTests(unittest.TestCase):
    home = Path("/isolated/home")

    @staticmethod
    def _which(paths: dict[str, str]):
        return lambda executable: paths.get(executable)

    def test_missing_agt_and_docker_are_reported_separately(self) -> None:
        report = inspect_agentteams_preflight(
            environment={}, which=self._which({}), home=self.home,
        )
        checks = {check.name: check for check in report.checks}

        self.assertFalse(report.ready)
        self.assertIn("`agt` was not found", checks["agt CLI"].detail)
        self.assertIn("`docker` was not found", checks["Docker CLI"].detail)
        self.assertIn("not checked", checks["Docker daemon"].detail)
        with self.assertRaises(AgentTeamsPreflightError) as raised:
            report.require_ready()
        self.assertIn("AgentTeams preflight: BLOCKED", str(raised.exception))

    def test_missing_local_docker_socket_blocks_preflight_without_docker_call(self) -> None:
        checked_paths: list[Path] = []

        def socket_available(path: Path) -> bool:
            checked_paths.append(path)
            return False

        report = inspect_agentteams_preflight(
            environment={},
            which=self._which({"agt": "/opt/bin/agt", "docker": "/opt/bin/docker"}),
            socket_available=socket_available,
            home=self.home,
        )
        daemon = next(check for check in report.checks if check.name == "Docker daemon")

        self.assertFalse(report.ready)
        self.assertIn("no local Docker Unix socket", daemon.detail)
        self.assertEqual(
            checked_paths,
            [self.home / ".docker" / "run" / "docker.sock", Path("/var/run/docker.sock")],
        )

    def test_local_socket_allows_only_the_local_prerequisites_to_pass(self) -> None:
        local_socket = Path("/tmp/agentteams-docker.sock")
        report = inspect_agentteams_preflight(
            environment={"DOCKER_HOST": f"unix://{local_socket}"},
            which=self._which({"agt": "/opt/bin/agt", "docker": "/opt/bin/docker"}),
            socket_available=lambda path: path == local_socket,
            home=self.home,
        )

        self.assertTrue(report.ready)
        self.assertIn("connectivity was not probed", report.format_text())

    def test_remote_docker_host_is_not_contacted(self) -> None:
        def socket_available(_path: Path) -> bool:
            raise AssertionError("remote Docker host must not trigger a socket probe")

        report = inspect_agentteams_preflight(
            environment={"DOCKER_HOST": "tcp://docker.example.test:2376"},
            which=self._which({"agt": "/opt/bin/agt", "docker": "/opt/bin/docker"}),
            socket_available=socket_available,
            home=self.home,
        )
        daemon = next(check for check in report.checks if check.name == "Docker daemon")

        self.assertFalse(report.ready)
        self.assertIn("intentionally not contacted", daemon.detail)

    def test_agentteams_runtime_request_stops_before_local_store_or_agentscope(self) -> None:
        blocked = inspect_agentteams_preflight(
            environment={}, which=self._which({}), home=self.home,
        )
        with (
            patch.object(sys, "argv", ["serve.py", "--runtime-mode", "agentteams"]),
            patch("daemon.serve.inspect_agentteams_preflight", return_value=blocked),
            patch("daemon.serve.StateStore") as state_store,
        ):
            from daemon import serve

            with self.assertRaises(SystemExit) as raised:
                serve.main()

        self.assertEqual(raised.exception.code, 2)
        state_store.assert_not_called()

    def test_agentteams_runtime_stops_when_the_workflow_bridge_is_unconfigured(self) -> None:
        ready = inspect_agentteams_preflight(
            environment={"DOCKER_HOST": "unix:///tmp/docker.sock"},
            which=self._which({"agt": "/opt/bin/agt", "docker": "/opt/bin/docker"}),
            socket_available=lambda path: path == Path("/tmp/docker.sock"),
            home=self.home,
        )
        self.assertTrue(ready.ready)
        with (
            patch.object(sys, "argv", ["serve.py", "--runtime-mode", "agentteams"]),
            patch("daemon.serve.inspect_agentteams_preflight", return_value=ready),
            patch("daemon.serve.StateStore") as state_store,
        ):
            from daemon import serve

            with self.assertRaises(SystemExit) as raised:
                serve.main()

        self.assertEqual(raised.exception.code, 2)
        state_store.assert_not_called()

    def test_legacy_adapter_name_is_only_an_agentscope_alias(self) -> None:
        self.assertIs(AgentTeamsAdapter, AgentScopeExecutionAdapter)


if __name__ == "__main__":
    unittest.main()
