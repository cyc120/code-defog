"""Lightweight per-platform path resolution for the Windows app.

Duplicates daemon/paths.py on purpose: the app must run standalone without
importing the daemon package (e.g. when installed separately). Keep the two in
sync; daemon/paths.py is the canonical source.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def data_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "CodeCCTV"
        return Path.home() / "AppData" / "Roaming" / "CodeCCTV"
    return Path.home() / "Library" / "Application Support" / "CodeCCTV"


def config_path() -> Path:
    override = os.environ.get("CODE_CCTV_CONFIG")
    if override:
        return Path(override).expanduser()
    return data_dir() / "service.json"
