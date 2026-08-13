#!/usr/bin/env python3
"""Best-effort client for the local Code Defog event service."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# Make the repository root importable so the shared path resolver is used,
# matching the daemon. Harmless no-op when the package is already on sys.path.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
from daemon import paths  # noqa: E402


def config_path() -> Path:
    return paths.config_path()


def load_config() -> dict[str, Any] | None:
    path = config_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if not payload.get("host") or not payload.get("port") or not payload.get("token"):
        return None
    return payload


def post_event(payload: dict[str, Any], timeout: float = 0.35) -> bool:
    """Send a summary event without making normal logging depend on the daemon."""
    config = load_config()
    if config is None:
        return False
    try:
        port = int(config["port"])
    except (TypeError, ValueError):
        return False
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"http://{config['host']}:{port}/api/events",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "X-Code-Defog-Token": str(config["token"]),
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 300
    except (OSError, HTTPError, URLError, ValueError):
        return False
