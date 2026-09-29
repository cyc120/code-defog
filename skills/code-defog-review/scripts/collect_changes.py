"""Portable, read-only review targets for local code changes.

This script belongs to the Code Defog Review skill and has no dependency on
the Code Defog application or third-party Python packages. Hints are not bugs.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)
_MAX_FILES = 80
_MAX_BYTES = 200_000
_MAX_HYPOTHESES = 12
_MAX_SOURCE_FILES = 60
_MAX_PREVIEW_LINES = 8
_SOURCE_LANGUAGES = {
    ".py": "Python", ".pyi": "Python", ".js": "JavaScript", ".jsx": "JavaScript",
    ".mjs": "JavaScript", ".cjs": "JavaScript", ".ts": "TypeScript",
    ".tsx": "TypeScript", ".mts": "TypeScript", ".cts": "TypeScript",
    ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin",
    ".kts": "Kotlin", ".c": "C", ".h": "C/C++", ".cc": "C++",
    ".cpp": "C++", ".cxx": "C++", ".hpp": "C++", ".cs": "C#",
    ".rb": "Ruby", ".php": "PHP", ".swift": "Swift", ".sh": "Shell",
    ".ps1": "PowerShell", ".sql": "SQL", ".vue": "Vue",
    ".svelte": "Svelte", ".html": "HTML", ".css": "CSS",
}


def _git(workspace: Path, *args: str) -> bytes | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), *args], capture_output=True,
            timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def _changed_lines(workspace: Path, name: str, base_commit: str | None) -> set[int] | None:
    if base_commit is None:
        return None  # Every function in a new file is part of this change.
    raw = _git(workspace, "diff", "--no-ext-diff", "--unified=0", base_commit, "--", name)
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


def _review_targets(
    root: Path, names: set[str], untracked_names: set[str], base_commit: str | None,
    deadline: float,
) -> tuple[list[dict[str, Any]], dict[str, int], int, bool]:
    language_counts: dict[str, int] = {}
    source_names = []
    for name in sorted(names):
        language = _SOURCE_LANGUAGES.get(Path(name).suffix.lower())
        if language:
            source_names.append((name, language))
            language_counts[language] = language_counts.get(language, 0) + 1
    targets: list[dict[str, Any]] = []
    truncated = len(source_names) > _MAX_SOURCE_FILES
    for name, language in source_names[:_MAX_SOURCE_FILES]:
        if time.monotonic() >= deadline:
            truncated = True
            break
        path = root / name
        entry: dict[str, Any] = {"file": name, "language": language}
        try:
            if path.is_symlink():
                entry["status"] = "symlink"
            elif not path.is_file():
                entry["status"] = "missing_or_deleted"
            elif path.stat().st_size > _MAX_BYTES:
                entry["status"] = "too_large"
            else:
                path.resolve().relative_to(root)
                lines = path.read_text(encoding="utf-8").splitlines()
                changed = _changed_lines(root, name, None if name in untracked_names else base_commit)
                selected = range(1, len(lines) + 1) if changed is None else sorted(changed)
                preview = [{"line": number, "text": lines[number - 1][:200]}
                           for number in selected if 1 <= number <= len(lines)]
                entry.update({"status": "located", "changed_lines": len(changed) if changed is not None else len(lines),
                              "preview": preview[:_MAX_PREVIEW_LINES]})
                if len(preview) > _MAX_PREVIEW_LINES:
                    entry["preview_truncated"] = True
        except (OSError, UnicodeDecodeError, ValueError):
            entry["status"] = "unreadable"
        targets.append(entry)
    skipped = sum(target["status"] != "located" for target in targets)
    return targets, language_counts, skipped, truncated


def _check_candidates(root: Path, languages: dict[str, int]) -> list[dict[str, Any]]:
    """Suggest manifest-backed commands; never execute project scripts here."""
    checks: list[dict[str, Any]] = []

    def add(language: str, source: str, kind: str, command: list[str]) -> None:
        checks.append({"language": language, "source": source, "kind": kind,
                       "command": command, "status": "not_run"})

    package = root / "package.json"
    if package.is_file() and any(name in languages for name in ("JavaScript", "TypeScript", "Vue", "Svelte")):
        try:
            data = json.loads(package.read_text(encoding="utf-8"))
            scripts = data.get("scripts", {})
            if isinstance(scripts, dict):
                manager = ("pnpm" if (root / "pnpm-lock.yaml").is_file() else
                           "yarn" if (root / "yarn.lock").is_file() else
                           "bun" if (root / "bun.lock").is_file() or (root / "bun.lockb").is_file() else "npm")
                standard = ("typecheck", "check", "lint", "build", "test")
                additional = sorted(name for name in scripts if isinstance(name, str) and
                                    name.startswith(("typecheck:", "check:", "lint:", "test:")))[:12]
                for name in (*standard, *additional):
                    if isinstance(scripts.get(name), str):
                        add("JavaScript/TypeScript", "package.json", name,
                            [manager, "run", name])
        except (OSError, UnicodeDecodeError, ValueError):
            pass
    if "Go" in languages and (root / "go.mod").is_file():
        add("Go", "go.mod", "build", ["go", "build", "./..."])
        add("Go", "go.mod", "test", ["go", "test", "./..."])
    if "Rust" in languages and (root / "Cargo.toml").is_file():
        add("Rust", "Cargo.toml", "check", ["cargo", "check"])
        add("Rust", "Cargo.toml", "test", ["cargo", "test"])
    if "Java" in languages or "Kotlin" in languages:
        if (root / "pom.xml").is_file():
            add("Java/Kotlin", "pom.xml", "build", ["mvn", "-DskipTests", "compile"])
        elif (root / "gradlew").is_file() or (root / "gradlew.bat").is_file():
            add("Java/Kotlin", "Gradle wrapper", "build", ["./gradlew", "classes"])
    if "C#" in languages and any(root.glob("*.sln")):
        add("C#", "*.sln", "build", ["dotnet", "build"])
    return checks


def review_local_changes(workspace: str | Path, base: str = "HEAD") -> dict[str, Any]:
    """Locate changed source lines and Python functions without executing project code."""
    requested = Path(workspace).expanduser().resolve()
    top = _git(requested, "rev-parse", "--show-toplevel")
    if top is None:
        return {"status": "not_git", "changed_files": 0, "functions": [], "hypotheses": []}
    root = Path(os.fsdecode(top.rstrip(b"\r\n"))).resolve()
    resolved_base = _git(root, "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}")
    if resolved_base is None and base != "HEAD":
        return {"status": "invalid_base", "workspace": str(root), "base": base,
                "changed_files": 0, "functions": [], "hypotheses": []}
    base_commit = resolved_base.decode("ascii").strip() if resolved_base else None
    tracked = (
        _git(root, "diff", "--name-only", "-z", base_commit, "--") if base_commit
        else _git(root, "ls-files", "--cached", "-z", "--")
    )
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "-z", "--")
    if tracked is None or untracked is None:
        return {"status": "error", "changed_files": 0, "functions": [], "hypotheses": []}
    untracked_names = {part.decode("utf-8", errors="surrogateescape") for part in untracked.split(b"\0") if part}
    names = {part.decode("utf-8", errors="surrogateescape") for part in tracked.split(b"\0") if part}
    names.update(untracked_names)
    if not names:
        return {"status": "no_changes", "workspace": str(root), "base": base,
                "changed_files": 0, "functions": [], "hypotheses": []}

    functions: list[dict[str, str | int]] = []
    hypotheses: list[dict[str, str | int]] = []
    skipped: list[dict[str, str]] = []
    skipped_count = 0

    def skip(name: str, reason: str) -> None:
        nonlocal skipped_count
        skipped_count += 1
        if len(skipped) < 30:
            skipped.append({"file": name, "reason": reason})

    inspected = 0
    deadline = time.monotonic() + 10
    timed_out = False
    hints_truncated = False
    candidates = [name for name in sorted(names) if name.endswith(".py") and
                  not any(part in {"tests", "test", ".venv", "venv"} for part in Path(name).parts)]
    for name in candidates[:_MAX_FILES]:
        if time.monotonic() >= deadline:
            timed_out = True
            break
        path = root / name
        try:
            if path.is_symlink():
                skip(name, "symlink")
                continue
            if not path.is_file():
                skip(name, "missing_or_deleted")
                continue
            if path.stat().st_size > _MAX_BYTES:
                skip(name, "too_large")
                continue
            path.resolve().relative_to(root)
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=name)
        except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
            skip(name, "unreadable_or_invalid_python")
            continue
        inspected += 1
        changed = _changed_lines(root, name, None if name in untracked_names else base_commit)
        mapped = False
        for function, node in _functions(tree):
            start = min([node.lineno, *(item.lineno for item in node.decorator_list)])
            end = getattr(node, "end_lineno", node.lineno)
            if changed is not None and not any(start <= line <= end for line in changed):
                continue
            mapped = True
            functions.append({"file": name, "function": function, "line": start})
            hints = _hypotheses(name, start, function, _calls(node), _attributes(node))
            remaining = _MAX_HYPOTHESES - len(hypotheses)
            if len(hints) > remaining:
                hints_truncated = True
            hypotheses.extend(hints[:remaining])
        if not mapped:
            skip(name, "no_changed_function")

    review_targets, languages, skipped_source_files, source_truncated = _review_targets(
        root, names, untracked_names, base_commit, time.monotonic() + 10,
    )

    return {
        "status": "ready", "workspace": str(root), "base": base if base_commit else "empty",
        "base_commit": base_commit, "changed_files": len(names),
        "non_python_files": len(names) - len([name for name in names if name.endswith(".py")]),
        "inspected_python_files": inspected, "functions": functions[:80],
        "hypotheses": hypotheses, "skipped_python_files": skipped_count, "skipped": skipped,
        "languages": languages, "non_source_files": len(names) - sum(languages.values()),
        "review_targets": review_targets, "skipped_source_files": skipped_source_files,
        "check_candidates": _check_candidates(root, languages),
        "truncated": (source_truncated or timed_out or hints_truncated or len(candidates) > _MAX_FILES
                      or len(functions) > 80 or skipped_count > len(skipped)),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Locate changed source lines, Python functions, and project checks.")
    parser.add_argument("--workspace", default=".", help="Local Git worktree to inspect (default: current directory)")
    parser.add_argument("--base", default="HEAD", help="Commit/ref to compare with the current worktree (default: HEAD)")
    args = parser.parse_args()
    print(json.dumps(review_local_changes(args.workspace, args.base), ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
