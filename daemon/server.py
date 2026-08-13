#!/usr/bin/env python3
"""Threaded localhost HTTP and SSE server for Code Defog — extended with DevLoop Case API."""

from __future__ import annotations

import getpass
import hashlib
import hmac
import json
import os
import queue
import secrets
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import urlparse

from .code_graph import CodeGraphError, build_code_graph, build_node_dossier
from .code_semantics import interpret_code_dossier
from .store import StateStore, ALL_GRANTED_ACTIONS, APPROVAL_ACTIONS, REJECT_ACTIONS, clean_text
from .llm_providers import LLMProviderStore
from .llm_summary import (
    get_llm_summary,
    generate_project_assistant_reply,
    normalize_project_assistant_history,
    test_llm_provider,
)


MAX_BODY_BYTES = 1_000_000
TOKEN_TYPE_SERVICE = "service"
TOKEN_TYPE_APPROVAL = "approval"
ASSISTANT_CACHE_TTL_S = 30.0
ASSISTANT_CACHE_MAX_ENTRIES = 128
CODE_GRAPH_CACHE_TTL_S = 8.0
CODE_GRAPH_CACHE_MAX_ENTRIES = 12
CODE_SEMANTIC_CACHE_MAX_ENTRIES = 160


class CodeDefogServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self, address: tuple[str, int], token: str, store: StateStore,
        orchestrator: Any | None = None, ui_dir: str | None = None,
        discovery_agent: Any | None = None, instance_id: str | None = None,
        approval_secret: str | None = None, runtime_mode: str = "mock",
        llm_summary_fn: Any | None = None,
        project_discovery_agent: Any | None = None,
        project_monitor: Any | None = None,
        drive_runner: Any | None = None,
        harness: Any | None = None,
        llm_chat_fn: Any | None = None,
        llm_provider_store: LLMProviderStore | None = None,
        code_graph_builder: Any | None = None,
        code_interpreter_fn: Any | None = None,
    ) -> None:
        super().__init__(address, CodeDefogHandler)
        self.token = token
        self.store = store
        self.orchestrator = orchestrator
        self.ui_dir = ui_dir
        self.discovery_agent = discovery_agent
        self.instance_id = instance_id
        # This second factor is deliberately not included in /ui/config or the
        # service descriptor. A service-token holder therefore cannot issue
        # its own approval Grants.
        self.approval_secret = approval_secret or secrets.token_urlsafe(32)
        self.runtime_mode = runtime_mode
        # Inject a fake summarizer in tests; default to the TTL-cached wrapper.
        # The cache lives server-side so SSE-triggered stat refreshes never
        # re-run the LLM until TTL expiry or an explicit ?refresh=1.
        self.summary_cache: dict[str, Any] = {}
        self.llm_provider_store = llm_provider_store or LLMProviderStore()
        self.code_graph_builder = code_graph_builder or build_code_graph
        if llm_summary_fn is None:
            self.llm_summary_fn = lambda stats, refresh=False, cache=None, key="default": get_llm_summary(
                stats, refresh=refresh, cache=cache, key=key,
                provider_store=self.llm_provider_store,
            )
        else:
            self.llm_summary_fn = llm_summary_fn
        # Local project discovery + monitoring (enterprise milestone 1).
        self.project_discovery_agent = project_discovery_agent
        self.project_monitor = project_monitor
        # Automated drive runner (browse + test + static-scan + LLM summary).
        self.drive_runner = drive_runner
        self.harness = harness
        # A separate injection point keeps project-assistant HTTP tests fully
        # offline and independent from the dashboard summary generator.
        if llm_chat_fn is None:
            self.llm_chat_fn = lambda question, project, stats, latest_drive, history: generate_project_assistant_reply(
                question, project, stats, latest_drive, history,
                provider_store=self.llm_provider_store,
            )
        else:
            self.llm_chat_fn = llm_chat_fn
        # Short-lived answers make retries and accidental double submits fast
        # without storing any conversation on disk. The key includes the
        # bounded context snapshot, so project changes naturally invalidate it.
        self.assistant_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self.assistant_cache_lock = Lock()
        # Graph data is local deterministic metadata.  It expires quickly and
        # ProjectMonitor explicitly invalidates it on file or Git changes.
        self.code_graph_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self.code_graph_cache_lock = Lock()
        # Semantic cache never stores source snippets, only normalized Agent
        # JSON. Provider/model and dossier fingerprints are part of its key.
        self.code_semantic_cache: dict[str, dict[str, Any]] = {}
        self.code_semantic_cache_lock = Lock()
        # Tests may inject a deterministic interpreter. Production requests
        # otherwise flow through the Harness so this Agent remains visible in
        # the single local coordination boundary.
        self.code_interpreter_fn = code_interpreter_fn
        self.started_at = time.monotonic()
        self.subscribers: set[queue.Queue[dict[str, Any]]] = set()
        self.subscriber_lock = Lock()
        # Wire store → SSE so every Case event reaches all subscribers
        self.store.publish_callback = self.publish

    def management_info(self) -> dict[str, Any]:
        payload = self.store.info()
        payload.update({
            "pid": os.getpid(), "host": self.server_address[0],
            "port": self.server_address[1],
            "uptime_seconds": round(time.monotonic() - self.started_at, 1),
        })
        return payload

    def clear_llm_caches(self) -> None:
        """Discard narrative responses after a provider/model/key switch."""
        self.summary_cache.clear()
        with self.assistant_cache_lock:
            self.assistant_cache.clear()
        with self.code_semantic_cache_lock:
            self.code_semantic_cache.clear()

    def invalidate_code_graph_cache(self, workspace: str | None = None) -> None:
        """Forget structural and semantic views after monitored project changes."""
        with self.code_graph_cache_lock:
            if workspace is None:
                self.code_graph_cache.clear()
            else:
                self.code_graph_cache.pop(str(Path(workspace).expanduser().resolve()), None)
        with self.code_semantic_cache_lock:
            if workspace is None:
                self.code_semantic_cache.clear()
            else:
                prefix = hashlib.sha256(
                    str(Path(workspace).expanduser().resolve()).encode("utf-8")
                ).hexdigest()[:16] + ":"
                for key in [key for key in self.code_semantic_cache if key.startswith(prefix)]:
                    self.code_semantic_cache.pop(key, None)

    def get_code_graph(self, workspace: str) -> dict[str, Any]:
        """Build/cache one registered workspace's metadata-only code graph."""
        normalized = str(Path(workspace).expanduser().resolve())
        now = time.monotonic()
        with self.code_graph_cache_lock:
            cached = self.code_graph_cache.get(normalized)
            if cached is not None and cached[0] > now:
                return cached[1]
        graph = self.code_graph_builder(normalized)
        if not isinstance(graph, dict):
            raise CodeGraphError("code graph builder returned invalid data")
        with self.code_graph_cache_lock:
            if len(self.code_graph_cache) >= CODE_GRAPH_CACHE_MAX_ENTRIES:
                oldest = next(iter(self.code_graph_cache), None)
                if oldest is not None:
                    self.code_graph_cache.pop(oldest, None)
            self.code_graph_cache[normalized] = (now + CODE_GRAPH_CACHE_TTL_S, graph)
        return graph

    def code_semantic_cache_key(
        self, workspace: str, dossier: dict[str, Any], include_source: bool,
    ) -> str:
        provider = self.llm_provider_store.public_config()
        active = provider.get("active_provider")
        selected = next(
            (item for item in provider.get("providers", []) if isinstance(item, dict) and item.get("id") == active),
            {},
        )
        workspace_prefix = hashlib.sha256(
            str(Path(workspace).expanduser().resolve()).encode("utf-8")
        ).hexdigest()[:16]
        material = {
            "dossier": dossier.get("fingerprint"), "source": bool(include_source),
            "provider": selected.get("id"), "model": selected.get("model"), "schema": 1,
        }
        digest = hashlib.sha256(
            json.dumps(material, ensure_ascii=True, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return f"{workspace_prefix}:{digest}"

    def get_cached_code_semantic(self, key: str) -> dict[str, Any] | None:
        with self.code_semantic_cache_lock:
            response = self.code_semantic_cache.get(key)
            return dict(response) if isinstance(response, dict) else None

    def cache_code_semantic(self, key: str, response: dict[str, Any]) -> None:
        if response.get("status") not in ("ok", "unavailable"):
            return
        with self.code_semantic_cache_lock:
            if len(self.code_semantic_cache) >= CODE_SEMANTIC_CACHE_MAX_ENTRIES:
                oldest = next(iter(self.code_semantic_cache), None)
                if oldest is not None:
                    self.code_semantic_cache.pop(oldest, None)
            self.code_semantic_cache[key] = dict(response)

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Ignore expected disconnects when a dashboard switches its SSE stream."""
        if isinstance(sys.exception(), (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        subscriber: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=8)
        with self.subscriber_lock:
            self.subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue[dict[str, Any]]) -> None:
        with self.subscriber_lock:
            self.subscribers.discard(subscriber)

    def publish(self, message: dict[str, Any]) -> None:
        with self.subscriber_lock:
            subscribers = list(self.subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(message)
            except queue.Full:
                try:
                    subscriber.get_nowait()
                    subscriber.put_nowait(message)
                except queue.Empty:
                    pass

    def assistant_cache_key(
        self,
        question: str,
        history: list[dict[str, str]],
        project: dict[str, Any],
        stats: dict[str, Any],
        latest_drive: dict[str, Any] | None,
    ) -> str:
        """Fingerprint the non-sensitive context that can change an answer."""
        stable_stats = {key: value for key, value in stats.items() if key != "generated_at"}
        drive_state = latest_drive if isinstance(latest_drive, dict) else {}
        snapshot = {
            "question": question,
            "history": history,
            "project": {
                key: project.get(key)
                for key in ("workspace", "name", "kind", "status", "base_commit", "last_seen")
            },
            "stats": stable_stats,
            "drive": {
                key: drive_state.get(key)
                for key in ("run_id", "status", "started_at", "finished_at", "duration_s")
            },
        }
        encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def get_cached_assistant_reply(self, key: str) -> dict[str, Any] | None:
        now = time.monotonic()
        with self.assistant_cache_lock:
            entry = self.assistant_cache.get(key)
            if entry is None:
                return None
            expires_at, reply = entry
            if expires_at <= now:
                self.assistant_cache.pop(key, None)
                return None
            return dict(reply)

    def cache_assistant_reply(self, key: str, reply: dict[str, Any]) -> None:
        if reply.get("status") not in ("ok", "unavailable"):
            return
        with self.assistant_cache_lock:
            if len(self.assistant_cache) >= ASSISTANT_CACHE_MAX_ENTRIES:
                expired = [
                    cache_key
                    for cache_key, (expires_at, _reply) in self.assistant_cache.items()
                    if expires_at <= time.monotonic()
                ]
                for cache_key in expired:
                    self.assistant_cache.pop(cache_key, None)
                if len(self.assistant_cache) >= ASSISTANT_CACHE_MAX_ENTRIES:
                    oldest = next(iter(self.assistant_cache), None)
                    if oldest is not None:
                        self.assistant_cache.pop(oldest, None)
            self.assistant_cache[key] = (
                time.monotonic() + ASSISTANT_CACHE_TTL_S,
                dict(reply),
            )


class CodeDefogHandler(BaseHTTPRequestHandler):
    server: CodeDefogServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        return

    # ── Auth helpers ─────────────────────────────────────────────────────

    def _supplied_token(self) -> str:
        return self.headers.get("X-Code-Defog-Token", "") or self.headers.get("X-Code-CCTV-Token", "")

    def _token_type(self) -> str:
        declared = (
            self.headers.get("X-Code-Defog-Token-Type", "")
            or self.headers.get("X-Code-CCTV-Token-Type", "service")
        ).strip().lower()
        return TOKEN_TYPE_APPROVAL if declared == "approval" else TOKEN_TYPE_SERVICE

    def authorized_service(self) -> bool:
        supplied = self._supplied_token()
        return bool(supplied) and hmac.compare_digest(supplied, self.server.token)

    def authorized_human_approval(self) -> bool:
        supplied = (
            self.headers.get("X-Code-Defog-Approval-Key", "")
            or self.headers.get("X-Code-CCTV-Approval-Key", "")
        )
        return bool(supplied) and hmac.compare_digest(supplied, self.server.approval_secret)

    # ── Response helpers ─────────────────────────────────────────────────

    def send_json(self, payload: dict[str, Any], status: int = HTTPStatus.OK,
                  *, cors: bool = True) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cors:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(body)

    def send_text(self, content: str, content_type: str,
                  status: int = HTTPStatus.OK) -> None:
        """Serve a text/HTML payload (used by the static Web console)."""
        body = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(body)

    def serve_ui(self) -> None:
        """Serve the self-contained Web console (web/index.html) if present."""
        ui_dir = self.server.ui_dir
        if not ui_dir:
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        index = Path(ui_dir) / "index.html"
        try:
            content = index.read_text(encoding="utf-8")
        except OSError:
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        self.send_text(content, "text/html")

    def serve_ui_config(self) -> None:
        """Hand the browser the connection config (host/port/token/user).

        The service token is intentionally separate from the human approval
        key, which is never returned by this endpoint. The daemon binds
        localhost and uses a fresh random port per start.
        """
        import getpass as _getpass

        self.send_json({
            "ok": True,
            "config": {
                "host": self.server.server_address[0],
                "port": self.server.server_address[1],
                "token": self.server.token,
                "user": _getpass.getuser(),
                "served": True,
                "runtime_mode": self.server.runtime_mode,
                "approval_required": True,
            },
        }, cors=False)

    def serve_ui_services(self) -> None:
        """Return public loopback service descriptors for the UI picker."""
        agent = self.server.discovery_agent
        services = agent.discover() if agent is not None else []
        self.send_json({
            "ok": True,
            "agent": "local-service-discovery",
            "services": services,
        }, cors=False)

    def read_json_body(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            self.send_json({"error": "invalid body size"}, HTTPStatus.BAD_REQUEST)
            return None
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("payload must be an object")
            return payload
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return None

    def require_service_auth(self) -> bool:
        if self.authorized_service():
            return True
        self.send_json({"error": "unauthorized — service token required"}, HTTPStatus.UNAUTHORIZED)
        return False

    def require_human_approval(self) -> bool:
        if not self.require_service_auth():
            return False
        if self.authorized_human_approval():
            return True
        self.send_json(
            {"error": "forbidden — human approval key required"},
            HTTPStatus.FORBIDDEN,
        )
        return False

    # ── Routing ──────────────────────────────────────────────────────────

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, X-Code-Defog-Token, X-Code-Defog-Token-Type, "
            "X-Code-Defog-Approval-Key, X-Code-CCTV-Token, "
            "X-Code-CCTV-Token-Type, X-Code-CCTV-Approval-Key",
        )
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:
        route = urlparse(self.path).path

        if route == "/health":
            self.send_json({
                "ok": True,
                "service": "code-defog",
                "instance_id": self.server.instance_id,
                "ui": bool(self.server.ui_dir),
            })
            return

        # ── Static Web console (no auth: the UI is served from the same
        #    localhost, and /ui/config hands the browser the service token
        #    exactly as /health is open.  Port is a fresh random one per
        #    daemon start, and the daemon binds 127.0.0.1 only.) ───────────
        if route in ("/", "/ui", "/ui/"):
            self.serve_ui()
            return
        if route == "/ui/config":
            self.serve_ui_config()
            return
        if route == "/ui/services":
            self.serve_ui_services()
            return

        if not self.require_service_auth():
            return
        if route == "/api/state":
            self.send_json(self.server.store.state())
            return
        if route == "/api/management/info":
            self.send_json(self.server.management_info())
            return
        if route == "/api/harness":
            self.get_harness()
            return
        if route == "/api/llm/providers":
            self.get_llm_providers()
            return
        if route == "/api/project/summary":
            self.project_summary()
            return
        if route == "/api/projects/discover":
            self.projects_discover()
            return
        if route.startswith("/api/projects/") and route.endswith("/reviews"):
            from urllib.parse import unquote

            workspace = unquote(route[len("/api/projects/"):-len("/reviews")])
            self.get_project_reviews(workspace)
            return
        if route.startswith("/api/projects/") and route.endswith("/code-graph"):
            from urllib.parse import unquote

            workspace = unquote(route[len("/api/projects/"):-len("/code-graph")])
            self.get_project_code_graph(workspace)
            return
        if route.startswith("/api/projects/") and route.endswith("/drive"):
            from urllib.parse import unquote

            workspace = unquote(route[len("/api/projects/"):-len("/drive")])
            self.get_project_drive(workspace)
            return
        if route == "/api/projects":
            self.list_monitored_projects()
            return
        if route == "/api/stream":
            self.stream_state()
            return
        if route == "/api/cases":
            self.list_cases()
            return
        if route.startswith("/api/cases/") and route.endswith("/evidence"):
            case_id = route.split("/")[3]
            self.get_case_evidence(case_id)
            return
        if route.startswith("/api/cases/"):
            case_id = route.split("/")[3]
            self.get_case(case_id)
            return
        self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        route = urlparse(self.path).path

        # ── Event endpoints (service token) ───────────────────────────────
        if route == "/api/events":
            if not self.require_service_auth():
                return
            payload = self.read_json_body()
            if payload is None:
                return
            try:
                state = self.server.store.ingest(payload)
            except ValueError as error:
                self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            self.server.publish({"type": "state", "state": state})
            self.send_json({"ok": True, "state": state}, HTTPStatus.ACCEPTED)
            return

        if route == "/api/management/session/clear":
            if not self.require_service_auth():
                return
            self.clear_session()
            return

        if route == "/api/management/clear-all":
            if not self.require_service_auth():
                return
            state = self.server.store.clear_all()
            self.server.publish({"type": "state", "state": state})
            self.send_json({"ok": True, "state": state}, HTTPStatus.ACCEPTED)
            return

        # ── Local LLM provider settings (service token) ───────────────────
        if route == "/api/llm/providers":
            if not self.require_service_auth():
                return
            payload = self.read_json_body()
            if payload is None:
                return
            self.save_llm_provider(payload)
            return

        if route == "/api/llm/providers/test":
            if not self.require_service_auth():
                return
            payload = self.read_json_body()
            if payload is None:
                return
            self.test_llm_provider_connection(payload)
            return

        # ── DevLoop: Case intake (service token) ─────────────────────────
        if route == "/api/cases":
            if not self.require_service_auth():
                return
            payload = self.read_json_body()
            if payload is None:
                return
            if self.server.orchestrator is not None:
                result = self.server.orchestrator.on_source_received(payload)
            else:
                result = self.server.store.create_or_find_case(payload)
            if result.get("duplicate"):
                self.send_json({"ok": True, "duplicate": True,
                                "observation_id": result["observation_id"]}, HTTPStatus.CONFLICT)
            elif result.get("pending"):
                self.send_json({"ok": True, "pending": True,
                                "observation_id": result["observation_id"]}, HTTPStatus.ACCEPTED)
            else:
                self.send_json({"ok": True, "case": result}, HTTPStatus.CREATED)
            return

        # ── DevLoop: Case actions (approval token for grant actions,
        #    service token for cancel only) ────────────────────────────────
        if route.startswith("/api/cases/") and route.endswith("/actions"):
            case_id = route.split("/")[3]
            self.handle_case_action(case_id)
            return

        # ── DevLoop: retrospective generation (service token) ────────────
        if route.startswith("/api/cases/") and route.endswith("/retrospective"):
            if not self.require_service_auth():
                return
            case_id = route.split("/")[3]
            self.handle_generate_retrospective(case_id)
            return

        # ── DevLoop: knowledge review (independent human authority) ───────
        if route.startswith("/api/knowledge/") and route.endswith("/review"):
            if not self.require_human_approval():
                return
            record_id = route.split("/")[3]
            self.handle_knowledge_review(record_id)
            return

        # ── DevLoop: approval grant issuance (independent human authority) ─
        if route.startswith("/api/cases/") and route.endswith("/approval-grant"):
            if not self.require_human_approval():
                return
            case_id = route.split("/")[3]
            self.handle_issue_approval_grant(case_id)
            return

        # ── Project monitoring: register a project to watch ───────────────
        if route == "/api/projects":
            if not self.require_service_auth():
                return
            payload = self.read_json_body()
            if payload is None:
                return
            try:
                project = self.server.store.register_monitored_project(payload)
            except ValueError as error:
                self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
            if self.server.project_monitor is not None:
                try:
                    self.server.project_monitor.start_project(project["workspace"])
                except Exception as exc:
                    project["error"] = f"watcher failed to start: {exc}"
            self.send_json({"ok": True, "project": project}, HTTPStatus.CREATED)
            return

        # ── Project Review Run (legacy /drive name): start read-only review ─
        if route.startswith("/api/projects/") and route.endswith("/drive"):
            if not self.require_service_auth():
                return
            from urllib.parse import unquote

            workspace = unquote(route[len("/api/projects/"):-len("/drive")])
            payload = self.read_json_body()
            if payload is None:
                return
            self.start_project_drive(workspace, payload)
            return

        # ── Read-only project assistant ───────────────────────────────────
        if route.startswith("/api/projects/") and route.endswith("/assistant"):
            if not self.require_service_auth():
                return
            from urllib.parse import unquote

            workspace = unquote(route[len("/api/projects/"):-len("/assistant")])
            payload = self.read_json_body()
            if payload is None:
                return
            self.project_assistant(workspace, payload)
            return

        # ── Selected code node interpretation (read-only, dossier-bound) ─
        if route.startswith("/api/projects/") and route.endswith("/code-graph/interpret"):
            if not self.require_service_auth():
                return
            from urllib.parse import unquote

            workspace = unquote(route[len("/api/projects/"):-len("/code-graph/interpret")])
            payload = self.read_json_body()
            if payload is None:
                return
            self.interpret_project_code_node(workspace, payload)
            return

        self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_DELETE(self) -> None:
        route = urlparse(self.path).path
        if route.startswith("/api/projects/"):
            if not self.require_service_auth():
                return
            from urllib.parse import unquote

            workspace = unquote(route[len("/api/projects/"):])
            if not workspace:
                self.send_json({"error": "workspace required"}, HTTPStatus.BAD_REQUEST)
                return
            if self.server.project_monitor is not None:
                try:
                    self.server.project_monitor.stop_project(workspace)
                except Exception:
                    pass
            removed = self.server.store.unregister_monitored_project(workspace)
            if not removed:
                self.send_json({"error": "project not found"}, HTTPStatus.NOT_FOUND)
                return
            self.send_json({"ok": True})
            return
        self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    # ═══════════════════════════════════════════════════════════════════════
    # Event handlers
    # ═══════════════════════════════════════════════════════════════════════

    def clear_session(self) -> None:
        payload = self.read_json_body()
        if payload is None:
            return
        workspace = payload.get("workspace")
        conversation = payload.get("conversation_id", "default")
        if not isinstance(workspace, str) or not workspace.strip():
            self.send_json({"error": "workspace is required"}, HTTPStatus.BAD_REQUEST)
            return
        state = self.server.store.delete_session(
            str(Path(workspace).expanduser().resolve()), str(conversation))
        self.server.publish({"type": "state", "state": state})
        self.send_json({"ok": True, "state": state}, HTTPStatus.ACCEPTED)

    def stream_state(self) -> None:
        subscriber = self.server.subscribe()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.write_sse({"type": "state", "state": self.server.store.state()})
            while True:
                try:
                    message = subscriber.get(timeout=20)
                except queue.Empty:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    continue
                self.write_sse(message)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.server.unsubscribe(subscriber)

    def write_sse(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.wfile.write(b"data: " + body + b"\n\n")
        self.wfile.flush()

    # ═══════════════════════════════════════════════════════════════════════
    # DevLoop: Case API handlers
    # ═══════════════════════════════════════════════════════════════════════

    def list_cases(self) -> None:
        parsed = urlparse(self.path)
        params: dict[str, list[str]] = {}
        if parsed.query:
            from urllib.parse import parse_qs
            params = parse_qs(parsed.query)
        status = params.get("status", [None])[0]
        repo = params.get("repository_ref", [None])[0]
        try:
            limit = int(params.get("limit", ["50"])[0])
        except (ValueError, TypeError):
            limit = 50
        cases = self.server.store.list_cases(status=status, repository_ref=repo, limit=limit)
        self.send_json({"ok": True, "cases": cases, "count": len(cases)})

    def get_harness(self) -> None:
        """GET /api/harness — read-only local Agent dispatch manifest."""
        harness = self.server.harness
        if harness is None:
            self.send_json({"error": "harness not configured"}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        try:
            manifest = harness.describe()
        except Exception:
            self.send_json({"error": "harness manifest unavailable"}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        self.send_json({"ok": True, "harness": manifest})

    def get_llm_providers(self) -> None:
        """GET /api/llm/providers — browser-safe local provider state."""
        self.send_json({"ok": True, "llm": self.server.llm_provider_store.public_config()})

    def save_llm_provider(self, payload: dict[str, Any]) -> None:
        """POST /api/llm/providers — persist and activate one provider.

        ``api_key`` is intentionally consumed by the confidential store only;
        the response and SSE message contain the redacted public view.
        """
        try:
            public = self.server.llm_provider_store.save_and_activate(payload)
        except ValueError as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        self.server.clear_llm_caches()
        self.server.publish({"type": "llm_provider_updated", "llm": public})
        self.send_json({"ok": True, "llm": public})

    def test_llm_provider_connection(self, payload: dict[str, Any]) -> None:
        """POST /api/llm/providers/test — validate a saved or one-time key."""
        try:
            candidate = self.server.llm_provider_store.resolve_candidate(payload)
        except ValueError as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        result = test_llm_provider(candidate)
        self.send_json({
            "ok": result.get("status") == "ok",
            "test": result,
            "llm": self.server.llm_provider_store.public_config(),
        })

    def project_assistant(self, workspace: str, payload: dict[str, Any]) -> None:
        """POST /api/projects/{workspace}/assistant — grounded read-only chat."""
        if not workspace:
            self.send_json({"error": "workspace required"}, HTTPStatus.BAD_REQUEST)
            return
        project = self.server.store.get_monitored_project(workspace)
        if project is None:
            self.send_json({"error": "project is not monitored"}, HTTPStatus.NOT_FOUND)
            return
        question = payload.get("question")
        if not isinstance(question, str) or not question.strip():
            self.send_json({"error": "question required"}, HTTPStatus.BAD_REQUEST)
            return
        if len(question) > 1000:
            self.send_json({"error": "question must be at most 1000 characters"},
                           HTTPStatus.BAD_REQUEST)
            return
        history = normalize_project_assistant_history(payload.get("history"))
        stats = self.server.store.project_summary(workspace=project["workspace"])
        latest_drive = self.server.store.get_latest_drive_run(project["workspace"])
        cache_key = self.server.assistant_cache_key(
            question.strip(), history, project, stats, latest_drive,
        )
        assistant = self.server.get_cached_assistant_reply(cache_key)
        cache_hit = assistant is not None
        try:
            if assistant is None:
                assistant = self.server.llm_chat_fn(
                    question.strip(), project, stats, latest_drive, history,
                )
                if isinstance(assistant, dict):
                    self.server.cache_assistant_reply(cache_key, assistant)
        except Exception:
            self.send_json({"error": "project assistant unavailable"},
                           HTTPStatus.SERVICE_UNAVAILABLE)
            return
        if not isinstance(assistant, dict):
            self.send_json({"error": "project assistant returned invalid response"},
                           HTTPStatus.SERVICE_UNAVAILABLE)
            return
        self.send_json({
            "ok": assistant.get("status") == "ok",
            "assistant": assistant,
            "cached": cache_hit,
        })

    def project_summary(self) -> None:
        """GET /api/project/summary — deterministic aggregates + optional LLM
        narrative for the overview dashboard.

        ``?refresh=1`` bypasses the LLM TTL cache so the human can force a
        re-generation.  ``?workspace=<abs_path>`` scopes the aggregates to one
        project (and keys the LLM cache per project).  The deterministic
        ``stats`` are always fresh (cheap SQLite GROUP BY, computed under the
        store lock and returned); the LLM step runs outside the lock.
        """
        from urllib.parse import parse_qs

        params = parse_qs(urlparse(self.path).query)
        refresh = (params.get("refresh") or ["0"])[0] == "1"
        workspace = (params.get("workspace") or [None])[0] or None
        stats = self.server.store.project_summary(workspace=workspace)
        llm = self.server.llm_summary_fn(
            stats, refresh=refresh, cache=self.server.summary_cache, key=workspace or "default")
        self.send_json({
            "ok": True,
            "generated_at": stats["generated_at"],
            "stats": stats,
            "llm": llm,
        })

    def projects_discover(self) -> None:
        """GET /api/projects/discover — local git repos + running-process cwds."""
        agent = self.server.project_discovery_agent
        if agent is None:
            self.send_json({"ok": True, "git": [], "processes": [], "generated_at": None,
                            "note": "project discovery not configured"})
            return
        payload = agent.discover()
        self.send_json({"ok": True, **payload})

    def list_monitored_projects(self) -> None:
        """GET /api/projects — user-selected monitored projects."""
        projects = self.server.store.list_monitored_projects()
        self.send_json({"ok": True, "projects": projects, "count": len(projects)})

    def _registered_project(self, workspace: str) -> dict[str, Any] | None:
        """Canonical registered-project lookup used by every code-map route."""
        if not workspace:
            return None
        try:
            return self.server.store.get_monitored_project(workspace)
        except (OSError, ValueError):
            return None

    @staticmethod
    def _public_code_graph(graph: dict[str, Any]) -> dict[str, Any]:
        """Explicitly whitelist graph response fields; never expose source text."""
        node_keys = (
            "id", "type", "label", "path", "language", "symbol_kind", "line_start", "line_end",
            "content_hash", "symbol_count", "parse_status", "note",
        )
        edge_keys = ("id", "source", "target", "relation", "evidence", "line", "specifier")
        return {
            "schema_version": graph.get("schema_version"), "truncated": bool(graph.get("truncated")),
            "limits": graph.get("limits", {}), "counts": graph.get("counts", {}),
            "graph_fingerprint": graph.get("graph_fingerprint"),
            "edge_fingerprint": graph.get("edge_fingerprint"),
            "nodes": [
                {key: node.get(key) for key in node_keys if key in node}
                for node in graph.get("nodes", []) if isinstance(node, dict)
            ],
            "edges": [
                {key: edge.get(key) for key in edge_keys if key in edge}
                for edge in graph.get("edges", []) if isinstance(edge, dict)
            ],
        }

    def get_project_code_graph(self, workspace: str) -> None:
        """GET graph metadata for one registered monitored project."""
        project = self._registered_project(workspace)
        if project is None:
            self.send_json({"error": "project is not monitored"}, HTTPStatus.NOT_FOUND)
            return
        try:
            graph = self.server.get_code_graph(project["workspace"])
        except CodeGraphError as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        except Exception:
            self.send_json({"error": "code graph unavailable"}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        self.send_json({
            "ok": True,
            "project": {key: project.get(key) for key in ("name", "kind", "base_commit", "last_seen")},
            "graph": self._public_code_graph(graph),
        })

    def interpret_project_code_node(self, workspace: str, payload: dict[str, Any]) -> None:
        """POST a node/selection request to the bounded Code Interpreter Agent."""
        project = self._registered_project(workspace)
        if project is None:
            self.send_json({"error": "project is not monitored"}, HTTPStatus.NOT_FOUND)
            return
        node_id = payload.get("node_id")
        if not isinstance(node_id, str) or not node_id.strip() or len(node_id) > 128:
            self.send_json({"error": "node_id is required"}, HTTPStatus.BAD_REQUEST)
            return
        selection = payload.get("selection")
        if selection is not None and not isinstance(selection, dict):
            self.send_json({"error": "selection must be an object"}, HTTPStatus.BAD_REQUEST)
            return
        include_source = payload.get("include_source") is True
        include_preview = payload.get("include_preview") is True or include_source
        request_interpretation = payload.get("interpret") is not False
        try:
            graph = self.server.get_code_graph(project["workspace"])
            dossier = build_node_dossier(
                project["workspace"], graph, node_id.strip(), selection=selection,
                include_preview=include_preview, include_source=include_source,
            )
        except CodeGraphError as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        except Exception:
            self.send_json({"error": "code node unavailable"}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        response: dict[str, Any] = {"status": "not_requested"}
        cached = False
        if request_interpretation:
            cache_key = self.server.code_semantic_cache_key(project["workspace"], dossier, include_source)
            response = self.server.get_cached_code_semantic(cache_key)
            cached = response is not None
            if response is None:
                try:
                    if callable(self.server.code_interpreter_fn):
                        response = self.server.code_interpreter_fn(dossier, include_source=include_source)
                    elif self.server.harness is not None:
                        response = self.server.harness.dispatch_code_interpreter({
                            "dossier": dossier,
                            "include_source": include_source,
                            "provider_store": self.server.llm_provider_store,
                        })
                    else:
                        response = interpret_code_dossier(
                            dossier, include_source=include_source,
                            provider_store=self.server.llm_provider_store,
                        )
                except Exception:
                    self.send_json({"error": "code interpreter unavailable"}, HTTPStatus.SERVICE_UNAVAILABLE)
                    return
                if not isinstance(response, dict):
                    self.send_json({"error": "code interpreter returned invalid response"}, HTTPStatus.SERVICE_UNAVAILABLE)
                    return
                self.server.cache_code_semantic(cache_key, response)
        # A dossier is safe metadata by default. Preview/source only return on
        # an explicit request, and source context is never repeated to browser.
        public_dossier = dict(dossier)
        public_dossier.pop("source_context", None)
        self.send_json({
            "ok": response.get("status") == "ok", "cached": cached,
            "dossier": public_dossier, "interpreter": response,
        })

    def start_project_drive(self, workspace: str, payload: dict[str, Any] | None = None) -> None:
        """POST /api/projects/{workspace}/drive — spawn a project Review Run.

        Returns 202 with the running run; 409 if one is already running.
        """
        from pathlib import Path
        from .drive import normalize_review_scope, review_task_specs

        if not workspace or not Path(workspace).is_dir():
            self.send_json({"error": f"workspace is not a directory: {workspace}"},
                           HTTPStatus.BAD_REQUEST)
            return

        import threading

        review_scope = normalize_review_scope((payload or {}).get("scope"))
        # Atomic check-and-create: two concurrent POSTs cannot both pass the
        # "is a drive already running" guard and start two Review Runs.
        run_id = self.server.store.begin_review_run_if_idle(
            workspace, review_scope, review_task_specs(),
        )
        if run_id is None:
            self.send_json({"error": "a drive is already running for this project"},
                           HTTPStatus.CONFLICT)
            return
        publish = self.server.publish

        def _drive_work() -> None:
            from .drive import run_drive
            from .llm_summary import generate_drive_summary

            if self.server.drive_runner is None:
                case_intake = (
                    self.server.orchestrator.on_source_received
                    if self.server.orchestrator is not None
                    else self.server.store.create_or_find_case
                )
                run_drive(
                    self.server.store, workspace, run_id=run_id, publish=publish,
                    case_intake=case_intake, harness=self.server.harness, scope=review_scope,
                    llm_fn=lambda target, browse, stats: generate_drive_summary(
                        target, browse, stats, provider_store=self.server.llm_provider_store,
                    ),
                )
            else:
                # Keep the small injection contract used by offline HTTP tests
                # and custom runners; only the built-in driver owns promotion.
                self.server.drive_runner(
                    self.server.store, workspace, run_id=run_id, publish=publish,
                )

        thread = threading.Thread(target=_drive_work, daemon=True)
        thread.start()
        self.send_json({
            "ok": True,
            "run": self.server.store.get_review_run(run_id),
        }, HTTPStatus.ACCEPTED)

    def get_project_drive(self, workspace: str) -> None:
        """GET /api/projects/{workspace}/drive — latest Review Run (compat)."""
        if not workspace:
            self.send_json({"error": "workspace required"}, HTTPStatus.BAD_REQUEST)
            return
        run = self.server.store.get_latest_drive_run(workspace)
        if run is None:
            self.send_json({"error": "no drive run yet"}, HTTPStatus.NOT_FOUND)
            return
        self.send_json({"ok": True, "run": run})

    def get_project_reviews(self, workspace: str) -> None:
        """GET /api/projects/{workspace}/reviews — recent project Review Runs."""
        if not workspace:
            self.send_json({"error": "workspace required"}, HTTPStatus.BAD_REQUEST)
            return
        runs = self.server.store.list_review_runs(workspace)
        self.send_json({"ok": True, "runs": runs, "count": len(runs)})

    def get_case(self, case_id: str) -> None:
        case = self.server.store.get_case(case_id)
        if case is None:
            self.send_json({"error": "case not found"}, HTTPStatus.NOT_FOUND)
            return
        self.send_json({"ok": True, "case": case})

    def get_case_evidence(self, case_id: str) -> None:
        evidence = self.server.store.get_case_evidence(case_id)
        if evidence is None:
            self.send_json({"error": "case not found"}, HTTPStatus.NOT_FOUND)
            return
        self.send_json({"ok": True, "evidence": evidence})

    def handle_generate_retrospective(self, case_id: str) -> None:
        """POST /api/cases/{case_id}/retrospective — generate (or fetch) a
        retrospective report + knowledge entries for a terminal Case."""
        # Lazy import to keep startup ordering simple; no circular dependency
        # (retrospective does not import daemon modules at module level).
        from retrospective.retrospective import generate_retrospective

        result = generate_retrospective(self.server.store, case_id)
        if "error" in result:
            code = (HTTPStatus.NOT_FOUND if result["error"] == "case not found"
                    else HTTPStatus.CONFLICT)
            self.send_json({"error": result["error"]}, code)
            return
        self.send_json({"ok": True, "retrospective": result})

    def handle_knowledge_review(self, record_id: str) -> None:
        """POST /api/knowledge/{record_id}/review — human review of a
        knowledge entry.  decision ∈ {'verified', 'rejected'}.

        Knowledge review requires the independent human approval authority at
        routing time. The reviewer identity is still taken from the server
        process rather than trusted from the request body.
        """
        payload = self.read_json_body()
        if payload is None:
            return
        decision = (payload.get("decision") or "").strip()
        if decision not in ("verified", "rejected"):
            self.send_json(
                {"error": "decision must be 'verified' or 'rejected'"},
                HTTPStatus.BAD_REQUEST)
            return
        # Reviewer identity comes from the server-side process user, never
        # from the request body — a client cannot impersonate a reviewer.
        reviewer = getpass.getuser()
        note = (payload.get("note") or "").strip()
        record = self.server.store.review_knowledge_record(
            record_id, reviewer, decision, note)
        if record is None:
            self.send_json({"error": "record not found"}, HTTPStatus.NOT_FOUND)
            return
        if "error" in record:
            self.send_json({"error": record["error"]}, HTTPStatus.BAD_REQUEST)
            return
        self.server.publish({"type": "knowledge_reviewed", "record": record})
        self.send_json({"ok": True, "record": record})

    def handle_issue_approval_grant(self, case_id: str) -> None:
        """POST /api/cases/{case_id}/approval-grant

        Human-approval guarded. Issues a one-time approval_token (stored as
        SHA-256) that the caller then consumes via POST /actions with
        X-Code-Defog-Token-Type: approval. The service token alone cannot
        obtain this bearer credential; the Grant is one-shot and bound to the
        Case state.
        """
        payload = self.read_json_body()
        if payload is None:
            return
        action = clean_text(payload.get("action"), 40)
        target_ref = clean_text(payload.get("target_ref"), 200)
        approver = clean_text(payload.get("approver"), 100)
        if action not in ALL_GRANTED_ACTIONS:
            self.send_json(
                {"error": f"unknown action: {action}. Valid: approve_plan, "
                          f"approve_release, reject_plan, reject_release"},
                HTTPStatus.BAD_REQUEST)
            return
        if not target_ref:
            self.send_json(
                {"error": "target_ref is required (base_commit for plan, patch_ref for release)"},
                HTTPStatus.BAD_REQUEST)
            return
        if not approver:
            self.send_json({"error": "approver is required"}, HTTPStatus.BAD_REQUEST)
            return
        result = self.server.store.issue_approval_grant(case_id, action, target_ref, approver)
        if result is None:
            self.send_json({"error": "case not found"}, HTTPStatus.NOT_FOUND)
            return
        if "error" in result:
            status = (HTTPStatus.BAD_REQUEST if "unknown grant action" in result["error"]
                      else HTTPStatus.CONFLICT)
            self.send_json({"error": result["error"]}, status)
            return
        self.send_json({"ok": True, "grant": result})

    def handle_case_action(self, case_id: str) -> None:
        """POST /api/cases/{case_id}/actions

        Grant-based actions (approve_plan, approve_release, reject_plan,
        reject_release): require X-Code-Defog-Token-Type: approval and a
        valid one-time approval_token.  Uses the same auth model as approve.

        Cancel: requires service_token.
        """
        payload = self.read_json_body()
        if payload is None:
            return
        action = payload.get("action", "").strip()

        if action in ALL_GRANTED_ACTIONS:
            # ── Grant-consumption path (approve + reject, same model) ─
            if self._token_type() != TOKEN_TYPE_APPROVAL:
                self.send_json(
                    {"error": "grant actions require X-Code-Defog-Token-Type: approval"},
                    HTTPStatus.FORBIDDEN)
                return
            result = self.server.store.perform_case_action(case_id, action, payload)
            if result is None:
                self.send_json({"error": "case not found"}, HTTPStatus.NOT_FOUND)
                return
            if "error" in result:
                self.send_json({"error": result["error"]}, result.get("status", 400))
                return
            if action == "approve_plan" and self.server.orchestrator is not None:
                # Approval is fully consumed before any agent is resumed. A
                # repair therefore never runs with a service/API token alone.
                try:
                    resumed = self.server.orchestrator.run_active_state(case_id)
                except Exception:
                    resumed = {"error": "approved workflow could not be resumed"}
                if isinstance(resumed, dict) and "error" not in resumed:
                    result = resumed
                elif isinstance(resumed, dict):
                    result = {**result, "workflow_error": resumed["error"]}
            self.send_json({"ok": True, "case": result})
            return

        elif action == "cancel":
            if not self.require_service_auth():
                return
            result = self.server.store.perform_case_action(case_id, action, payload)
            if result is None:
                self.send_json({"error": "case not found"}, HTTPStatus.NOT_FOUND)
                return
            if "error" in result:
                self.send_json({"error": result["error"]}, result.get("status", 400))
                return
            self.send_json({"ok": True, "case": result})
            return

        else:
            self.send_json(
                {"error": f"unknown action: {action}. Valid: approve_plan, approve_release, "
                           "reject_plan, reject_release, cancel"},
                HTTPStatus.BAD_REQUEST)
            return


# Compatibility import for local scripts and third-party integrations that
# constructed the server by its former product name. New code uses
# ``CodeDefogServer``; both names intentionally share the same implementation.
CodeCCTVServer = CodeDefogServer
