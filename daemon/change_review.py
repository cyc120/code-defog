"""Bounded, read-only risk hypotheses for local Python changes.

These hints identify changed functions worth testing. They are not findings:
no hypothesis becomes a Case without an observed failing probe.
"""

from __future__ import annotations

import ast
import re
import subprocess
import time
from pathlib import Path
from typing import Any


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)
_MAX_FILES = 80
_MAX_BYTES = 200_000
_MAX_HYPOTHESES = 12


def _git(workspace: Path, *args: str) -> bytes | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), *args], capture_output=True,
            timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def _changed_lines(workspace: Path, name: str, untracked: bool) -> set[int] | None:
    if untracked:
        return None  # Every function in a new file is part of this change.
    raw = _git(workspace, "diff", "--no-ext-diff", "--unified=0", "HEAD", "--", name)
    if raw is None:
        return set()
    lines: set[int] = set()
    for start_text, count_text in _HUNK.findall(raw.decode("utf-8", errors="replace")):
        start = int(start_text)
        count = int(count_text) if count_text else 1
        if count == 0:
            lines.add(max(1, start))  # A deletion affects the adjacent new line.
        else:
            lines.update(range(start, start + min(count, 10_000)))
    return lines


def _functions(tree: ast.AST, prefix: str = "") -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    found = []
    for node in getattr(tree, "body", []):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            name = f"{prefix}{node.name}"
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.append((name, node))
            found.extend(_functions(node, f"{name}."))
    return found


def _calls(node: ast.AST) -> set[str]:
    names = set()
    for item in ast.walk(node):
        if not isinstance(item, ast.Call):
            continue
        func = item.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def _attributes(node: ast.AST) -> set[str]:
    return {item.attr for item in ast.walk(node) if isinstance(item, ast.Attribute)}


def _hypotheses(
    name: str, line: int, function: str, calls: set[str], attributes: set[str],
) -> list[dict[str, str | int]]:
    candidates: list[tuple[str, str, str]] = []
    if calls & {"Popen", "communicate", "taskkill", "killpg", "TerminateJobObject"}:
        candidates.append((
            "process_tree", "子进程退出与超时边界",
            "让父进程先退出、子进程继续持有输出；核对超时、退出码和残留进程。",
        ))
    if ("config" in function.lower() or "state" in function.lower() or "command" in function.lower()) and (
        calls & {"load", "loads", "read_text"} and calls & {"get", "parse", "validate_config"}
    ):
        candidates.append((
            "config_boundary", "缺失和异常配置输入",
            "分别输入缺字段、空值和错误类型；确认必填校验没有被默认值绕过。",
        ))
    if "stat" in calls and attributes & {"st_mtime_ns", "st_ctime_ns"}:
        candidates.append((
            "file_change", "文件变化与替换边界",
            "保持文件大小并恢复修改时间，或在写入时替换文件；核对变化是否仍被发现。",
        ))
    if "replace" in calls and "fsync" in calls:
        candidates.append((
            "atomic_write", "原子写入的中断边界",
            "在临时文件写入和替换之间中断；核对旧数据、状态文件与临时文件。",
        ))
    return [
        {"kind": kind, "title": title, "file": name, "function": function,
         "line": line, "probe": probe, "status": "unverified"}
        for kind, title, probe in candidates
    ]


def review_local_changes(workspace: str | Path) -> dict[str, Any]:
    """Inspect changed Python functions against HEAD without executing code."""
    root = Path(workspace).expanduser().resolve()
    if _git(root, "rev-parse", "--show-toplevel") is None:
        return {"status": "not_git", "changed_files": 0, "functions": [], "hypotheses": []}
    has_head = _git(root, "rev-parse", "--verify", "HEAD") is not None
    tracked = (
        _git(root, "diff", "--name-only", "-z", "HEAD", "--") if has_head
        else _git(root, "ls-files", "--cached", "-z", "--")
    )
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "-z", "--")
    if tracked is None or untracked is None:
        return {"status": "error", "changed_files": 0, "functions": [], "hypotheses": []}
    untracked_names = {part.decode("utf-8", errors="surrogateescape") for part in untracked.split(b"\0") if part}
    names = {part.decode("utf-8", errors="surrogateescape") for part in tracked.split(b"\0") if part}
    names.update(untracked_names)
    if not names:
        return {"status": "no_changes", "changed_files": 0, "functions": [], "hypotheses": []}

    functions: list[dict[str, str | int]] = []
    hypotheses: list[dict[str, str | int]] = []
    inspected = 0
    deadline = time.monotonic() + 10
    timed_out = False
    candidates = [name for name in sorted(names) if name.endswith(".py") and
                  not any(part in {"tests", "test", ".venv", "venv"} for part in Path(name).parts)]
    for name in candidates[:_MAX_FILES]:
        if time.monotonic() >= deadline:
            timed_out = True
            break
        path = root / name
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_BYTES:
                continue
            path.resolve().relative_to(root)
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=name)
        except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
            continue
        inspected += 1
        changed = _changed_lines(root, name, name in untracked_names or not has_head)
        for function, node in _functions(tree):
            start = min([node.lineno, *(item.lineno for item in node.decorator_list)])
            end = getattr(node, "end_lineno", node.lineno)
            if changed is not None and not any(start <= line <= end for line in changed):
                continue
            functions.append({"file": name, "function": function, "line": start})
            if len(hypotheses) < _MAX_HYPOTHESES:
                hypotheses.extend(_hypotheses(
                    name, start, function, _calls(node), _attributes(node),
                ))
                hypotheses = hypotheses[:_MAX_HYPOTHESES]

    return {
        "status": "ready", "base": "HEAD" if has_head else "empty", "changed_files": len(names),
        "inspected_python_files": inspected, "functions": functions[:80],
        "hypotheses": hypotheses,
        "truncated": timed_out or len(candidates) > _MAX_FILES or len(functions) > 80,
    }
