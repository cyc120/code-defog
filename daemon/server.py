#!/usr/bin/env python3
"""Threaded localhost HTTP and SSE server for Code CCTV — extended with DevLoop Case API."""

from __future__ import annotations

import getpass
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

from .store import StateStore, ALL_GRANTED_ACTIONS, APPROVAL_ACTIONS, REJECT_ACTIONS, clean_text


MAX_BODY_BYTES = 1_000_000
TOKEN_TYPE_SERVICE = "service"
TOKEN_TYPE_APPROVAL = "approval"


class CodeCCTVServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self, address: tuple[str, int], token: str, store: StateStore,
        orchestrator: Any | None = None, ui_dir: str | None = None,
        discovery_agent: Any | None = None, instance_id: str | None = None,
        approval_secret: str | None = None, runtime_mode: str = "mock",
    ) -> None:
        super().__init__(address, CodeCCTVHandler)
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


class CodeCCTVHandler(BaseHTTPRequestHandler):
    server: CodeCCTVServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        return

    # ── Auth helpers ─────────────────────────────────────────────────────

    def _supplied_token(self) -> str:
        return self.headers.get("X-Code-CCTV-Token", "")

    def _token_type(self) -> str:
        declared = self.headers.get("X-Code-CCTV-Token-Type", "service").strip().lower()
        return TOKEN_TYPE_APPROVAL if declared == "approval" else TOKEN_TYPE_SERVICE

    def authorized_service(self) -> bool:
        supplied = self._supplied_token()
        return bool(supplied) and hmac.compare_digest(supplied, self.server.token)

    def authorized_human_approval(self) -> bool:
        supplied = self.headers.get("X-Code-CCTV-Approval-Key", "")
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
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, X-Code-CCTV-Token, X-Code-CCTV-Token-Type, X-Code-CCTV-Approval-Key")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:
        route = urlparse(self.path).path

        if route == "/health":
            self.send_json({
                "ok": True,
                "service": "code-cctv",
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

        # ── Original Code CCTV endpoints (service token) ─────────────────
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

        self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    # ═══════════════════════════════════════════════════════════════════════
    # Original Code CCTV handlers
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
        X-Code-CCTV-Token-Type: approval. The service token alone cannot
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
        reject_release): require X-Code-CCTV-Token-Type: approval and a
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
                    {"error": "grant actions require X-Code-CCTV-Token-Type: approval"},
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
