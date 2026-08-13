"""Regression tests for monitored-project code maps and Code Interpreter Agent.

The suite deliberately builds small user-project fixtures rather than pointing
at this repository.  It guards the core promise that Code Defog visualizes a
selected monitored project, not its own implementation.
"""

from __future__ import annotations

import json
import secrets
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from agent_runtime.harness import DevLoopHarness
from agent_runtime.teams_adapter import AgentScopeExecutionAdapter
from daemon.code_graph import CodeGraphError, build_code_graph, build_node_dossier
from daemon.code_semantics import build_code_interpreter_prompt, interpret_code_dossier
from daemon.llm_providers import LLMProviderStore
from daemon.server import CodeCCTVServer
from daemon.store import StateStore


def _project(root: Path) -> Path:
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "helper.py").write_text(
        "def normalize(value: str) -> str:\n    return value.strip()\n",
        encoding="utf-8",
    )
    (root / "main.py").write_text(
        "from pkg.helper import normalize\n\n"
        "def greet(name: str) -> str:\n"
        "    return normalize(name)\n",
        encoding="utf-8",
    )
    (root / "ui.ts").write_text(
        "import { render } from './view';\nexport function start() { render(); }\n",
        encoding="utf-8",
    )
    (root / "view.ts").write_text("export function render() {}\n", encoding="utf-8")
    return root


class CodeGraphBuilderTests(unittest.TestCase):
    def test_graph_has_relative_nodes_static_imports_and_no_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _project(Path(directory) / "target")
            graph = build_code_graph(root)
            file_nodes = [node for node in graph["nodes"] if node["type"] == "file"]
            self.assertEqual({node["path"] for node in file_nodes}, {
                "main.py", "pkg/__init__.py", "pkg/helper.py", "ui.ts", "view.ts",
            })
            self.assertNotIn("workspace", graph)
            self.assertNotIn("return normalize", json.dumps(graph))
            imports = [edge for edge in graph["edges"] if edge["relation"] == "imports"]
            self.assertTrue(any(edge["evidence"] == "static" for edge in imports))
            self.assertTrue(all("source_context" not in node for node in graph["nodes"]))
            self.assertTrue(graph["graph_fingerprint"])

    def test_symbol_selection_cannot_escape_the_selected_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _project(Path(directory) / "target")
            graph = build_code_graph(root)
            greet = next(node for node in graph["nodes"] if node.get("label") == "greet")
            dossier = build_node_dossier(
                root, graph, greet["id"],
                selection={"path": "main.py", "start_line": 3, "end_line": 4},
                include_preview=True,
            )
            self.assertEqual(dossier["preview"]["text"], "def greet(name: str) -> str:\n    return normalize(name)")
            with self.assertRaises(CodeGraphError):
                build_node_dossier(
                    root, graph, greet["id"],
                    selection={"path": "main.py", "start_line": 1, "end_line": 4},
                )
            with self.assertRaises(CodeGraphError):
                build_node_dossier(root, graph, greet["id"], selection={
                    "path": "../outside.py", "start_line": 1, "end_line": 1,
                })
            with self.assertRaises(CodeGraphError):
                build_node_dossier(root, graph, next(node["id"] for node in graph["nodes"] if node["type"] == "file" and node["path"] == "main.py"), selection={
                    "path": "main.py", "start_line": 1, "end_line": 81,
                })

    def test_source_only_enters_prompt_with_explicit_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _project(Path(directory) / "target")
            graph = build_code_graph(root)
            greet = next(node for node in graph["nodes"] if node.get("label") == "greet")
            metadata_dossier = build_node_dossier(root, graph, greet["id"])
            source_dossier = build_node_dossier(root, graph, greet["id"], include_source=True)
            self.assertNotIn("return normalize", build_code_interpreter_prompt(metadata_dossier))
            self.assertIn("return normalize", build_code_interpreter_prompt(source_dossier, include_source=True))


class CodeInterpreterTests(unittest.TestCase):
    def test_interpreter_uses_the_active_shared_provider(self) -> None:
        """Node explanations must follow the same provider switch as summaries."""
        with tempfile.TemporaryDirectory() as directory:
            root = _project(Path(directory) / "target")
            graph = build_code_graph(root)
            greet = next(node for node in graph["nodes"] if node.get("label") == "greet")
            dossier = build_node_dossier(root, graph, greet["id"])
            providers = LLMProviderStore(
                Path(directory) / "providers.json", legacy_key_loader=lambda: "",
            )
            providers.save_and_activate({
                "provider_id": "openai",
                "base_url": "https://api.openai.com/v1",
                "model": "gpt-4o-mini",
                "api_key": "shared-provider-test-key",
            })
            from daemon import code_semantics

            seen: dict[str, object] = {}
            original_post = code_semantics._post_chat
            try:
                def fake_post(api_key: str, _prompt: str, **kwargs: object) -> str:
                    seen["api_key"] = api_key
                    seen["provider"] = kwargs.get("provider")
                    return json.dumps({
                        "role": "名称格式化函数", "certainty": "confirmed",
                        "responsibilities": ["清理字符串"], "inputs_outputs": [],
                        "collaborators": [], "flow": [], "risks": [],
                        "evidence_refs": ["E1"], "limitations": [],
                    })

                code_semantics._post_chat = fake_post
                response = interpret_code_dossier(dossier, provider_store=providers)
            finally:
                code_semantics._post_chat = original_post

        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["provider"], "openai")
        self.assertEqual(response["model"], "gpt-4o-mini")
        self.assertEqual(seen["api_key"], "shared-provider-test-key")
        self.assertEqual((seen["provider"] or {})["base_url"], "https://api.openai.com/v1")

    def test_normalizer_rejects_unknown_neighbor_and_evidence_refs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _project(Path(directory) / "target")
            graph = build_code_graph(root)
            greet = next(node for node in graph["nodes"] if node.get("label") == "greet")
            dossier = build_node_dossier(root, graph, greet["id"])
            from daemon import code_semantics

            original_post = code_semantics._post_chat
            try:
                code_semantics._post_chat = lambda *_args, **_kwargs: json.dumps({
                    "role": "名字清理入口",
                    "certainty": "confirmed",
                    "responsibilities": ["规范化输入"],
                    "inputs_outputs": ["字符串到字符串"],
                    "collaborators": [{
                        "node_id": "invented", "relationship": "伪造", "evidence_refs": ["E99"],
                    }],
                    "flow": ["调用依赖"], "risks": [],
                    "evidence_refs": ["E1", "E99"], "limitations": ["运行时待确认"],
                })
                class _Provider:
                    def resolve_active(self):
                        return {"id": "test", "name": "Test", "model": "model", "api_key": "k", "json_mode": True, "base_url": "http://127.0.0.1"}
                response = interpret_code_dossier(dossier, provider_store=_Provider())
            finally:
                code_semantics._post_chat = original_post
            self.assertEqual(response["status"], "ok")
            self.assertEqual(response["semantic"]["evidence_refs"], ["E1"])
            self.assertEqual(response["semantic"]["collaborators"], [])


class CodeGraphEndpointTests(unittest.TestCase):
    def _start(self, store: StateStore, **kwargs: object) -> tuple[CodeCCTVServer, str, str]:
        token = secrets.token_hex(16)
        server = CodeCCTVServer(("127.0.0.1", 0), token, store, **kwargs)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, f"http://127.0.0.1:{server.server_address[1]}", token

    @staticmethod
    def _request(base: str, workspace: str, token: str | None, suffix: str = "/code-graph", body: dict | None = None) -> Request:
        headers: dict[str, str] = {}
        if token:
            headers["X-Code-CCTV-Token"] = token
        if body is not None:
            headers["Content-Type"] = "application/json"
        return Request(
            f"{base}/api/projects/{quote(workspace, safe='')}{suffix}",
            headers=headers,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            method="POST" if body is not None else "GET",
        )

    def test_endpoint_requires_registered_project_and_redacts_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _project(Path(directory) / "target")
            store = StateStore(Path(directory) / "state.sqlite3")
            registered = store.register_monitored_project({"workspace": str(root), "kind": "process"})
            server, base, token = self._start(store)
            try:
                with self.assertRaises(HTTPError) as unauthenticated:
                    urlopen(self._request(base, registered["workspace"], None), timeout=3)
                self.assertEqual(unauthenticated.exception.code, 401)
                with self.assertRaises(HTTPError) as unknown:
                    urlopen(self._request(base, str(Path(directory) / "unregistered"), token), timeout=3)
                self.assertEqual(unknown.exception.code, 404)
                with urlopen(self._request(base, registered["workspace"], token), timeout=3) as response:
                    payload = json.loads(response.read())
                self.assertTrue(payload["ok"])
                dumped = json.dumps(payload, ensure_ascii=False)
                self.assertNotIn("return normalize", dumped)
                self.assertNotIn(str(root), dumped)
                self.assertTrue(payload["graph"]["nodes"])
            finally:
                server.shutdown(); server.server_close(); store.close()

    def test_interpret_returns_preview_only_on_request_and_uses_cache(self) -> None:
        calls = {"count": 0}

        def interpreter(dossier: dict, *, include_source: bool = False) -> dict:
            calls["count"] += 1
            return {
                "status": "ok", "fingerprint": dossier["fingerprint"],
                "source_included": include_source,
                "semantic": {"role": "测试角色", "certainty": "confirmed", "responsibilities": [], "inputs_outputs": [], "collaborators": [], "flow": [], "risks": [], "evidence_refs": ["E1"], "limitations": []},
            }

        with tempfile.TemporaryDirectory() as directory:
            root = _project(Path(directory) / "target")
            store = StateStore(Path(directory) / "state.sqlite3")
            registered = store.register_monitored_project({"workspace": str(root), "kind": "process"})
            server, base, token = self._start(store, code_interpreter_fn=interpreter)
            try:
                with urlopen(self._request(base, registered["workspace"], token), timeout=3) as response:
                    graph = json.loads(response.read())["graph"]
                node = next(item for item in graph["nodes"] if item.get("label") == "greet")
                request = self._request(
                    base, registered["workspace"], token, "/code-graph/interpret",
                    {"node_id": node["id"]},
                )
                with urlopen(request, timeout=3) as response:
                    first = json.loads(response.read())
                with urlopen(request, timeout=3) as response:
                    second = json.loads(response.read())
                preview_request = self._request(
                    base, registered["workspace"], token, "/code-graph/interpret",
                    {"node_id": node["id"], "include_preview": True},
                )
                with urlopen(preview_request, timeout=3) as response:
                    preview = json.loads(response.read())
                self.assertTrue(first["ok"])
                self.assertNotIn("preview", first["dossier"])
                self.assertNotIn("source_context", first["dossier"])
                self.assertFalse(first["cached"])
                self.assertTrue(second["cached"])
                self.assertEqual(calls["count"], 1)
                self.assertNotIn("return normalize", json.dumps(first))
                self.assertIn("return normalize", json.dumps(preview))
                self.assertNotIn("source_context", preview["dossier"])
            finally:
                server.shutdown(); server.server_close(); store.close()

    def test_project_change_invalidation_forces_fresh_semantic_explanation(self) -> None:
        calls = {"count": 0}

        def interpreter(dossier: dict, *, include_source: bool = False) -> dict:
            calls["count"] += 1
            return {
                "status": "ok", "fingerprint": dossier["fingerprint"], "source_included": include_source,
                "semantic": {"role": "测试角色", "certainty": "confirmed", "responsibilities": [], "inputs_outputs": [], "collaborators": [], "flow": [], "risks": [], "evidence_refs": ["E1"], "limitations": []},
            }

        with tempfile.TemporaryDirectory() as directory:
            root = _project(Path(directory) / "target")
            store = StateStore(Path(directory) / "state.sqlite3")
            registered = store.register_monitored_project({"workspace": str(root), "kind": "process"})
            server, base, token = self._start(store, code_interpreter_fn=interpreter)
            try:
                with urlopen(self._request(base, registered["workspace"], token), timeout=3) as response:
                    graph = json.loads(response.read())["graph"]
                node = next(item for item in graph["nodes"] if item.get("label") == "greet")
                request = self._request(base, registered["workspace"], token, "/code-graph/interpret", {"node_id": node["id"]})
                with urlopen(request, timeout=3) as response:
                    self.assertFalse(json.loads(response.read())["cached"])
                server.invalidate_code_graph_cache(registered["workspace"])
                with urlopen(request, timeout=3) as response:
                    self.assertFalse(json.loads(response.read())["cached"])
                self.assertEqual(calls["count"], 2)
            finally:
                server.shutdown(); server.server_close(); store.close()

    def test_console_has_opt_in_code_map_and_agent_action(self) -> None:
        console = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn('data-view="code-map"', console)
        self.assertIn('code-map-source-consent', console)
        self.assertIn("让 Agent 解读", console)
        self.assertIn('interpret: options.interpret !== false', console)
        self.assertIn("/code-graph/interpret", console)

    def test_harness_is_the_default_interpreter_dispatch_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _project(Path(directory) / "target")
            store = StateStore(Path(directory) / "state.sqlite3")
            registered = store.register_monitored_project({"workspace": str(root), "kind": "process"})
            harness = DevLoopHarness(AgentScopeExecutionAdapter(store))
            providers = LLMProviderStore(Path(directory) / "providers.json", legacy_key_loader=lambda: "")
            server, base, token = self._start(store, harness=harness, llm_provider_store=providers)
            try:
                with urlopen(self._request(base, registered["workspace"], token), timeout=3) as response:
                    graph = json.loads(response.read())["graph"]
                node = next(item for item in graph["nodes"] if item.get("label") == "greet")
                with urlopen(self._request(
                    base, registered["workspace"], token, "/code-graph/interpret",
                    {"node_id": node["id"]},
                ), timeout=3) as response:
                    payload = json.loads(response.read())
                self.assertEqual(payload["interpreter"]["agent"], "code_interpreter")
                self.assertEqual(payload["interpreter"]["runtime_kind"], "local_bounded_code_interpreter")
                self.assertIn(payload["interpreter"]["status"], {"unavailable", "ok", "error"})
            finally:
                server.shutdown(); server.server_close(); store.close()


if __name__ == "__main__":
    unittest.main()
