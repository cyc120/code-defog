"""HTTP contract tests for the local service-discovery dashboard."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import urlopen

from daemon.dashboard import DashboardServer
from daemon.service_discovery import LocalServiceDiscoveryAgent


class DashboardServerTests(unittest.TestCase):
    def test_discovery_dashboard_serves_tokenless_picker_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent = LocalServiceDiscoveryAgent(Path(directory) / "services")
            server = DashboardServer(("127.0.0.1", 0), agent)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with urlopen(f"{base_url}/ui/config", timeout=1) as response:
                    config = json.loads(response.read())
                    self.assertIsNone(response.headers.get("Access-Control-Allow-Origin"))
                self.assertEqual(config, {"ok": True, "mode": "discovery"})

                with urlopen(f"{base_url}/ui/services", timeout=1) as response:
                    payload = json.loads(response.read())
                    self.assertIsNone(response.headers.get("Access-Control-Allow-Origin"))
                self.assertEqual(payload["agent"], "local-service-discovery")
                self.assertEqual(payload["services"], [])
                self.assertNotIn("token", json.dumps(payload))

                with urlopen(f"{base_url}/ui", timeout=1) as response:
                    page = response.read().decode("utf-8")
                self.assertIn('id="service-list"', page)
                self.assertIn('id="connect-btn"', page)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=1)


if __name__ == "__main__":
    unittest.main()
