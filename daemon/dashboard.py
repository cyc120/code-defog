#!/usr/bin/env python3
"""Start a local, tokenless dashboard launcher for service discovery."""

from __future__ import annotations

import argparse
import json
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import paths
from .service_discovery import LocalServiceDiscoveryAgent


UI_DIR = Path(__file__).resolve().parent.parent / "web"


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], discovery_agent: LocalServiceDiscoveryAgent) -> None:
        super().__init__(address, DashboardHandler)
        self.discovery_agent = discovery_agent


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        return

    def send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(body)

    def serve_ui(self) -> None:
        try:
            content = (UI_DIR / "index.html").read_text(encoding="utf-8")
        except OSError:
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        body = content.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(body)

    _LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

    def _host_allowed(self) -> bool:
        host_header = (self.headers.get("Host") or "").strip()
        if not host_header:
            return False
        try:
            # urlparse handles IPv6 brackets and ports correctly.
            parsed = urlparse(f"//{host_header}")
        except ValueError:
            return False
        host = (parsed.hostname or "").lower().rstrip(".")
        return host in self._LOOPBACK_HOSTS

    def do_GET(self) -> None:
        route = urlparse(self.path).path
        if not self._host_allowed():
            self.send_json({"error": "invalid Host header"}, HTTPStatus.BAD_REQUEST)
            return
        if route in ("/", "/ui", "/ui/"):
            self.serve_ui()
            return
        if route == "/ui/config":
            self.send_json({"ok": True, "mode": "discovery"})
            return
        if route == "/ui/services":
            self.send_json({"ok": True, "agent": "local-service-discovery", "services": self.server.discovery_agent.discover()})
            return
        if route == "/health":
            self.send_json({"ok": True, "service": "code-defog-dashboard"})
            return
        self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Code Defog local service discovery dashboard.")
    parser.add_argument("--port", type=int, default=0, help="Use 0 to select a free local port.")
    parser.add_argument("--registry-dir", type=Path, default=paths.service_registry_dir())
    parser.add_argument("--legacy-config", type=Path, default=paths.config_path())
    parser.add_argument("--open", action="store_true", help="Open the dashboard URL in the default browser.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    agent = LocalServiceDiscoveryAgent(args.registry_dir, args.legacy_config)
    server = DashboardServer(("127.0.0.1", args.port), agent)
    host, port = server.server_address
    url = f"http://{host}:{port}/ui"
    print(f"Code Defog service discovery dashboard: {url}", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
