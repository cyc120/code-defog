"""Per-platform data/config path resolution for Code CCTV.

This is the single source of truth for where service.json, state.sqlite3 and
log files live. macOS keeps the historical location byte-identical; Windows
uses the per-user ``%APPDATA%`` directory whose NTFS ACLs are already
restricted to the owning user.

Standalone scripts (scripts/event_client.py, scripts/manage_service.py) add a
sys.path shim and ``from daemon import paths``; the daemon package imports this
module relatively. The Web console is served by the daemon and therefore uses
this same configuration source.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def data_dir() -> Path:
    """Root directory for config, state and logs (per user, per platform)."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "CodeCCTV"
        return Path.home() / "AppData" / "Roaming" / "CodeCCTV"
    if sys.platform == "darwin":
        # Historical location; must stay byte-identical for existing installs.
        return Path.home() / "Library" / "Application Support" / "CodeCCTV"
    # Other POSIX (Linux, *BSD).
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "code-cctv"
    return Path.home() / ".local" / "share" / "code-cctv"


def config_path() -> Path:
    """Path to service.json (host/port/token). Honors CODE_CCTV_CONFIG override."""
    override = os.environ.get("CODE_CCTV_CONFIG")
    if override:
        return Path(override).expanduser()
    return data_dir() / "service.json"


def service_registry_dir() -> Path:
    """Directory containing public descriptors for locally running services."""
    return data_dir() / "services"


def state_path() -> Path:
    """Path to the SQLite state database."""
    return data_dir() / "state.sqlite3"


def log_path(kind: str) -> Path:
    """Standard output log for a local service component."""
    return data_dir() / f"{kind}.log"


def error_log_path(kind: str) -> Path:
    """Standard error log for a component."""
    return data_dir() / f"{kind}.error.log"
