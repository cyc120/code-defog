"""Pure-Python client for the Code CCTV daemon (no Qt dependency).

Mirrors the macOS StatusStore.swift logic: loads service.json (host/port/token),
subscribes to the SSE stream with /api/state polling as a fallback, and dedups
state updates by a content ID so local management changes do not re-trigger.
Uses urllib + threading so it is unit-testable without PySide6.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from typing import Any, Callable

from app_paths import config_path


def load_config(override: str | None = None) -> dict[str, Any] | None:
    """Read service.json; returns {host, port, token} or None when unavailable."""
    path = config_path() if override is None else override
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    host = payload.get("host")
    port = payload.get("port")
    token = payload.get("token")
    if not host or not port or not token:
        return None
    return {"host": host, "port": int(port), "token": str(token)}


def session_id(project: dict[str, Any]) -> str:
    conversation = project.get("conversation_id") or ""
    return conversation if conversation else "default"


def _encode(value: Any) -> str:
    text = str(value)
    return f"{len(text.encode('utf-8'))}:{text}"


def content_id(state: dict[str, Any]) -> str:
    """Exact mirror of Swift GlobalState.contentID(for:): a length-prefixed
    join of the fields that determine whether the UI content actually changed.
    generated_at is intentionally excluded so polling does not re-trigger."""
    projects = state.get("projects") or []
    return default_content_id(projects)


def default_content_id(projects: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for project in projects:
        events = project.get("recent_events") or []
        event_id = events[0].get("id", "") if events else ""
        fields = [
            project.get("workspace", ""),
            session_id(project),
            project.get("conversation_name", "") or "",
            project.get("updated_at", ""),
            project.get("event_count", ""),
            project.get("status", ""),
            project.get("phase", ""),
            project.get("focus", ""),
            project.get("note", ""),
            project.get("evidence", ""),
            project.get("event_type", ""),
            "1" if project.get("active") else "0",
            event_id,
        ]
        parts.append("|".join(_encode(field) for field in fields))
    return ";".join(parts)


def parse_sse_line(line: bytes | str) -> tuple[str, dict[str, Any]] | None:
    """Parse one SSE line. Heartbeat/comment lines (': ...') and unknown event
    types return None; a 'data:' line returns (type, payload)."""
    if isinstance(line, bytes):
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError:
            return None
    stripped = line.rstrip("\r\n")
    if not stripped.startswith("data:"):
        return None
    data = stripped[len("data:"):].lstrip()
    try:
        envelope = json.loads(data)
    except json.JSONDecodeError:
        return None
    if not isinstance(envelope, dict):
        return None
    event_type = envelope.get("type", "")
    state = envelope.get("state")
    if event_type != "state" or not isinstance(state, dict):
        return None
    return (event_type, state)


class StatusClient:
    """Background SSE + polling client with callback observers (no Qt)."""

    def __init__(
        self,
        config_provider: Callable[[], dict[str, Any] | None] = load_config,
        poll_interval: float = 5.0,
        stream_timeout: float = 30.0,
        enable_stream: bool = True,
    ) -> None:
        self._config_provider = config_provider
        self._poll_interval = poll_interval
        self._stream_timeout = stream_timeout
        self._enable_stream = enable_stream
        self._stop = threading.Event()
        self._stream_thread: threading.Thread | None = None
        self._poll_thread: threading.Thread | None = None
        self._stream_connected = False
        self._poll_connected = False
        self._connected = False
        self._state: dict[str, Any] = {}
        self._lock = threading.Lock()
        self.on_state: Callable[[dict[str, Any]], None] | None = None
        self.on_connection: Callable[[bool], None] | None = None

    def start(self) -> None:
        if self._enable_stream:
            self._stream_thread = threading.Thread(
                target=self._stream_loop, name="code-cctv-sse", daemon=True
            )
            self._stream_thread.start()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, name="code-cctv-poll", daemon=True
        )
        self._poll_thread.start()

    def stop(self) -> None:
        self._stop.set()
        for thread in (self._stream_thread, self._poll_thread):
            if thread is not None:
                thread.join(timeout=2)

    @property
    def state(self) -> dict[str, Any]:
        with self._lock:
            return self._state

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    # -- HTTP plumbing ------------------------------------------------------

    def _request(self, method: str, path: str, body: Any = None,
                 timeout: float = 6.0) -> tuple[int, bytes]:
        config = self._config_provider()
        if config is None:
            raise OSError("no daemon config")
        url = f"http://{config['host']}:{config['port']}{path}"
        request = urllib.request.Request(url, method=method)
        request.add_header("X-Code-CCTV-Token", config["token"])
        if body is not None:
            request.add_header("Content-Type", "application/json; charset=utf-8")
            request.data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return (response.status, response.read())

    # -- SSE loop -----------------------------------------------------------

    def _stream_loop(self) -> None:
        retry_delay = 1.0
        while not self._stop.is_set():
            try:
                config = self._config_provider()
                if config is None:
                    self._set_stream_connected(False)
                    self._sleep_or_stop(retry_delay)
                    retry_delay = min(retry_delay * 2, 10.0)
                    continue
                url = f"http://{config['host']}:{config['port']}/api/stream"
                request = urllib.request.Request(url)
                request.add_header("X-Code-CCTV-Token", config["token"])
                with urllib.request.urlopen(request, timeout=self._stream_timeout) as response:
                    self._set_stream_connected(True)
                    retry_delay = 1.0
                    for raw_line in response:
                        if self._stop.is_set():
                            break
                        parsed = parse_sse_line(raw_line)
                        if parsed is not None:
                            self._apply_state(parsed[1])
                self._set_stream_connected(False)
            except OSError:
                self._set_stream_connected(False)
            self._sleep_or_stop(retry_delay)
            retry_delay = min(retry_delay * 2, 10.0)

    # -- Polling fallback ---------------------------------------------------

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not self._stream_connected:
                    status, body = self._request("GET", "/api/state")
                    if 200 <= status < 300:
                        state = json.loads(body.decode("utf-8"))
                        self._set_poll_connected(True)
                        self._apply_state(state)
            except (OSError, json.JSONDecodeError):
                self._set_poll_connected(False)
            self._sleep_or_stop(self._poll_interval)

    # -- State + connection bookkeeping -------------------------------------

    def _apply_state(self, next_state: dict[str, Any]) -> None:
        with self._lock:
            current_id = content_id(self._state)
            next_id = content_id(next_state)
            if next_id == current_id:
                return
            self._state = next_state
            callback = self.on_state
        if callback is not None:
            callback(next_state)

    def _set_stream_connected(self, value: bool) -> None:
        with self._lock:
            self._stream_connected = value
            self._refresh_connection_locked()

    def _set_poll_connected(self, value: bool) -> None:
        with self._lock:
            self._poll_connected = value
            self._refresh_connection_locked()

    def _refresh_connection_locked(self) -> None:
        next_connected = self._stream_connected or self._poll_connected
        if next_connected == self._connected:
            return
        self._connected = next_connected
        callback = self.on_connection
        if callback is not None:
            callback(next_connected)

    def _sleep_or_stop(self, seconds: float) -> None:
        self._stop.wait(max(seconds, 0.1))

    # -- Management calls ---------------------------------------------------

    def refresh_management_info(self) -> dict[str, Any] | None:
        try:
            status, body = self._request("GET", "/api/management/info")
        except OSError:
            return None
        if not 200 <= status < 300:
            return None
        try:
            return json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            return None

    def clear_session(self, workspace: str, conversation_id: str) -> None:
        self._post_management("session/clear",
                              {"workspace": workspace, "conversation_id": conversation_id})

    def clear_all(self) -> None:
        self._post_management("clear-all", {})

    def _post_management(self, action: str, payload: dict[str, Any]) -> None:
        try:
            self._request("POST", f"/api/management/{action}", body=payload)
        except OSError:
            pass  # State refresh arrives through SSE/polling; management failures are silent.
