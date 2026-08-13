"""Per-platform data/config path resolution for Code Defog.

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


def _platform_data_dir(product_dir: str) -> Path:
    """Return the platform-native application data path for one product name."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / product_dir
        return Path.home() / "AppData" / "Roaming" / product_dir
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / product_dir
    # Other POSIX (Linux, *BSD).
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / product_dir.lower()
    return Path.home() / ".local" / "share" / product_dir.lower()


def legacy_data_dir() -> Path:
    """Pre-rename data location, kept read-compatible for existing installs."""
    return _platform_data_dir("CodeCCTV")


def data_dir() -> Path:
    """Root directory for config, state and logs (per user, per platform).

    New installations use ``CodeDefog``.  When only the former Code CCTV
    directory exists, keep using it so a product rename never strands local
    SQLite history, service descriptors, or provider configuration.
    """
    current = _platform_data_dir("CodeDefog")
    legacy = legacy_data_dir()
    return legacy if legacy.exists() and not current.exists() else current


def config_path() -> Path:
    """Path to service.json; new and legacy environment names are supported."""
    override = os.environ.get("CODE_DEFOG_CONFIG") or os.environ.get("CODE_CCTV_CONFIG")
    if override:
        return Path(override).expanduser()
    return data_dir() / "service.json"


def llm_provider_config_path() -> Path:
    """Local confidential LLM configuration, never part of project state.

    ``CODE_DEFOG_LLM_CONFIG`` is primarily useful for isolated local tests and
    advanced deployments that provide their own protected data directory.
    """
    override = os.environ.get("CODE_DEFOG_LLM_CONFIG") or os.environ.get("CODE_CCTV_LLM_CONFIG")
    if override:
        return Path(override).expanduser()
    return data_dir() / "llm_providers.json"


def service_registry_dir() -> Path:
    """Directory containing public descriptors for locally running services."""
    return data_dir() / "services"


def state_path() -> Path:
    """Path to the SQLite state database."""
    return data_dir() / "state.sqlite3"


def monitor_state_dir() -> Path:
    """Directory for per-project watcher state snapshots."""
    return data_dir() / "monitor_state"


def project_registry_dir() -> Path:
    """Directory for optional per-project JSON descriptors (cache layer)."""
    return data_dir() / "projects"


def worktree_root() -> Path:
    """Root under which isolated git worktrees are created (repair milestone)."""
    return data_dir() / "worktrees"


def log_path(kind: str) -> Path:
    """Standard output log for a local service component."""
    return data_dir() / f"{kind}.log"


def error_log_path(kind: str) -> Path:
    """Standard error log for a component."""
    return data_dir() / f"{kind}.error.log"
