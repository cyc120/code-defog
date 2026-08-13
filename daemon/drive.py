"""Automated project drive — browse a real project, run a full diagnosis, and
produce an LLM summary even when there are no errors or Cases.

The "启动自动化驱动" button in the overview view starts a daemon thread that:
1. browses the project (file tree, language, size, git state, key files, symbols),
2. detects and runs the project's own test suite (safe subprocess + timeout),
3. statically scans for TODO/FIXME markers and error-handling gaps,
4. feeds all of that plus the deterministic store stats to DeepSeek, producing a
   Code Defog-style information-pyramid summary regardless of Case count.

Read-only: this module never mutates the project.  Test runs execute only an
obviously-detected project test command (pytest / npm test / make test / go test)
in the project directory with a hard timeout.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
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

def browse_project(
    workspace: str, *, test_timeout: float = 60.0, include_static: bool = True,
    include_git: bool = True,
) -> dict[str, Any]:
    """Collect a read-only structural overview of *workspace*."""
    ws = Path(workspace).expanduser().resolve()
    base: dict[str, Any] = {"workspace": str(ws), "browsed_at": utc_now()}

    # File tree via watch_worklog.snapshot (count, total size, language stats).
    language_stats: dict[str, int] = {}
    file_count = 0
    total_size = 0
    if _snapshot is not None:
        try:
            tree = _snapshot(ws, ".code-defog-monitor", Path("/dev/null"))
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
    base["git"] = _git_browse(ws) if include_git else {
        "is_git": False, "remote": "", "branch": "", "head": "",
        "dirty_count": 0, "recent_commits": [], "skipped": True,
    }
    base["test"] = detect_test_command(ws)
    base["static_scan"] = scan_static(ws) if include_static else {}
    return base


# ── test detection & probe ───────────────────────────────────────────────

def detect_test_command(workspace: Path) -> dict[str, Any]:
    """Detect an obvious test command; returns {detected:false} when unsure."""
    pytest_command = f"{shlex.quote(sys.executable)} -m pytest -q --tb=short"
    if (workspace / "pytest.ini").exists() or (workspace / "conftest.py").exists():
        return {"detected": True, "kind": "pytest", "command": pytest_command, "detail": "pytest 配置"}
    if (workspace / "pyproject.toml").exists():
        text = _safe_read(workspace / "pyproject.toml")
        if "[tool.pytest" in text:
            return {"detected": True, "kind": "pytest", "command": pytest_command, "detail": "pyproject 含 pytest"}
    tests_dir = workspace / "tests"
    test_files = list(tests_dir.rglob("test_*.py"))[:80] if tests_dir.is_dir() else []
    if test_files and any("unittest" in _safe_read(path, 20_000) for path in test_files):
        return {
            "detected": True,
            "kind": "unittest",
            "command": f"{shlex.quote(sys.executable)} -m unittest discover -s tests",
            "detail": "tests 目录（unittest）",
        }
    if test_files:
        return {"detected": True, "kind": "pytest", "command": pytest_command, "detail": "tests 目录"}
    if any(workspace.glob("test_*.py")):
        return {"detected": True, "kind": "pytest", "command": pytest_command, "detail": "根目录测试文件"}
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
    if test_cmd.get("kind") == "pytest" and importlib.util.find_spec("pytest") is None:
        return {
            "detected": True, "ran": False, "passed": None,
            "execution_error": True, "runner_unavailable": True,
            "output_summary": "pytest 运行器未安装；未执行项目测试。",
        }
    command = test_cmd["command"]
    try:
        result = subprocess.run(
            shlex.split(command), cwd=ws, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        return {"detected": True, "ran": True, "timed_out": True, "passed": False,
                "exit_code": None, "output_summary": "测试超时（超过 %ds）" % timeout,
                "stdout_tail": _output_tail(exc.stdout),
                "stderr_tail": _output_tail(exc.stderr)}
    except OSError as exc:
        return {"detected": True, "ran": True, "timed_out": False, "passed": False,
                "execution_error": True,
                "exit_code": None, "output_summary": f"测试无法启动: {exc}"}
    stdout_tail = _output_tail(result.stdout)
    stderr_tail = _output_tail(result.stderr)
    tail = stdout_tail or stderr_tail
    return {
        "detected": True, "ran": True, "timed_out": False,
        "passed": result.returncode == 0, "exit_code": result.returncode,
        "output_summary": tail or "（无输出）",
    }


def _output_tail(value: str | bytes | None, limit: int = 2000) -> str:
    """Convert subprocess output into JSON-safe bounded text.

    ``TimeoutExpired`` may expose bytes even when ``text=True`` was used.  A
    drive report is persisted as JSON, so normalize it before storing rather
    than turning a useful timeout observation into a failed drive run.
    """
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return str(value or "").strip()[-limit:]


def _test_failure_kind(test: dict[str, Any]) -> str | None:
    """Return the narrowly actionable self-test outcome, if any.

    A detected-but-unrunnable command is an environment observation, not a
    product failure.  Static scans and working-tree changes are intentionally
    outside this gate as well, preventing noisy automatic Case creation.
    """
    if not test.get("detected") or not test.get("ran"):
        return None
    if test.get("timed_out") is True:
        return "timeout"
    exit_code = test.get("exit_code")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0:
        return "failed"
    return None


def _self_test_case_payload(
    workspace: str,
    run_id: str,
    test: dict[str, Any],
    failure_kind: str,
) -> dict[str, Any]:
    """Build complete, non-sensitive intake signals for one failed test run."""
    test_kind = str(test.get("kind") or "project")[:40]
    command = str(test.get("command") or "")
    command_fingerprint = hashlib.sha256(command.encode("utf-8")).hexdigest()[:16]
    if failure_kind == "timeout":
        exception_type = "SelfTestTimeout"
        message_pattern = f"{test_kind} test exceeded its configured timeout"
        outcome_frame = "outcome:timeout"
        title = f"自动化自检超时：{test_kind}"
        keywords = ["self-test", test_kind, "timeout"]
    else:
        exit_code = test.get("exit_code")
        exception_type = "SelfTestFailure"
        message_pattern = f"{test_kind} test exited with code {exit_code}"
        outcome_frame = f"outcome:exit-{exit_code}"
        title = f"自动化自检失败：{test_kind}"
        keywords = ["self-test", test_kind, "failed", f"exit-{exit_code}"]

    # The source links back to a persisted drive run.  Raw test output remains
    # in that bounded run report and is not copied into Case intake signals.
    return {
        "source_type": "self_test",
        "source_uri": f"drive://{run_id}/test",
        "client_nonce": f"{run_id}:{failure_kind}",
        "raw_content": f"{exception_type}; {message_pattern}; drive_run={run_id}",
        "repository_ref": workspace,
        "title": title,
        "extracted_signals": {
            "exception_type": exception_type,
            "message_pattern": message_pattern,
            "key_frames": [
                "automated-drive:test",
                f"test-kind:{test_kind}",
                outcome_frame,
                f"command-sha256:{command_fingerprint}",
            ],
            "keywords": keywords,
            "repository_ref": workspace,
        },
    }


def promote_test_failure(
    store: Any,
    workspace: str,
    run_id: str,
    test: dict[str, Any],
    *,
    case_intake: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Promote an actionable self-test failure to the Case workflow.

    The production server passes ``Orchestrator.on_source_received`` here, so
    the newly created Case immediately enters the existing Harness.  Direct
    callers retain a useful store-only fallback for diagnostics and tests.
    """
    failure_kind = _test_failure_kind(test)
    if failure_kind is None:
        return {"triggered": False}

    payload = _self_test_case_payload(workspace, run_id, test, failure_kind)
    try:
        result = (case_intake or store.create_or_find_case)(payload)
    except Exception as exc:  # The drive report must survive an intake fault.
        return {
            "triggered": True,
            "outcome": failure_kind,
            "status": "error",
            "error": f"Case 自动升级失败: {exc}",
        }
    if not isinstance(result, dict):
        return {
            "triggered": True,
            "outcome": failure_kind,
            "status": "error",
            "error": "Case 自动升级返回了无效结果",
        }
    if result.get("duplicate"):
        return {
            "triggered": True,
            "outcome": failure_kind,
            "status": "duplicate",
            "case_id": result.get("case_id"),
        }
    if result.get("pending"):
        return {
            "triggered": True,
            "outcome": failure_kind,
            "status": "pending",
            "observation_id": result.get("observation_id"),
        }
    return {
        "triggered": True,
        "outcome": failure_kind,
        "status": "linked" if result.get("case_id") else "error",
        "case_id": result.get("case_id"),
        "case_status": result.get("status"),
        "title": result.get("title") or payload["title"],
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


# ── project-review orchestration (runs on a daemon thread) ───────────────

_REVIEW_TASK_SPECS: tuple[dict[str, str | int], ...] = (
    {"task_key": "prepare", "title": "准备审查上下文", "stage": "prepare", "order": 1},
    {"task_key": "project_review", "title": "项目结构审查", "stage": "agent", "agent_id": "project_review", "order": 2},
    {"task_key": "test_probe", "title": "项目测试探测", "stage": "parallel", "order": 3},
    {"task_key": "static_scan", "title": "静态风险扫描", "stage": "parallel", "order": 4},
    {"task_key": "summary", "title": "汇总审查结论", "stage": "summary", "order": 5},
    {"task_key": "case_handling", "title": "Case 处理", "stage": "case", "order": 6},
)


def normalize_review_scope(scope: dict[str, Any] | None) -> dict[str, Any]:
    """Return a bounded, explicit project-review scope for persistence."""
    raw = scope if isinstance(scope, dict) else {}
    mode = str(raw.get("mode") or "full").lower()
    if mode not in {"full", "fast"}:
        mode = "full"
    raw_components = raw.get("components") if isinstance(raw.get("components"), dict) else {}
    components = {
        key: bool(raw_components.get(key, True))
        for key in ("structure", "tests", "static", "git")
    }
    # A project review without structural context is not meaningful.  Keep the
    # invariant server-side rather than trusting a browser control.
    components["structure"] = True
    return {"mode": mode, "components": components, "read_only": True}


def review_task_specs() -> list[dict[str, str | int]]:
    """Return independent copies for persistent Review Run initialization."""
    return [dict(spec) for spec in _REVIEW_TASK_SPECS]


def _publish_review_task(
    store: Any,
    publish: Callable[[dict[str, Any]], None] | None,
    review_run_id: str,
    task: dict[str, Any] | None,
) -> None:
    if publish and task is not None:
        publish({"type": "review_task_status", "review_run_id": review_run_id, "task": task})


def _update_review_task(
    store: Any,
    publish: Callable[[dict[str, Any]], None] | None,
    review_run_id: str,
    task_key: str,
    status: str,
    **kwargs: Any,
) -> dict[str, Any] | None:
    task = store.update_review_task(review_run_id, task_key, status, **kwargs)
    _publish_review_task(store, publish, review_run_id, task)
    return task

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
    case_intake: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    harness: Any | None = None,
    scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one full project Review Run synchronously (caller spawns the thread).

    The legacy name remains because the HTTP ``/drive`` endpoint is stable.
    New calls create a first-class ``review_run`` with visible task records.
    ``harness`` can only dispatch the dedicated read-only Project Review Agent;
    Case Agents continue to be reached exclusively through Case promotion.
    """
    from daemon.llm_summary import generate_drive_summary  # lazy, avoid cycle
    from agent_runtime.review_context import ReviewContext

    review_scope = normalize_review_scope(scope)
    if run_id is None:
        run_id = store.begin_review_run(workspace, review_scope, review_task_specs())
    started = time.monotonic()
    if publish:
        publish({"type": "review_status", "run": store.get_review_run(run_id)})
    try:
        _update_review_task(
            store, publish, run_id, "prepare", "running",
            input_data={"workspace": str(Path(workspace).expanduser().resolve()), "scope": review_scope},
        )
        browser = browse_fn or (
            lambda target: browse_project(
                target,
                include_static=review_scope["components"]["static"],
                include_git=review_scope["components"]["git"],
            )
        )
        browse = browser(workspace)
        _update_review_task(
            store, publish, run_id, "prepare", "complete",
            output_data={
                "file_count": int(browse.get("file_count") or 0),
                "git_branch": (browse.get("git") or {}).get("branch"),
                "test_detected": bool((browse.get("test") or {}).get("detected")),
            },
        )

        agent_context = ReviewContext(
            review_run_id=run_id,
            workspace=str(Path(workspace).expanduser().resolve()),
            scope=review_scope,
            browse=browse,
            trace_id=f"review-trace-{run_id}",
        ).to_dict()
        if harness is None:
            _update_review_task(
                store, publish, run_id, "project_review", "skipped",
                input_data={"reason": "local Harness is not configured"},
                output_data={"note": "未配置 Harness；保留确定性审查步骤。"},
            )
        else:
            _update_review_task(store, publish, run_id, "project_review", "running", input_data=agent_context)
            agent_result = harness.dispatch_review(run_id, "project_review", agent_context)
            agent_status = "complete" if agent_result.get("status") == "completed" else "error"
            _update_review_task(
                store, publish, run_id, "project_review", agent_status,
                output_data=agent_result,
                failure_reason=agent_result.get("failure_reason") if agent_status == "error" else None,
            )

        test = browse.get("test") or {}
        test_enabled = review_scope["components"]["tests"]
        static_enabled = review_scope["components"]["static"]
        effective_test_timeout = min(test_timeout, 20.0) if review_scope["mode"] == "fast" else test_timeout
        scan_runner = scan_fn or (
            (lambda target: scan_static(target, max_files=80, max_lines=300))
            if review_scope["mode"] == "fast" else scan_static
        )
        if test_enabled:
            _update_review_task(store, publish, run_id, "test_probe", "running", input_data=test)
        else:
            _update_review_task(store, publish, run_id, "test_probe", "skipped", output_data={"reason": "scope disabled tests"})
        if static_enabled:
            _update_review_task(store, publish, run_id, "static_scan", "running")
        else:
            _update_review_task(store, publish, run_id, "static_scan", "skipped", output_data={"reason": "scope disabled static scan"})

        # Independent read-only checks run concurrently and remain separate in
        # the review ledger, mirroring a pipeline graph rather than a spinner.
        with ThreadPoolExecutor(max_workers=2) as pool:
            test_future = (
                pool.submit((test_fn or run_test_probe), workspace, test, effective_test_timeout)
                if test_enabled and test.get("detected") else None
            )
            scan_future = (
                pool.submit(scan_runner, workspace)
                if static_enabled else None
            )
            if test_enabled:
                try:
                    probe = test_future.result() if test_future else {
                        "detected": False, "ran": False, "passed": None,
                    }
                    # Preserve detection metadata so both the report and Case
                    # signals identify the command producing the result.
                    browse["test"] = {**test, **probe}
                    _update_review_task(
                        store, publish, run_id, "test_probe", "complete", output_data=browse["test"],
                    )
                except Exception as exc:
                    browse["test"] = {
                        **test, "ran": False, "passed": None,
                        "execution_error": True,
                        "output_summary": "测试探测发生内部异常；未执行有效测试结果。",
                    }
                    _update_review_task(
                        store, publish, run_id, "test_probe", "error",
                        output_data=browse["test"],
                        failure_reason=f"test probe exception: {type(exc).__name__}",
                    )
            if static_enabled:
                try:
                    browse["static_scan"] = scan_future.result() if scan_future else {}
                    scan = browse["static_scan"]
                    _update_review_task(
                        store, publish, run_id, "static_scan", "complete",
                        output_data={
                            "todo_count": int(scan.get("todo_count") or 0),
                            "fixme_count": int(scan.get("fixme_count") or 0),
                            "error_handling_gap_count": len(scan.get("error_handling_gaps") or []),
                            "scanned_files": int(scan.get("scanned_files") or 0),
                        },
                    )
                except Exception as exc:
                    browse["static_scan"] = {
                        "todo_count": 0, "fixme_count": 0, "error_handling_gaps": [],
                        "scanned_files": 0, "error": "静态扫描发生内部异常。",
                    }
                    _update_review_task(
                        store, publish, run_id, "static_scan", "error",
                        output_data=browse["static_scan"],
                        failure_reason=f"static scan exception: {type(exc).__name__}",
                    )

        _update_review_task(store, publish, run_id, "case_handling", "running")
        browse["case_promotion"] = promote_test_failure(
            store, workspace, run_id, browse.get("test") or {}, case_intake=case_intake,
        )
        promotion = browse["case_promotion"]
        linked_case_ids = [promotion["case_id"]] if promotion.get("case_id") else []
        _update_review_task(
            store, publish, run_id, "case_handling", "complete",
            output_data={
                "triggered": bool(promotion.get("triggered")),
                "case_id": promotion.get("case_id"),
                "case_status": promotion.get("case_status"),
                "outcome": promotion.get("outcome"),
                "note": "仅测试超时或非零退出会进入 Case；修复和验证仍需 Case 审批。",
            },
        )
        # The LLM receives post-intake stats, so a newly linked Case is part
        # of the same drive's deterministic context rather than the next one.
        _update_review_task(store, publish, run_id, "summary", "running")
        stats = store.project_summary(workspace=workspace)
        llm = (llm_fn or (lambda w, b, s: generate_drive_summary(w, b, s)))(workspace, browse, stats)
        _update_review_task(
            store, publish, run_id, "summary", "complete",
            output_data={"status": llm.get("status"), "overall_status": (llm.get("summary") or {}).get("overall_status")},
        )
        duration = round(time.monotonic() - started, 2)
        store.finish_review_run(run_id, "complete", duration, browse, llm, linked_case_ids, None)
        if publish:
            run = store.get_review_run(run_id)
            publish({"type": "review_status", "run": run})
            # Keep existing clients functional while they migrate to the
            # Review Run event name.
            publish({"type": "drive_status", "run": run})
        return {"run_id": run_id, "status": "complete", "browse": browse, "llm": llm}
    except Exception as exc:  # pragma: no cover - defensive
        duration = round(time.monotonic() - started, 2)
        _update_review_task(store, publish, run_id, "summary", "error", failure_reason=str(exc))
        store.finish_review_run(run_id, "error", duration, None, None, [], str(exc))
        if publish:
            run = store.get_review_run(run_id)
            publish({"type": "review_status", "run": run})
            publish({"type": "drive_status", "run": run})
        return {"run_id": run_id, "status": "error", "error": str(exc)}
