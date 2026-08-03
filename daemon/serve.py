#!/usr/bin/env python3
"""Start the Code CCTV localhost service."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from .server import CodeCCTVServer
from .store import StateStore


APP_SUPPORT = Path.home() / "Library" / "Application Support" / "CodeCCTV"
DEFAULT_CONFIG = APP_SUPPORT / "service.json"
DEFAULT_STATE = APP_SUPPORT / "state.sqlite3"


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Code CCTV local background service.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="Use 0 to select a free local port.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    token = secrets.token_urlsafe(32)
    store = StateStore(args.state)
    server = CodeCCTVServer((args.host, args.port), token, store)
    address, port = server.server_address
    write_json(
        args.config.expanduser().resolve(),
        {
            "host": address,
            "port": port,
            "token": token,
            "state_path": str(args.state.expanduser().resolve()),
            "pid": os.getpid(),
            "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        },
    )

    stopping = threading.Event()

    def stop(*_signals: object) -> None:
        if stopping.is_set():
            return
        stopping.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        store.close()


if __name__ == "__main__":
    main()
