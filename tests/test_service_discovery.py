"""Contract tests for the tokenless local Code Defog service registry."""

from __future__ import annotations

import contextlib
import json
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

from daemon.service_discovery import LocalServiceDiscoveryAgent


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        if self.path != "/health":
            self.send_error(404)
            return
        body = json.dumps({
            "ok": True,
            "service": self.server.service_id,
            "instance_id": self.server.instance_id,
            "ui": self.server.ui_available,
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@contextlib.contextmanager
def _healthy_service(
    instance_id: str, *, ui_available: bool = True, service_id: str = "code-defog",
) -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
    server.instance_id = instance_id
    server.ui_available = ui_available
    server.service_id = service_id
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def _unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _descriptor(instance_id: str, port: int, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "instance_id": instance_id,
        "display_name": f"Code Defog {instance_id}",
        "host": "127.0.0.1",
        "port": port,
        "pid": 4242,
        "started_at": "2026-08-06T07:59:00Z",
        "updated_at": "2026-08-06T08:00:00Z",
    }
    payload.update(overrides)
    return payload


class LocalServiceDiscoveryAgentTests(unittest.TestCase):
    def test_register_discover_and_unregister_use_tokenless_registry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = Path(directory) / "services"
            with _healthy_service("alpha-01") as port:
                agent = LocalServiceDiscoveryAgent(registry)
                path = agent.register(_descriptor("alpha-01", port, token="must-not-be-persisted"))

                stored = json.loads(path.read_text(encoding="utf-8"))
                self.assertNotIn("token", stored)

                services = agent.discover()
                self.assertEqual(len(services), 1)
                service = services[0]
                self.assertTrue({
                    "id", "label", "host", "port", "ui_url", "status",
                    "source", "pid", "updated_at",
                }.issubset(service))
                self.assertEqual(service["id"], "alpha-01")
                self.assertEqual(service["label"], "Code Defog alpha-01")
                self.assertEqual(service["host"], "127.0.0.1")
                self.assertEqual(service["port"], port)
                self.assertEqual(service["ui_url"], f"http://127.0.0.1:{port}/ui")
                self.assertEqual(service["status"], "ready")
                self.assertEqual(service["source"], "registry")
                self.assertEqual(service["pid"], 4242)
                self.assertEqual(service["updated_at"], "2026-08-06T08:00:00Z")
                self.assertNotIn("token", service)

                agent.unregister("alpha-01")
                self.assertFalse(path.exists())
                self.assertEqual(agent.discover(), [])

    def test_discover_filters_invalid_or_non_loopback_registry_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = Path(directory) / "services"
            registry.mkdir()
            with _healthy_service("valid-001") as port:
                (registry / "valid.json").write_text(
                    json.dumps(_descriptor("valid-001", port)), encoding="utf-8")
                (registry / "missing-id.json").write_text(
                    json.dumps(_descriptor("", port)), encoding="utf-8")
                (registry / "remote.json").write_text(
                    json.dumps(_descriptor("remote-01", port, host="192.0.2.22")), encoding="utf-8")
                (registry / "bad-port.json").write_text(
                    json.dumps(_descriptor("bad-port-01", 70000)), encoding="utf-8")
                (registry / "wrong-schema.json").write_text(
                    json.dumps(_descriptor("wrong-schema", port, schema_version=2)),
                    encoding="utf-8")
                (registry / "not-json.json").write_text("{", encoding="utf-8")

                services = LocalServiceDiscoveryAgent(registry).discover()

            self.assertEqual([service["id"] for service in services], ["valid-001"])

    def test_register_rejects_malformed_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent = LocalServiceDiscoveryAgent(Path(directory) / "services")

            with self.assertRaises(ValueError):
                agent.register(_descriptor("remote-01", 41000, host="192.0.2.22"))
            with self.assertRaises(ValueError):
                agent.register(_descriptor("short", 41000))

    def test_registry_probe_requires_matching_instance_id_and_ui(self) -> None:
        cases = (("different-01", True), ("expected-01", False))
        for reported_id, ui_available in cases:
            with self.subTest(reported_id=reported_id, ui_available=ui_available):
                with tempfile.TemporaryDirectory() as directory:
                    registry = Path(directory) / "services"
                    with _healthy_service(reported_id, ui_available=ui_available) as port:
                        agent = LocalServiceDiscoveryAgent(registry)
                        agent.register(_descriptor("expected-01", port))
                        services = agent.discover()

                self.assertEqual(len(services), 1)
                self.assertEqual(services[0]["status"], "offline")

    def test_registry_probe_accepts_legacy_health_identity(self) -> None:
        """Old local daemons remain discoverable after the product rename."""
        with tempfile.TemporaryDirectory() as directory:
            registry = Path(directory) / "services"
            with _healthy_service("legacy-01", service_id="code-cctv") as port:
                agent = LocalServiceDiscoveryAgent(registry)
                agent.register(_descriptor("legacy-01", port))
                services = agent.discover()

        self.assertEqual(len(services), 1)
        self.assertEqual(services[0]["status"], "ready")

    def test_legacy_config_is_discoverable_without_returning_its_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy_config = root / "service.json"
            with _healthy_service("legacy-instance") as port:
                legacy_config.write_text(json.dumps({
                    "host": "127.0.0.1",
                    "port": port,
                    "token": "legacy-service-token",
                    "pid": 99,
                    "state_path": "/private/state.sqlite3",
                    "updated_at": "2026-08-06T08:01:00Z",
                }), encoding="utf-8")
                services = LocalServiceDiscoveryAgent(
                    root / "services", legacy_config_path=legacy_config,
                ).discover()

            self.assertEqual(len(services), 1)
            service = services[0]
            self.assertEqual(service["source"], "legacy_config")
            self.assertEqual(service["host"], "127.0.0.1")
            self.assertEqual(service["port"], port)
            self.assertEqual(service["status"], "legacy")
            self.assertEqual(service["ui_url"], f"http://127.0.0.1:{port}/ui")
            self.assertNotIn("token", service)
            self.assertNotIn("state_path", service)
            self.assertNotIn("legacy-service-token", json.dumps(service))
            self.assertNotIn("/private/state.sqlite3", json.dumps(service))

    def test_offline_loopback_service_is_returned_as_offline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = Path(directory) / "services"
            agent = LocalServiceDiscoveryAgent(registry, probe_timeout=0.01)
            agent.register(_descriptor("offline-01", _unused_loopback_port()))

            services = agent.discover()

            self.assertEqual(len(services), 1)
            self.assertEqual(services[0]["id"], "offline-01")
            self.assertEqual(services[0]["status"], "offline")


if __name__ == "__main__":
    unittest.main()
