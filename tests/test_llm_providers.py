"""Offline regression coverage for local LLM provider configuration."""

from __future__ import annotations

import json
import secrets
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from daemon import llm_summary
from daemon import server as server_module
from daemon.llm_providers import LLMProviderStore
from daemon.server import CodeCCTVServer
from daemon.store import StateStore


class LLMProviderStoreTests(unittest.TestCase):
    def test_public_config_redacts_keys_and_writes_restricted_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private" / "providers.json"
            store = LLMProviderStore(path, legacy_key_loader=lambda: "legacy-test-key")
            initial = store.public_config()
            deepseek = next(item for item in initial["providers"] if item["id"] == "deepseek")
            self.assertTrue(deepseek["configured"])
            self.assertEqual(deepseek["key_source"], "环境变量兼容回退")
            self.assertNotIn("legacy-test-key", json.dumps(initial))

            public = store.save_and_activate({
                "provider_id": "openai",
                "base_url": "https://api.openai.com/v1",
                "model": "gpt-4o-mini",
                "api_key": "saved-test-key",
            })
            self.assertEqual(public["active_provider"], "openai")
            self.assertNotIn("saved-test-key", json.dumps(public))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(store.resolve_active()["api_key"], "saved-test-key")

    def test_candidate_never_persists_one_time_key_and_rejects_remote_http(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LLMProviderStore(Path(directory) / "providers.json", legacy_key_loader=lambda: "")
            with self.assertRaisesRegex(ValueError, "local provider"):
                store.resolve_candidate({
                    "provider_id": "custom",
                    "base_url": "http://example.invalid/v1",
                    "model": "demo",
                })
            candidate = store.resolve_candidate({
                "provider_id": "ollama",
                "base_url": "http://127.0.0.1:11434/v1",
                "model": "qwen2.5",
                "api_key": "one-time-test-key",
            })
            self.assertEqual(candidate["api_key"], "one-time-test-key")
            self.assertNotIn("one-time-test-key", json.dumps(store.public_config()))

    def test_candidate_refuses_to_reuse_stored_key_against_foreign_host(self) -> None:
        """A saved DeepSeek key must never be sent to an overridden https host."""
        with tempfile.TemporaryDirectory() as directory:
            store = LLMProviderStore(
                Path(directory) / "providers.json", legacy_key_loader=lambda: "legacy-secret-key",
            )
            # Reusing the legacy/environment key against DeepSeek's own host is safe.
            candidate = store.resolve_candidate({
                "provider_id": "deepseek",
                "base_url": "https://api.deepseek.com",
                "model": "deepseek-chat",
            })
            self.assertEqual(candidate["api_key"], "legacy-secret-key")

            # Same key against an arbitrary https host is exfiltration → refuse.
            with self.assertRaisesRegex(ValueError, "explicit api_key"):
                store.resolve_candidate({
                    "provider_id": "deepseek",
                    "base_url": "https://attacker.example.com",
                    "model": "deepseek-chat",
                })

            # Supplying an explicit one-time key for the same foreign host is allowed.
            candidate = store.resolve_candidate({
                "provider_id": "deepseek",
                "base_url": "https://attacker.example.com",
                "model": "deepseek-chat",
                "api_key": "explicit-one-time-key",
            })
            self.assertEqual(candidate["api_key"], "explicit-one-time-key")

    def test_save_refuses_silent_key_repointing_to_foreign_host(self) -> None:
        """save_and_activate must not keep the old key when the endpoint
        host moves outside the provider's preset hosts."""
        with tempfile.TemporaryDirectory() as directory:
            store = LLMProviderStore(
                Path(directory) / "providers.json", legacy_key_loader=lambda: "",
            )
            store.save_and_activate({
                "provider_id": "deepseek",
                "base_url": "https://api.deepseek.com",
                "model": "deepseek-chat",
                "api_key": "stored-secret",
            })
            # Repointing to a foreign host without an explicit key is refused.
            with self.assertRaisesRegex(ValueError, "explicit api_key"):
                store.save_and_activate({
                    "provider_id": "deepseek",
                    "base_url": "https://attacker.example.com",
                    "model": "deepseek-chat",
                })
            # The stored key must still target the original host.
            resolved = store.resolve_active()
            self.assertEqual(resolved["base_url"], "https://api.deepseek.com")
            self.assertEqual(resolved["api_key"], "stored-secret")
            # Supplying an explicit key for the new host is allowed.
            store.save_and_activate({
                "provider_id": "deepseek",
                "base_url": "https://attacker.example.com",
                "model": "deepseek-chat",
                "api_key": "new-host-key",
            })
            self.assertEqual(store.resolve_active()["api_key"], "new-host-key")
            # Preset-host saves without a key (env fallback) stay allowed.
            store2 = LLMProviderStore(
                Path(directory) / "p2.json", legacy_key_loader=lambda: "env-key",
            )
            store2.save_and_activate({
                "provider_id": "deepseek",
                "base_url": "https://api.deepseek.com",
                "model": "deepseek-chat",
            })
            self.assertEqual(store2.resolve_active()["api_key"], "env-key")

    def test_candidate_reuses_stored_key_against_its_saved_host(self) -> None:
        """A saved key for a custom provider may be reused against that provider's
        own persisted base_url, but not silently against a different one."""
        with tempfile.TemporaryDirectory() as directory:
            store = LLMProviderStore(Path(directory) / "providers.json", legacy_key_loader=lambda: "")
            store.save_and_activate({
                "provider_id": "custom",
                "base_url": "https://gateway.corp.example/v1",
                "model": "model-x",
                "api_key": "corp-key",
            })
            # No base_url override → resolves to the persisted host, key reused.
            candidate = store.resolve_candidate({"provider_id": "custom", "model": "model-x"})
            self.assertEqual(candidate["api_key"], "corp-key")
            self.assertEqual(candidate["base_url"], "https://gateway.corp.example/v1")
            # Overriding to a different host while reusing the key is refused.
            with self.assertRaisesRegex(ValueError, "explicit api_key"):
                store.resolve_candidate({
                    "provider_id": "custom",
                    "base_url": "https://other.corp.example/v1",
                    "model": "model-x",
                })


class LLMProviderTransportTests(unittest.TestCase):
    def test_summary_uses_selected_provider_model_and_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LLMProviderStore(Path(directory) / "providers.json", legacy_key_loader=lambda: "")
            store.save_and_activate({
                "provider_id": "openai",
                "base_url": "https://gateway.example.test/v1",
                "model": "model-test",
                "api_key": "transport-test-key",
            })
            original = llm_summary._post_chat
            seen: dict[str, object] = {}
            try:
                def fake_post(key: str, prompt: str, timeout: float = 30.0,
                              system_prompt: str | None = None, *, provider: dict | None = None) -> str:
                    seen.update({"key": key, "provider": provider, "system": system_prompt})
                    return json.dumps({"overall_status": "可用", "top_priorities": []})

                llm_summary._post_chat = fake_post
                result = llm_summary.generate_project_summary(
                    {"totals": {"cases": 1}}, provider_store=store,
                )
            finally:
                llm_summary._post_chat = original
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["provider"], "openai")
            self.assertEqual(result["model"], "model-test")
            self.assertEqual(seen["key"], "transport-test-key")
            self.assertEqual((seen["provider"] or {})["base_url"], "https://gateway.example.test/v1")


class LLMProviderPostChatWireTests(unittest.TestCase):
    """Exercise the real _post_chat transport against a loopback HTTP server.

    This is the only function that sends the API key over the network, so its
    request construction, Authorization header, JSON body and response parsing
    are pinned here rather than monkeypatched away.
    """

    def _serve(self, handler) -> tuple[object, str]:
        import http.server

        server = http.server.HTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, f"http://127.0.0.1:{server.server_address[1]}"

    def test_post_chat_builds_request_and_parses_response(self) -> None:
        import http.server

        captured: dict[str, object] = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                captured["path"] = self.path
                captured["auth"] = self.headers.get("Authorization")
                captured["content_type"] = self.headers.get("Content-Type")
                captured["body"] = json.loads(self.rfile.read(length))
                payload = json.dumps(
                    {"choices": [{"message": {"content": '{"ok":true}'}}]}
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                return

        server, base = self._serve(Handler)
        try:
            content = llm_summary._post_chat(
                "secret-key",
                "hello",
                system_prompt="you are a test",
                provider={
                    "id": "custom",
                    "base_url": base,
                    "model": "model-wire",
                    "json_mode": True,
                    "api_key": "secret-key",
                },
            )
            self.assertEqual(content, '{"ok":true}')
            self.assertEqual(captured["path"], "/chat/completions")
            self.assertEqual(captured["auth"], "Bearer secret-key")
            self.assertEqual(captured["content_type"], "application/json")
            body = captured["body"]
            self.assertEqual(body["model"], "model-wire")
            self.assertEqual(body["messages"][0]["role"], "system")
            self.assertEqual(body["messages"][0]["content"], "you are a test")
            self.assertEqual(body["messages"][1]["role"], "user")
            self.assertEqual(body["messages"][1]["content"], "hello")
            self.assertEqual(body["response_format"], {"type": "json_object"})
        finally:
            server.shutdown(); server.server_close()

    def test_post_chat_omits_response_format_when_json_mode_off(self) -> None:
        import http.server

        captured: dict[str, object] = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                captured["body"] = json.loads(self.rfile.read(length))
                payload = b'{"choices": [{"message": {"content": "plain text"}}]}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                return

        server, base = self._serve(Handler)
        try:
            content = llm_summary._post_chat(
                "k", "hi", provider={"id": "ollama", "base_url": base,
                                     "model": "llama", "json_mode": False, "api_key": "k"},
            )
            self.assertEqual(content, "plain text")
            self.assertNotIn("response_format", captured["body"])
        finally:
            server.shutdown(); server.server_close()


class LLMProviderEndpointTests(unittest.TestCase):
    def _start(self, store: StateStore, providers: LLMProviderStore) -> tuple[CodeCCTVServer, str, str]:
        token = secrets.token_hex(16)
        server = CodeCCTVServer(
            ("127.0.0.1", 0), token, store,
            llm_provider_store=providers,
            llm_summary_fn=lambda stats, refresh=False, cache=None, key="default": {
                "status": "ok", "summary": {"overall_status": "test"},
            },
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, f"http://127.0.0.1:{server.server_address[1]}", token

    @staticmethod
    def _request(base_url: str, token: str | None, path: str, body: dict | None = None) -> Request:
        headers: dict[str, str] = {}
        if token:
            headers["X-Code-CCTV-Token"] = token
        if body is not None:
            headers["Content-Type"] = "application/json"
        return Request(
            base_url + path,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers=headers,
            method="POST" if body is not None else "GET",
        )

    def test_settings_api_requires_auth_and_redacts_saved_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite3")
            providers = LLMProviderStore(Path(directory) / "providers.json", legacy_key_loader=lambda: "")
            server, base_url, token = self._start(state, providers)
            try:
                with self.assertRaises(HTTPError) as unauthenticated:
                    urlopen(self._request(base_url, None, "/api/llm/providers"), timeout=3)
                self.assertEqual(unauthenticated.exception.code, 401)

                server.summary_cache["llm:demo"] = {"status": "ok"}
                server.assistant_cache["answer"] = (1.0, {"status": "ok"})
                payload = {
                    "provider_id": "openai",
                    "base_url": "https://api.openai.com/v1",
                    "model": "gpt-4o-mini",
                    "api_key": "endpoint-test-key",
                }
                with urlopen(self._request(base_url, token, "/api/llm/providers", payload), timeout=3) as response:
                    body = json.loads(response.read())
                self.assertTrue(body["ok"])
                self.assertEqual(body["llm"]["active_provider"], "openai")
                self.assertNotIn("endpoint-test-key", json.dumps(body))
                self.assertEqual(server.summary_cache, {})
                self.assertEqual(server.assistant_cache, {})
            finally:
                server.shutdown(); server.server_close(); state.close()

    def test_connection_test_can_use_one_time_key_without_returning_or_saving_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(Path(directory) / "state.sqlite3")
            providers = LLMProviderStore(Path(directory) / "providers.json", legacy_key_loader=lambda: "")
            server, base_url, token = self._start(state, providers)
            original = server_module.test_llm_provider
            try:
                server_module.test_llm_provider = lambda candidate: {
                    "status": "ok", "provider": candidate["id"], "model": candidate["model"],
                }
                payload = {
                    "provider_id": "ollama",
                    "base_url": "http://127.0.0.1:11434/v1",
                    "model": "qwen2.5",
                    "api_key": "one-time-endpoint-key",
                }
                with urlopen(self._request(base_url, token, "/api/llm/providers/test", payload), timeout=3) as response:
                    body = json.loads(response.read())
                self.assertTrue(body["ok"])
                self.assertNotIn("one-time-endpoint-key", json.dumps(body))
                self.assertNotIn("one-time-endpoint-key", json.dumps(providers.public_config()))
            finally:
                server_module.test_llm_provider = original
                server.shutdown(); server.server_close(); state.close()


class LLMProviderConsoleTests(unittest.TestCase):
    def test_console_has_provider_settings_without_key_local_storage(self) -> None:
        console = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="llm-settings-drawer"', console)
        self.assertIn('id="llm-provider-select"', console)
        self.assertIn('id="llm-api-key"', console)
        self.assertIn('/api/llm/providers/test', console)
        section = console[console.index("// ── 本地 LLM 设置"):console.index("function cachedServices")]
        self.assertNotIn("localStorage.", section)
        self.assertIn('$("llm-api-key").value = ""', section)


if __name__ == "__main__":
    unittest.main()
