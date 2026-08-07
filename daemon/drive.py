"""Automated project drive — browse a real project, run a full diagnosis, and
produce an LLM summary even when there are no errors or Cases.

The "启动自动化驱动" button in the overview view starts a daemon thread that:
1. browses the project (file tree, language, size, git state, key files, symbols),
2. detects and runs the project's own test suite (safe subprocess + timeout),
3. statically scans for TODO/FIXME markers and error-handling gaps,
4. feeds all of that plus the deterministic store stats to DeepSeek, producing a
   codecctv-style info-pyramid summary regardless of Case count.

Read-only: this module never mutates the project.  Test runs execute only an
obviously-detected project test command (pytest / npm test / make test / go test)
in the project directory with a hard timeout.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from scripts.watch_worklog import snapshot as _snapshot
    from scripts.scan_code_map import iter_files as _iter_files
    from scripts.scan_code_map import python_symbols as _python_symbols
    from scripts.scan_code_map import js_symbols as _js_symbols
    from scripts.scan_code_map import DEFAULT_EXTENSIONS as _DEFAULT_EXTENSIONS
except Exception:  # pragma: no cover - scripts import fallback
    _snapshot = None  # type: ignore[assignment]
    _iter_files = None  # type: ignore[assignment]
    _python_symbols = None  # type: ignore[assignment]
    _js_symbols = None  # type: ignore[assignment]
    _DEFAULT_EXTENSIONS = {".py", ".js", ".jsx", ".ts", ".tsx"}

_MARKERS = ("README.md", "package.json", "pyproject.toml", "requirements.txt",
            "Cargo.toml", "go.mod", "pom.xml", "Makefile", "Dockerfile")
_TEST_KEYWORDS = ("test", "tests", "spec", "specs")
_TODO_RE = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b")
_BARE_EXCEPT_RE = re.compile(r"^\s*except\s*:\s*$")
_CATCH_EXCEPT_RE = re.compile(r"^\s*except\s+Exception\s*:\s*$")
_EMPTY_CATCH_RE = re.compile(r"^\s*catch\s*\([^)]*\)\s*\{\s*\}\s*$")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ── git helpers ──────────────────────────────────────────────────────────

def _run_git(args: list[str], cwd: Path, timeout: float = 5.0) -> str:
    try:
        proc = subprocess.run(["git", "-C", str(cwd), *args],
                              capture_output=True, text=True, timeout=timeout, check=False)
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _git_browse(workspace: Path) -> dict[str, Any]:
    if not (workspace / ".git").exists():
        return {"is_git": False, "remote": "", "branch": "", "head": "",
                "dirty_count": 0, "recent_commits": []}
    remote = _run_git(["remote", "get-url", "origin"], workspace)
    branch = _run_git(["branch", "--show-current"], workspace)
    head = _run_git(["rev-parse", "--short", "HEAD"], workspace)
    status = _run_git(["status", "--porcelain"], workspace)
    commits = _run_git(["log", "-n", "8", "--format=%h %ad %an %s", "--date=short"], workspace)
    return {
        "is_git": True,
        "remote": remote,
        "branch": branch or "detached",
        "head": head,
        "dirty_count": len([ln for ln in status.splitlines() if ln.strip()]) if status else 0,
        "recent_commits": [ln for ln in commits.splitlines() if ln.strip()][:8],
    }


# ── browse ───────────────────────────────────────────────────────────────

def browse_project(workspace: str, *, test_timeout: float = 60.0) -> dict[str, Any]:
    """Collect a read-only structural overview of *workspace*."""
    ws = Path(workspace).expanduser().resolve()
    base: dict[str, Any] = {"workspace": str(ws), "browsed_at": utc_now()}

    # File tree via watch_worklog.snapshot (count, total size, language stats).
    language_stats: dict[str, int] = {}
    file_count = 0
    total_size = 0
    if _snapshot is not None:
        try:
            tree = _snapshot(ws, ".code-cctv-monitor", Path("/dev/null"))
            for rel, meta in tree.items():
                file_count += 1
                total_size += int(meta.get("size", 0) or 0)
                ext = Path(rel).suffix.lstrip(".")
                if ext:
                    language_stats[ext] = language_stats.get(ext, 0) + 1
        except OSError:
            pass
    base["file_count"] = file_count
    base["total_size"] = total_size
    base["language_stats"] = dict(sorted(language_stats.items(), key=lambda kv: -kv[1])[:10])

    # Markers present.
    markers = [m for m in _MARKERS if (ws / m).exists()]
    base["markers"] = markers

    # Key files + symbols (bounded).
    key_files: list[dict[str, Any]] = []
    if _iter_files is not None:
        try:
            code_files = _iter_files([str(ws)], _DEFAULT_EXTENSIONS)[:80]
        except OSError:
            code_files = []
        for path in code_files:
            rel = str(path.relative_to(ws))
            try:
                if path.suffix == ".py" and _python_symbols is not None:
                    symbols = _python_symbols(path)
                elif _js_symbols is not None:
                    symbols = _js_symbols(path)
                else:
                    symbols = []
            except OSError:
                symbols = []
            key_files.append({
                "path": rel,
                "symbol_count": len(symbols),
                "first_symbols": [name for _, name, _ in symbols[:6]],
            })
    base["key_files"] = key_files[:40]
    base["symbol_total"] = sum(kf["symbol_count"] for kf in base["key_files"])

    # Git state + test detection + static scan.
    base["git"] = _git_browse(ws)
    base["test"] = detect_test_command(ws)
    base["static_scan"] = scan_static(ws)
    return base


# ── test detection & probe ───────────────────────────────────────────────

def detect_test_command(workspace: Path) -> dict[str, Any]:
    """Detect an obvious test command; returns {detected:false} when unsure."""
    if (workspace / "pytest.ini").exists() or (workspace / "conftest.py").exists():
        return {"detected": True, "kind": "pytest", "command": "python -m pytest -q --tb=short", "detail": "pytest 配置"}
    if (workspace / "pyproject.toml").exists():
        text = _safe_read(workspace / "pyproject.toml")
        if "[tool.pytest" in text:
            return {"detected": True, "kind": "pytest", "command": "python -m pytest -q --tb=short", "detail": "pyproject 含 pytest"}
    if (workspace / "package.json").exists():
        try:
            pkg = json.loads((workspace / "package.json").read_text(encoding="utf-8"))
            script = (pkg.get("scripts") or {}).get("test")
            if script:
                return {"detected": True, "kind": "npm", "command": "npm test -- --runInBand", "detail": f"npm test: {script}"}
        except (OSError, json.JSONDecodeError):
            pass
    if (workspace / "Makefile").exists() and "test:" in _safe_read(workspace / "Makefile"):
        return {"detected": True, "kind": "make", "command": "make test", "detail": "Makefile test target"}
    if (workspace / "go.mod").exists():
        return {"detected": True, "kind": "go", "command": "go test ./...", "detail": "Go module"}
    return {"detected": False}


def _safe_read(path: Path, limit: int = 200_000) -> str:
    try:
        return path.read_text(encoding="utf-8")[:limit]
    except (OSError, UnicodeDecodeError):
        return ""


def run_test_probe(workspace: str, test_cmd: dict[str, Any], timeout: float = 60.0) -> dict[str, Any]:
    """Run a detected test command safely; never hangs (hard timeout)."""
    if not test_cmd or not test_cmd.get("detected"):
        return {"detected": False, "ran": False, "passed": None}
    ws = Path(workspace)
    command = test_cmd["command"]
    try:
        result = subprocess.run(
            command.split(), cwd=ws, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        return {"detected": True, "ran": True, "timed_out": True, "passed": False,
                "exit_code": None, "output_summary": "测试超时（超过 %ds）" % timeout,
                "stdout_tail": (exc.stdout or "").strip()[-2000:] if exc.stdout else "",
                "stderr_tail": (exc.stderr or "").strip()[-2000:] if exc.stderr else ""}
    except OSError as exc:
        return {"detected": True, "ran": True, "timed_out": False, "passed": False,
                "exit_code": None, "output_summary": f"测试无法启动: {exc}"}
    tail = (result.stdout or "").strip()[-2000:]
    return {
        "detected": True, "ran": True, "timed_out": False,
        "passed": result.returncode == 0, "exit_code": result.returncode,
        "output_summary": tail or "（无输出）",
    }


# ── static scan ──────────────────────────────────────────────────────────

def scan_static(workspace: str, max_files: int = 200, max_lines: int = 800) -> dict[str, Any]:
    """Count TODO/FIXME markers and detect error-handling gaps (bounded)."""
    ws = Path(workspace).expanduser().resolve()
    todo = fixme = 0
    gaps: list[dict[str, Any]] = []
    scanned = 0
    if _iter_files is None:
        return {"todo_count": 0, "fixme_count": 0, "error_handling_gaps": [], "scanned_files": 0}
    try:
        files = _iter_files([str(ws)], _DEFAULT_EXTENSIONS)[:max_files]
    except OSError:
        files = []
    for path in files:
        if scanned >= max_files:
            break
        try:
            lines = path.read_text(encoding="utf-8").splitlines()[:max_lines]
        except (OSError, UnicodeDecodeError):
            continue
        scanned += 1
        for idx, line in enumerate(lines, start=1):
            for match in _TODO_RE.findall(line):
                if match in ("TODO", "HACK", "XXX"):
                    todo += 1
                elif match == "FIXME":
                    fixme += 1
            if path.suffix == ".py" and (_BARE_EXCEPT_RE.match(line) or _CATCH_EXCEPT_RE.match(line)):
                gaps.append({"file": str(path.relative_to(ws)), "line": idx,
                             "kind": "python_bare_except", "snippet": line.strip()[:80]})
            elif path.suffix in (".js", ".jsx", ".ts", ".tsx") and _EMPTY_CATCH_RE.match(line):
                gaps.append({"file": str(path.relative_to(ws)), "line": idx,
                             "kind": "js_empty_catch", "snippet": line.strip()[:80]})
            if len(gaps) >= 30:
                break
        if len(gaps) >= 30:
            break
    return {"todo_count": todo, "fixme_count": fixme,
            "error_handling_gaps": gaps, "scanned_files": scanned}


# ── orchestrator (runs on a daemon thread) ───────────────────────────────

def run_drive(
    store: Any,
    workspace: str,
    *,
    run_id: str | None = None,
    llm_fn: Callable[..., dict[str, Any]] | None = None,
    browse_fn: Callable[..., dict[str, Any]] | None = None,
    test_fn: Callable[..., dict[str, Any]] | None = None,
    scan_fn: Callable[..., dict[str, Any]] | None = None,
    publish: Callable[[dict[str, Any]], None] | None = None,
    test_timeout: float = 60.0,
) -> dict[str, Any]:
    """Run a full project drive synchronously (caller spawns the thread).

    *run_id* is the already-inserted 'running' row (the HTTP handler begins it);
    when omitted (e.g. direct test use) one is created here.
    """
    from daemon.llm_summary import generate_drive_summary  # lazy, avoid cycle

    if run_id is None:
        run_id = store.begin_drive_run(workspace)
    browse = (browse_fn or browse_project)(workspace)
    if publish:
        publish({"type": "drive_status", "run": {"run_id": run_id, "workspace": workspace,
                                                 "status": "running", "started_at": utc_now()}})
    started = time.monotonic()
    try:
        stats = store.project_summary(workspace=workspace)
        test = browse.get("test") or {}
        if test.get("detected"):
            browse["test"] = (test_fn or run_test_probe)(workspace, test, test_timeout)
        scan = browse.get("static_scan") or {}
        browse["static_scan"] = (scan_fn or scan_static)(workspace)
        llm = (llm_fn or (lambda w, b, s: generate_drive_summary(w, b, s)))(workspace, browse, stats)
        duration = round(time.monotonic() - started, 2)
        store.finish_drive_run(run_id, "complete", duration, browse, llm, None)
        if publish:
            publish({"type": "drive_status", "run": {
                "run_id": run_id, "workspace": workspace, "status": "complete",
                "finished_at": utc_now(), "duration_s": duration,
                "llm": llm, "browse": browse,
            }})
        return {"run_id": run_id, "status": "complete", "browse": browse, "llm": llm}
    except Exception as exc:  # pragma: no cover - defensive
        duration = round(time.monotonic() - started, 2)
        store.finish_drive_run(run_id, "error", duration, None, None, str(exc))
        if publish:
            publish({"type": "drive_status", "run": {
                "run_id": run_id, "workspace": workspace, "status": "error",
                "finished_at": utc_now(), "duration_s": duration, "error": str(exc),
            }})
        return {"run_id": run_id, "status": "error", "error": str(exc)}
