"""Read-only Project Review Agent used by the local Harness.

This Agent does not execute commands, write the working tree, create approval
grants, or change Case state.  Test execution and static scanning remain
separate deterministic Review Tasks owned by ``daemon.drive``.
"""

from __future__ import annotations

from typing import Any


def run(context: dict[str, Any]) -> dict[str, Any]:
    """Return bounded structural observations from a ``ReviewContext``."""
    browse = context.get("browse") if isinstance(context.get("browse"), dict) else {}
    git = browse.get("git") if isinstance(browse.get("git"), dict) else {}
    static_scan = browse.get("static_scan") if isinstance(browse.get("static_scan"), dict) else {}
    gaps = static_scan.get("error_handling_gaps")
    gap_count = len(gaps) if isinstance(gaps, list) else 0
    observations = [
        f"已读取 {int(browse.get('file_count') or 0)} 个文件，识别 {int(browse.get('symbol_total') or 0)} 个符号。",
        (
            f"Git: {git.get('branch') or '非 Git 项目'}，"
            f"未提交变更 {int(git.get('dirty_count') or 0)} 个。"
        ),
    ]
    if gap_count:
        observations.append(f"静态扫描已标记 {gap_count} 处待人工判断的错误处理风险。")
    return {
        "agent": "project_review",
        "action": "read_only_project_review",
        "status": "completed",
        "read_only": True,
        "observations": observations,
        "file_count": int(browse.get("file_count") or 0),
        "symbol_total": int(browse.get("symbol_total") or 0),
        "static_gap_count": gap_count,
        "note": "本地确定性项目审查；不创建或推进 Case，不执行修复或发布。",
    }
