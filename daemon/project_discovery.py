"""Local project discovery — enumerate candidate projects on this machine.

Finds two kinds of candidates for the "select projects to monitor" window:
1. **Git repositories** under configured root directories (``CODE_DEFOG_PROJECT_ROOTS``).
2. **Running processes** whose working directory looks like a project.

Privacy: processes expose only ``pid``/``name``/``executable``/``cwd`` — never
argv or environment.  Permission errors and unreadable directories are skipped,
never fatal.  Discovery is bounded (depth, candidate count) so a root like
``~`` cannot blow up memory.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Reuse the junk-dir set from the code scanner so discovery skips the same
# noise (node_modules, venv, .git internals, build output, …).
try:
    from scripts.scan_code_map import SKIP_DIRS as _SCAN_SKIP_DIRS
except Exception:  # pragma: no cover - fallback if scripts not importable
    _SCAN_SKIP_DIRS = {".git", ".hg", ".svn", ".venv", "venv", "env",
                       "node_modules", "dist", "build", "__pycache__"}

DEFAULT_ROOTS = ("~/code", "~/projects", "~", ".")
DEFAULT_MAX_DEPTH = 6
DEFAULT_MAX_CANDIDATES = 200
DEFAULT_GIT_TIMEOUT = 2.0
DEFAULT_MAX_PROCESS_SAMPLE = 400

_SAFE_NAME = re.compile(r"[^\w .-]")


def _run(args: list[str], cwd: Path | None = None, timeout: float = 2.0) -> str:
    try:
        proc = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _git_meta(repo_path: Path) -> dict[str, str]:
    """Best-effort git metadata for a candidate repo (never fatal)."""
    return {
        "git_remote": _run(["git", "remote", "get-url", "origin"], repo_path, DEFAULT_GIT_TIMEOUT),
        "branch": _run(["git", "branch", "--show-current"], repo_path, DEFAULT_GIT_TIMEOUT),
        "last_commit": _run(
            ["git", "log", "-1", "--format=%h %ad %s", "--date=short"],
            repo_path, DEFAULT_GIT_TIMEOUT),
    }


def _clean_name(value: str) -> str:
    return _SAFE_NAME.sub("_", value.strip()) or "unknown"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class LocalProjectDiscoveryAgent:
    """Enumerate candidate local projects without touching credentials."""

    def __init__(
        self,
        roots: tuple[str, ...] | None = None,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_candidates: int = DEFAULT_MAX_CANDIDATES,
        git_timeout: float = DEFAULT_GIT_TIMEOUT,
        max_process_sample: int = DEFAULT_MAX_PROCESS_SAMPLE,
    ) -> None:
        self.roots = roots or self._resolve_roots()
        self.max_depth = max(max_depth, 1)
        self.max_candidates = max(max_candidates, 1)
        self.git_timeout = git_timeout
        self.max_process_sample = max(max_process_sample, 1)
        self.skip_dirs = set(_SCAN_SKIP_DIRS)

    @staticmethod
    def _resolve_roots() -> tuple[str, ...]:
        env = (
            os.environ.get("CODE_DEFOG_PROJECT_ROOTS")
            or os.environ.get("CODE_CCTV_PROJECT_ROOTS", "")
        ).strip()
        if env:
            return tuple(part.strip() for part in env.split(os.pathsep) if part.strip())
        return DEFAULT_ROOTS

    # ── Git repositories ────────────────────────────────────────────────

    def discover_git_projects(self) -> list[dict[str, Any]]:
        """Walk configured roots (depth-bounded) and return git-repo candidates."""
        candidates: list[dict[str, Any]] = []
        seen: set[Path] = set()

        for root in self.roots:
            if len(candidates) >= self.max_candidates:
                break
            try:
                root_path = Path(root).expanduser().resolve()
            except OSError:
                continue
            if not root_path.is_dir():
                continue
            for dirpath, dirnames, _filenames in os.walk(root_path):
                if len(candidates) >= self.max_candidates:
                    break
                current = Path(dirpath)
                # Depth bound relative to the scanned root.
                try:
                    depth = len(current.relative_to(root_path).parts)
                except ValueError:
                    depth = 0
                if depth >= self.max_depth:
                    dirnames[:] = []
                    continue
                # Prune junk dirs in-place (do not descend into them).
                dirnames[:] = [dn for dn in dirnames if dn not in self.skip_dirs]

                if (current / ".git").is_dir() or (current / ".git").is_file():
                    if current in seen:
                        continue
                    seen.add(current)
                    candidates.append(self._git_candidate(current))
                    # A git worktree's .git is a file; skip further descent only
                    # when we hit the repo root so we don't list nested repos.
                    continue

        return candidates

    def _git_candidate(self, repo_path: Path) -> dict[str, Any]:
        meta = _git_meta(repo_path)
        return {
            "kind": "git",
            "path": str(repo_path),
            "name": repo_path.name,
            "git_remote": meta["git_remote"],
            "branch": meta["branch"],
            "last_commit": meta["last_commit"],
            "git_available": True,
        }

    # ── Running processes ───────────────────────────────────────────────

    # Executable basenames that indicate a development/runtime process whose cwd
    # is worth inspecting for project discovery.
    _DEV_COMM_KEYS = (
        "python", "python3", "node", "npm", "npx", "pnpm", "yarn", "uvicorn",
        "gunicorn", "flask", "pytest", "go", "npm-run", "vite", "tsx", "next",
        "bun", "ruby", "rails", "java", "gradle", "mvn", "docker", "docker-compose",
    )

    def _list_processes(self) -> list[dict[str, str]]:
        """Return [{pid, name}] for running processes (best-effort).

        Scans the full ``ps`` output (no early truncation), but only returns
        rows whose executable basename matches a development keyword so we never
        call ``lsof`` on hundreds of system processes.
        """
        if sys.platform == "win32":
            # Windows: tasklist, no cwd resolution here.
            try:
                out = subprocess.run(
                    ["tasklist", "/FO", "CSV", "/NH"],
                    capture_output=True, text=True, timeout=3.0, check=False).stdout
            except (OSError, subprocess.TimeoutExpired):
                return []
            rows = []
            for line in out.splitlines()[: self.max_process_sample * 4]:
                parts = line.strip().split('","')
                if len(parts) >= 2:
                    name = parts[0].strip('"').lower()
                    if any(k in name for k in self._DEV_COMM_KEYS):
                        rows.append({"pid": parts[1].strip('"'), "name": parts[0].strip('"')})
            return rows
        try:
            out = subprocess.run(
                ["ps", "-axo", "pid=,comm="], capture_output=True, text=True,
                timeout=3.0, check=False).stdout
        except (OSError, subprocess.TimeoutExpired):
            return []
        rows = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            pid, _, name = line.partition(" ")
            pid = pid.strip()
            name = name.strip()
            if not pid.isdigit() or not name:
                continue
            basename = Path(name).name.lower()
            if any(key in basename for key in self._DEV_COMM_KEYS):
                rows.append({"pid": pid, "name": name})
            if len(rows) >= self.max_process_sample:
                break
        return rows

    def _process_cwd(self, pid: str) -> str:
        """Best-effort cwd for a PID; ``""`` when unavailable."""
        if sys.platform == "win32":
            return ""
        try:
            out = subprocess.run(
                ["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"],
                capture_output=True, text=True, timeout=1.5, check=False).stdout
        except (OSError, subprocess.TimeoutExpired):
            return ""
        for line in out.splitlines():
            if line.startswith("n"):
                return line[1:].strip()
        return ""

    # Markers that indicate a directory is a project (not a system container).
    _PROJECT_MARKERS = (
        ".git", "package.json", "pyproject.toml", "requirements.txt", "Cargo.toml",
        "go.mod", "pom.xml", "build.gradle", "Makefile", "CMakeLists.txt", "README.md",
    )

    def _looks_like_project(self, cwd_path: Path) -> bool:
        try:
            return any((cwd_path / marker).exists() for marker in self._PROJECT_MARKERS)
        except OSError:
            return False

    def discover_processes(self) -> list[dict[str, Any]]:
        """Return running-process candidates whose cwd is a project directory.

        A cwd is a project when it contains ``.git`` or a common project marker
        (package.json, pyproject.toml, …).  System containers and home-root cwds
        are excluded.  Only exposes pid/name/cwd; never argv or env.
        """
        candidates: list[dict[str, Any]] = []
        seen_cwds: set[str] = set()
        for proc in self._list_processes():
            if len(candidates) >= self.max_candidates:
                break
            cwd = self._process_cwd(proc["pid"])
            if not cwd:
                continue
            cwd_path = Path(cwd)
            if not cwd_path.is_dir() or cwd_path.name in self.skip_dirs:
                continue
            resolved = str(cwd_path.resolve() if cwd_path.exists() else cwd_path)
            if resolved in seen_cwds:
                continue  # dedupe: multiple PIDs in the same project dir
            if not self._looks_like_project(cwd_path):
                continue
            seen_cwds.add(resolved)
            candidates.append({
                "kind": "process",
                "pid": proc["pid"],
                "name": _clean_name(proc["name"]),
                "executable": _clean_name(proc["name"]),
                "cwd": resolved,
                "project_path": resolved,
            })
        return candidates

    # ── Combined ────────────────────────────────────────────────────────

    def discover(self) -> dict[str, Any]:
        """Return both candidate lists plus a timestamp."""
        return {
            "git": self.discover_git_projects(),
            "processes": self.discover_processes(),
            "generated_at": utc_now(),
        }
