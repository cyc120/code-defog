#!/usr/bin/env python3
"""Watch workspace file changes and append updates to AI_WORKLOG.md."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


DEFAULT_LANGUAGE = "zh"
DEFAULT_WORKLOG = "AI_WORKLOG.md"
DEFAULT_INTERVAL = 2.0
DEFAULT_MAX_FILES_PER_NOTE = 12
SKIP_DIRS = {
    ".cache",
    ".git",
    ".hg",
    ".mypy_cache",
    ".next",
    ".nuxt",
    ".pytest_cache",
    ".svn",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "env",
    "node_modules",
    "venv",
}
SKIP_FILES = {
    ".DS_Store",
}
STATE_VERSION = 1


@dataclass(frozen=True)
class Change:
    path: str
    state: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="自动监听文件变化并更新 AI_WORKLOG.md。")
    parser.add_argument("--workspace", default=".", help="要监听的项目根目录。")
    parser.add_argument("--file", default=DEFAULT_WORKLOG, help="工作日志文件名。")
    parser.add_argument("--language", choices=["zh", "en"], default=DEFAULT_LANGUAGE, help="工作日志语言。")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL, help="连续监听时的轮询间隔秒数。")
    parser.add_argument("--once", action="store_true", help="只检查一次文件变化，然后退出。")
    parser.add_argument("--state-file", default=None, help="快照状态文件路径；默认写入系统临时目录。")
    parser.add_argument("--max-files-per-note", type=int, default=DEFAULT_MAX_FILES_PER_NOTE, help="单条日志最多列出的文件数。")
    parser.add_argument("--quiet", action="store_true", help="减少终端输出。")
    parser.add_argument("--no-start-note", action="store_true", help="连续监听启动时不写入启动记录。")
    return parser.parse_args()


def default_state_file(workspace: Path, worklog_name: str) -> Path:
    digest = hashlib.sha256(f"{workspace}:{worklog_name}".encode("utf-8")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / "code-cctv" / f"{digest}.json"


def load_state(path: Path) -> dict[str, dict[str, int]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if payload.get("version") != STATE_VERSION:
        return {}
    files = payload.get("files")
    return files if isinstance(files, dict) else {}


def save_state(path: Path, workspace: Path, files: dict[str, dict[str, int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": STATE_VERSION,
        "workspace": str(workspace),
        "files": files,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def should_skip_dir(dirname: str) -> bool:
    return dirname in SKIP_DIRS


def should_skip_file(path: Path, workspace: Path, worklog_name: str, state_file: Path) -> bool:
    if path.name in SKIP_FILES:
        return True
    # update_worklog's atomic writer creates .{worklog}.*.tmp in the workspace
    # root; they are transient and must not be reported as file changes.
    if worklog_name and path.name.startswith(f".{worklog_name}.") and path.name.endswith(".tmp"):
        return True
    try:
        resolved = path.resolve()
    except OSError:
        return True
    if resolved == (workspace / worklog_name).resolve():
        return True
    if resolved == state_file.resolve():
        return True
    return False


def snapshot(workspace: Path, worklog_name: str, state_file: Path) -> dict[str, dict[str, int]]:
    files: dict[str, dict[str, int]] = {}
    for root, dirnames, filenames in os.walk(workspace):
        dirnames[:] = [dirname for dirname in dirnames if not should_skip_dir(dirname)]
        root_path = Path(root)
        for filename in filenames:
            path = root_path / filename
            if should_skip_file(path, workspace, worklog_name, state_file):
                continue
            try:
                stat = path.stat()
                relative = path.relative_to(workspace).as_posix()
            except OSError:
                continue
            files[relative] = {
                "mtime_ns": stat.st_mtime_ns,
                "size": stat.st_size,
            }
    return files


def diff_snapshots(
    previous: dict[str, dict[str, int]],
    current: dict[str, dict[str, int]],
) -> list[Change]:
    changes: list[Change] = []
    previous_paths = set(previous)
    current_paths = set(current)
    for path in sorted(current_paths - previous_paths):
        changes.append(Change(path, "已新增"))
    for path in sorted(previous_paths - current_paths):
        changes.append(Change(path, "已删除"))
    for path in sorted(previous_paths & current_paths):
        if previous[path] != current[path]:
            changes.append(Change(path, "已修改"))
    return changes


def safe_cell(value: str) -> str:
    # Mirror update_worklog.escape_cell so the round-trip through --touch
    # (split_escaped_values) preserves backslashes and pipes exactly.
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def summarize_changes(changes: list[Change], limit: int) -> str:
    visible = changes[:limit]
    summary = "、".join(f"{change.path}（{change.state}）" for change in visible)
    remaining = len(changes) - len(visible)
    if remaining > 0:
        summary += f"、另有 {remaining} 个文件变化"
    return f"检测到 {len(changes)} 个文件变化：{summary}。"


def update_worklog(
    workspace: Path,
    worklog_name: str,
    language: str,
    phase: str,
    note: str,
    evidence: str,
    changes: list[Change],
    max_files_per_note: int,
) -> None:
    updater = Path(__file__).resolve().with_name("update_worklog.py")
    command = [
        sys.executable,
        str(updater),
        "--workspace",
        str(workspace),
        "--file",
        worklog_name,
        "--language",
        language,
        "--status",
        "监听中" if language == "zh" else "Watching",
        "--focus",
        "自动更新 AI_WORKLOG.md：记录交互、代码输出和文件变化。"
        if language == "zh"
        else "Auto-updating AI_WORKLOG.md with interactions, code output, and file changes.",
        "--phase",
        phase,
        "--note",
        note,
        "--evidence",
        evidence,
    ]
    for change in changes[:max_files_per_note]:
        command.extend(
            [
                "--touch",
                f"{safe_cell(change.path)}|自动监听检测到文件变化|{change.state}",
            ]
        )
    subprocess.run(command, check=True)


def update_worklog_safe(quiet: bool, *args: object, **kwargs: object) -> bool:
    """Best-effort updater: a single failure must not kill the watcher loop."""
    try:
        update_worklog(*args, **kwargs)  # type: ignore[arg-type]
        return True
    except subprocess.CalledProcessError as error:
        print_status(quiet, f"更新工作日志失败，继续运行：{error}")
        return False


def print_status(quiet: bool, message: str) -> None:
    if not quiet:
        print(message, flush=True)


def run_once(args: argparse.Namespace, workspace: Path, state_file: Path) -> int:
    previous = load_state(state_file)
    current = snapshot(workspace, args.file, state_file)
    if not previous:
        save_state(state_file, workspace, current)
        update_worklog_safe(
            args.quiet,
            workspace,
            args.file,
            args.language,
            "自动监听",
            "已建立文件变化快照；后续检测到代码或文档变化时会自动写入日志。",
            "watch_worklog.py --once",
            [],
            args.max_files_per_note,
        )
        print_status(args.quiet, f"Baseline created: {state_file}")
        return 0
    changes = diff_snapshots(previous, current)
    save_state(state_file, workspace, current)
    if changes:
        update_worklog_safe(
            args.quiet,
            workspace,
            args.file,
            args.language,
            "自动更新",
            summarize_changes(changes, args.max_files_per_note),
            "watch_worklog.py --once",
            changes,
            args.max_files_per_note,
        )
        print_status(args.quiet, f"Updated worklog with {len(changes)} file change(s).")
    else:
        print_status(args.quiet, "No file changes detected.")
    return 0


def run_forever(args: argparse.Namespace, workspace: Path, state_file: Path) -> int:
    current = snapshot(workspace, args.file, state_file)
    save_state(state_file, workspace, current)
    if not args.no_start_note:
        update_worklog_safe(
            args.quiet,
            workspace,
            args.file,
            args.language,
            "自动监听",
            "已启动文件变化监听；后续代码或文档变化会自动写入日志。",
            "watch_worklog.py",
            [],
            args.max_files_per_note,
        )
    print_status(args.quiet, f"Watching {workspace} every {args.interval:g}s. State: {state_file}")
    previous = current
    while True:
        time.sleep(max(args.interval, 0.2))
        current = snapshot(workspace, args.file, state_file)
        changes = diff_snapshots(previous, current)
        if not changes:
            previous = current
            continue
        save_state(state_file, workspace, current)
        update_worklog_safe(
            args.quiet,
            workspace,
            args.file,
            args.language,
            "自动更新",
            summarize_changes(changes, args.max_files_per_note),
            "watch_worklog.py",
            changes,
            args.max_files_per_note,
        )
        print_status(args.quiet, f"Updated worklog with {len(changes)} file change(s).")
        previous = current


def main() -> None:
    args = parse_args()
    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.is_dir():
        raise SystemExit(f"Workspace does not exist or is not a directory: {workspace}")
    state_file = Path(args.state_file).expanduser().resolve() if args.state_file else default_state_file(workspace, args.file)
    if args.once:
        raise SystemExit(run_once(args, workspace, state_file))
    raise SystemExit(run_forever(args, workspace, state_file))


if __name__ == "__main__":
    main()
