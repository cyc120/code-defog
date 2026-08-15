"""Regression tests for the loopback Host guard and CORS narrowing."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from daemon.server import CodeDefogServer
from daemon.store import StateStore


class LoopbackGuardTests(unittest.TestCase):
    """A malicious website (cross-site, non-loopback Host/Origin) must not
    be able to read the service token or any API payload."""

    def _start(self):
        directory = tempfile.TemporaryDirectory()
        store = StateStore(Path(directory.name) / "state.sqlite3")
        server = CodeDefogServer(("127.0.0.1", 0), "guard-test-token", store)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return directory, store, server, thread

    def _get(self, base, path, host=None, origin=None):
        headers = {}
        if host:
            headers["Host"] = host
        if origin:
            headers["Origin"] = origin
        return urlopen(Request(f"{base}{path}", headers=headers), timeout=3)

    def test_non_loopback_host_rejected_on_health(self):
        directory, store, server, thread = self._start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            with self.assertRaises(HTTPError) as ctx:
                self._get(base, "/health", host="evil.example")
            self.assertEqual(ctx.exception.code, 400)
        finally:
            server.shutdown(); server.server_close(); store.close()
            directory.cleanup(); thread.join(timeout=1)

    def test_non_loopback_host_cannot_read_token(self):
        """DNS-rebinding: attacker domain as Host must not yield /ui/config."""
        directory, store, server, thread = self._start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            with self.assertRaises(HTTPError) as ctx:
                self._get(base, "/ui/config", host="evil.example")
            self.assertEqual(ctx.exception.code, 400)
        finally:
            server.shutdown(); server.server_close(); store.close()
            directory.cleanup(); thread.join(timeout=1)

    def test_loopback_host_still_reads_token(self):
        directory, store, server, thread = self._start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            with self._get(base, "/ui/config", host=f"127.0.0.1:{server.server_address[1]}") as resp:
                body = json.loads(resp.read())
            self.assertEqual(body["config"]["token"], "guard-test-token")
        finally:
            server.shutdown(); server.server_close(); store.close()
            directory.cleanup(); thread.join(timeout=1)

    def test_ipv6_bracket_host_with_port_accepted(self):
        """[::1]:<port> must parse as the IPv6 loopback, not be mangled."""
        directory, store, server, thread = self._start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            with self._get(base, "/health", host=f"[::1]:{server.server_address[1]}") as resp:
                body = json.loads(resp.read())
            self.assertEqual(body["ok"], True)
        finally:
            server.shutdown(); server.server_close(); store.close()
            directory.cleanup(); thread.join(timeout=1)

    def test_cross_site_origin_gets_no_cors_header(self):
        directory, store, server, thread = self._start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            with self._get(base, "/health", origin="https://evil.example") as resp:
                body = json.loads(resp.read())
                acao = resp.headers.get("Access-Control-Allow-Origin")
            self.assertEqual(body["ok"], True)
            self.assertIsNone(acao, "cross-site origin must not be reflected")
        finally:
            server.shutdown(); server.server_close(); store.close()
            directory.cleanup(); thread.join(timeout=1)

    def test_null_origin_still_allowed_for_file_pages(self):
        directory, store, server, thread = self._start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            with self._get(base, "/health", origin="null") as resp:
                resp.read()
                acao = resp.headers.get("Access-Control-Allow-Origin")
            self.assertEqual(acao, "null")
        finally:
            server.shutdown(); server.server_close(); store.close()
            directory.cleanup(); thread.join(timeout=1)


if __name__ == "__main__":
    unittest.main()
