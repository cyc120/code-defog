"""Canonical repository identity + base-commit resolution.

Real projects can be referenced by many spellings — ``/a/b/c``, ``/a/b/c/``,
a symlink, two clones, or a git URL — which would fragment ``incident_signature``
into duplicate Cases for the same incident.  This module resolves a stable
canonical identity so Code Defog can associate sources across spellings.

Canonical form (in priority order):
- ``<git_remote>|<abs_path>``  when the path is a git repo with a remote
- ``<abs_path>``              when the path is a git repo without a remote
- ``<abs_path>``              when the path resolves to a directory (non-git)
- the raw string               when nothing else applies

A short TTL cache avoids spawning ``git remote`` per event.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

_GIT_REMOTE_CACHE_TTL_S = 60.0
_git_remote_cache: dict[str, tuple[float, str]] = {}
_base_commit_cache: dict[str, tuple[float, str | None]] = {}


def redact_remote_url(url: str) -> str:
    """Strip ``userinfo`` (``https://user:token@host/...``) from a git remote.

    CI tokens embedded in remotes must never reach persisted reports, the
    LLM prompt, the UI or the canonical identity.  SSH ``git@host`` forms
    carry no secret and are left untouched.
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
    except ValueError:
        return raw
    if parsed.scheme not in ("http", "https") or not parsed.username:
        return raw
    hostname = parsed.hostname or ""
    netloc = hostname
    if parsed.port is not None:
        netloc = f"{hostname}:{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


def _run_git(args: list[str], cwd: Path, timeout: float = 2.0) -> str:
    """Best-effort ``git <args>`` in *cwd*; returns stripped stdout or ``""``."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _is_git_repo(abs_path: Path) -> bool:
    return (abs_path / ".git").is_dir() or (abs_path / ".git").is_file()


def _git_remote(abs_path: Path) -> str:
    """Cached ``git remote get-url origin``; ``""`` when none/error."""
    key = str(abs_path)
    now = time.monotonic()
    cached = _git_remote_cache.get(key)
    if cached is not None and now - cached[0] < _GIT_REMOTE_CACHE_TTL_S:
        return cached[1]
    remote = redact_remote_url(_run_git(["remote", "get-url", "origin"], abs_path))
    _git_remote_cache[key] = (now, remote)
    return remote


def canonical_repo_identity(repository_ref: str) -> dict[str, Any]:
    """Return ``{canonical_ref, abs_path, git_remote, is_git}`` for *repository_ref*."""
    if not isinstance(repository_ref, str) or not repository_ref.strip():
        return {"canonical_ref": "", "abs_path": "", "git_remote": "", "is_git": False}

    raw = repository_ref.strip()
    try:
        abs_path = Path(raw).expanduser().resolve()
    except OSError:
        abs_path = Path(raw)

    is_git = _is_git_repo(abs_path)
    git_remote = _git_remote(abs_path) if is_git else ""

    if git_remote:
        canonical_ref = f"{git_remote}|{abs_path}"
    else:
        canonical_ref = str(abs_path) if (is_git or abs_path.is_dir()) else raw

    return {
        "canonical_ref": canonical_ref,
        "abs_path": str(abs_path),
        "git_remote": git_remote,
        "is_git": is_git,
    }


def resolve_base_commit(abs_path: str) -> str | None:
    """``git rev-parse HEAD`` for *abs_path*; ``None`` when git unavailable or not a repo."""
    if not abs_path:
        return None
    path = Path(abs_path)
    key = str(path)
    now = time.monotonic()
    cached = _base_commit_cache.get(key)
    if cached is not None and now - cached[0] < _GIT_REMOTE_CACHE_TTL_S:
        return cached[1]
    commit = _run_git(["rev-parse", "HEAD"], path) or None
    _base_commit_cache[key] = (now, commit)
    return commit


def clear_caches() -> None:
    """Drop both TTL caches (used by tests)."""
    _git_remote_cache.clear()
    _base_commit_cache.clear()
