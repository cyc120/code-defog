"""Minimal .env loader for Code Defog.

Reads a KEY=VALUE file from the repo root (default: .env) and populates
os.environ for keys not already set.  Real environment variables always
win over .env values.

Used as a fallback so scripts work with a project-local .env file even
when the shell hasn't exported DEEPSEEK_API_KEY.
"""

from __future__ import annotations

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]


def load_dotenv(path: str | os.PathLike | None = None) -> bool:
    """Load KEY=VALUE lines from *path* (default: <repo root>/.env).

    Returns True if the file existed and was read, False otherwise.
    Ignores blank lines and lines starting with '#'.
    """
    env_file = Path(path) if path else _REPO_ROOT / ".env"
    if not env_file.is_file():
        return False

    changed = False
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            changed = True
    return changed


def get_key(name: str, default: str = "") -> str:
    """Load .env if present, then return os.environ.get(name, default)."""
    load_dotenv()
    return os.environ.get(name, default)
