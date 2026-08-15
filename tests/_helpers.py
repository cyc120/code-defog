"""Shared test utilities.

One server-start helper instead of eight near-identical copies that drift
whenever the CodeDefogServer constructor changes.  Every helper here is
loopback-only, temp-dir-isolated and touches no network.
"""

from __future__ import annotations

import secrets
import threading
from typing import Any

from daemon.server import CodeDefogServer
from daemon.store import StateStore


def start_server(store: StateStore, *, token: str | None = None, **kwargs: Any):
    """Start CodeDefogServer on a random loopback port.

    *kwargs* pass straight through to the server constructor (approval_secret,
    drive_runner, llm_summary_fn, orchestrator, ...).  Returns the tuple
    (server, base_url, token).
    """
    token = token or secrets.token_hex(16)
    server = CodeDefogServer(("127.0.0.1", 0), token, store, **kwargs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}", token


def seed_case(store: StateStore, case_id: str, status: str = "RECEIVED") -> None:
    """Create a real cases row so FK-enforced child inserts are valid."""
    now = "2026-08-01T00:00:00Z"
    store.connection.execute(
        "INSERT INTO cases (case_id, status, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (case_id, status, now, now),
    )
    store.connection.commit()
