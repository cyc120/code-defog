#!/usr/bin/env python3
"""Create and update a Markdown worklog for AI-assisted coding sessions."""

from __future__ import annotations

import argparse
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from event_client import post_event


START = "<!-- code-defog:start -->"
END = "<!-- code-defog:end -->"
LEGACY_CODE_CCTV_START = "<!-- code-cctv:start -->"
LEGACY_CODE_CCTV_END = "<!-- code-cctv:end -->"
LEGACY_START = "<!-- show-your-shit:start -->"
LEGACY_END = "<!-- show-your-shit:end -->"
OLDER_LEGACY_START = "<!-- ai-work-monitor:start -->"
OLDER_LEGACY_END = "<!-- ai-work-monitor:end -->"
DEFAULT_TZ = "Asia/Shanghai"
DEFAULT_LANGUAGE = "zh"
DEFAULT_CONVERSATION_ID = "default"

TEXT = {
    "en": {
        "default_status": "Scouting",
        "default_focus": "Preparing to inspect the project.",
        "pending": "Pending.",
        "none": "None yet.",
        "last_updated": "Last updated",
        "status": "Status",
        "focus": "Current focus",
        "pyramid": "Information Pyramid",
        "module_map": "Module Map",
        "flow": "Flow",
        "live_notes": "Live Notes",
        "files": "Files Touched",
        "functions": "Function Map",
        "segments": "Code Segment Notes",
        "decisions": "Decisions",
        "validation": "Validation",
        "beginner_checks": "Beginner Verification Checklist",
        "risks": "Risks And Open Questions",
        "final": "Final Summary",
        "priority_headers": ["Priority", "What to see first", "Evidence / next step"],
        "module_headers": ["Module", "Related code", "Responsibility", "Depends on", "Risk", "How to verify"],
        "live_headers": ["Time", "Phase", "What changed", "Evidence"],
        "file_headers": ["File", "Purpose", "State"],
        "function_headers": ["Location", "Function", "Purpose", "How to verify"],
        "segment_headers": ["Location", "Code segment", "What it does", "Beginner check"],
        "decision_headers": ["Decision", "Reason", "Tradeoff"],
        "validation_headers": ["Check", "Result", "Notes"],
        "beginner_check_headers": ["What to check", "How to check", "Expected result"],
        "flow_nodes": [
            "Goal received",
            "Read context",
            "Plan change",
            "Edit files",
            "Validate",
            "Summarize",
            "Current status",
        ],
    },
    "zh": {
        "default_status": "侦察中",
        "default_focus": "准备查看项目上下文。",
        "pending": "待完成。",
        "none": "暂无。",
        "last_updated": "最后更新",
        "status": "状态",
        "focus": "当前关注",
        "pyramid": "信息金字塔",
        "module_map": "模块图谱",
        "flow": "流程图",
        "live_notes": "实时记录",
        "files": "涉及文件",
        "functions": "函数定位",
        "segments": "代码片段说明",
        "decisions": "决策记录",
        "validation": "验证结果",
        "beginner_checks": "初学者核对清单",
        "risks": "风险与待确认",
        "final": "最终总结",
        "priority_headers": ["优先级", "先看什么", "证据 / 下一步"],
        "module_headers": ["模块", "相关代码", "职责", "依赖", "风险", "怎么核对"],
        "live_headers": ["时间", "阶段", "发生了什么", "证据"],
        "file_headers": ["文件", "用途", "状态"],
        "function_headers": ["位置", "函数", "作用", "怎么核对"],
        "segment_headers": ["位置", "代码片段", "这段在做什么", "初学者核对点"],
        "decision_headers": ["决策", "原因", "取舍"],
        "validation_headers": ["检查", "结果", "备注"],
        "beginner_check_headers": ["要核对什么", "怎么核对", "预期结果"],
        "flow_nodes": [
            "收到目标",
            "阅读上下文",
            "制定改动方案",
            "编辑文件",
            "验证",
            "总结",
            "当前状态",
        ],
    },
}
HEADING_ALIASES = {
    "priority": ["Information Pyramid", "信息金字塔"],
    "modules": ["Module Map", "模块图谱"],
    "live_notes": ["Live Notes", "实时记录"],
    "files": ["Files Touched", "涉及文件"],
    "functions": ["Function Map", "函数定位"],
    "segments": ["Code Segment Notes", "代码片段说明"],
    "decisions": ["Decisions", "决策记录"],
    "validation": ["Validation", "验证结果"],
    "beginner_checks": ["Beginner Verification Checklist", "初学者核对清单"],
    "risks": ["Risks And Open Questions", "风险与待确认"],
    "final": ["Final Summary", "最终总结"],
}
ZH_CELL_TRANSLATIONS = {
    "Plugin creation": "插件创建",
    "Validation": "验证",
    "Finish": "完成",
    "Rename": "重命名",
    "Polish": "打磨",
    "Updated": "已更新",
    "Added": "已新增",
    "Passed": "通过",
    "Blocked": "受阻",
    "Created plugin manifest, skill instructions, skill UI metadata, and update_worklog.py.": "已创建插件清单、技能说明、技能界面元数据和 update_worklog.py。",
    "System python lacked PyYAML, so validation moved to the bundled Codex Python runtime.": "系统 Python 缺少 PyYAML，因此改用 Codex 自带 Python 运行校验。",
    "Installed PyYAML into /tmp/codex-plugin-validate-pyyaml for validator-only use.": "已把 PyYAML 安装到 /tmp/codex-plugin-validate-pyyaml，仅用于本次校验。",
    "Official skill and plugin validators passed; final summary written.": "官方技能和插件校验均已通过，并已写入最终总结。",
    "Renamed plugin, skill, marketplace entry, display name, and invocation prompt.": "已重命名插件、技能、插件市场入口、显示名和调用提示。",
    "Updated generated worklog title and section markers while keeping legacy marker compatibility.": "已更新生成的工作日志标题和区块标记，同时保留旧标记兼容。",
    "Plugin metadata and UI prompts": "插件元数据和界面提示词",
    "Monitoring workflow instructions": "监控工作流说明",
    "Skill UI metadata": "技能界面元数据",
    "Deterministic Markdown worklog updater": "确定性的 Markdown 工作日志更新脚本",
    "Plugin name and display copy": "插件名称和显示文案",
    "Skill name, trigger, and script path": "技能名称、触发词和脚本路径",
    "Marketplace entry now points to show-your-shit": "历史记录：插件市场入口曾指向旧名 show-your-shit",
    "Generated title, markers, and legacy compatibility": "生成标题、区块标记和旧版兼容",
    "Use a plugin-backed skill": "使用插件承载 skill",
    "The user asked for a skill or plugin, and a plugin makes the monitor visible in Codex plugin UI": "用户要求技能或插件，而插件能在 Codex 插件界面中可见",
    "Requires installing/enabling the local plugin after creation": "创建后需要安装或启用本地插件",
    "Use show-your-shit as the machine name": "历史记录：曾使用 show-your-shit 作为机器名",
    "Codex plugin and skill IDs require lowercase hyphen-case": "Codex 插件和 skill ID 需要小写短横线命名",
    "The user-facing display name remains show your shit": "历史记录：旧显示名是 show your shit",
    "system python validator": "系统 Python 校验器",
    "temporary PyYAML install": "临时安装 PyYAML",
    "update_worklog.py syntax": "update_worklog.py 语法检查",
    "legacy marker migration": "旧标记迁移",
    "plugin validate_plugin.py after cachebuster": "cachebuster 后插件校验",
    "No syntax errors": "无语法错误",
    "Missing PyYAML in system python": "系统 Python 缺少 PyYAML",
    "Installed under /tmp for validation only": "仅安装在 /tmp 下用于校验",
    "Skill is valid": "skill 有效",
    "Plugin validation passed": "插件校验通过",
    "py_compile completed without errors": "py_compile 已无错误完成",
    "Existing AI_WORKLOG.md was rewritten into show-your-shit markers": "历史记录：曾将 AI_WORKLOG.md 重写为旧版 show-your-shit 标记",
    "Example monitor regenerated with show-your-shit markers": "历史记录：示例监控文件曾用旧版 show-your-shit 标记重新生成",
    "Skill UI 文案改为中文导向": "skill 界面文案改为中文导向",
    "codex plugin add show-your-shit@personal after function map": "历史记录：旧名函数定位版本插件安装",
}
ZH_SUBSTRING_TRANSLATIONS = {
    " and /": " 和 /",
    " after function map": "（函数定位版本）",
    "技能 和": "技能和",
    "插件 界面": "插件界面",
    "官方 skill": "官方技能",
    "skill 和": "技能和",
    "skill、": "技能、",
    "skill 或": "技能或",
    "skill 说明": "技能说明",
    "skill 界面": "技能界面",
    "skill 名称": "技能名称",
    "skill UI": "技能界面",
    "skill 校验": "技能校验",
    "skill 有效": "技能有效",
    "plugin 校验": "插件校验",
    "plugin、": "插件、",
    "skill ID": "技能标识",
    "和 技能标识 需要": "和技能标识需要",
    "要求 技能或插件": "要求技能或插件",
    "worklog 标题": "工作日志标题",
    "生成的 工作日志": "生成的工作日志",
    "marketplace 入口": "插件市场入口",
    "UI 文案": "界面文案",
    "、UI": "、界面",
    "技能界面 文案": "技能界面文案",
    "说明里的 技能、marketplace、worklog、UI": "说明里的技能、插件市场、工作日志、界面",
    "说明里的 技能、marketplace、worklog、界面": "说明里的技能、插件市场、工作日志、界面",
    "技能、marketplace、worklog、界面": "技能、插件市场、工作日志、界面",
    "补充 技能、plugin、界面": "补充技能、插件、界面",
}


@dataclass
class Worklog:
    status: str
    focus: str
    priority: list[list[str]] = field(default_factory=list)
    modules: list[list[str]] = field(default_factory=list)
    live_notes: list[list[str]] = field(default_factory=list)
    files: list[list[str]] = field(default_factory=list)
    functions: list[list[str]] = field(default_factory=list)
    segments: list[list[str]] = field(default_factory=list)
    decisions: list[list[str]] = field(default_factory=list)
    validation: list[list[str]] = field(default_factory=list)
    beginner_checks: list[list[str]] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    final_summary: str = "Pending."


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update AI_WORKLOG.md.")
    parser.add_argument("--workspace", default=".", help="Workspace root containing the worklog.")
    parser.add_argument("--file", default="AI_WORKLOG.md", help="Worklog filename.")
    parser.add_argument("--timezone", default=DEFAULT_TZ, help="IANA timezone for timestamps.")
    parser.add_argument(
        "--language",
        choices=sorted(TEXT),
        default=DEFAULT_LANGUAGE,
        help="Template language for generated headings and default labels.",
    )
    parser.add_argument("--status", default=None, help="Current status label.")
    parser.add_argument("--focus", default=None, help="Current focus sentence.")
    parser.add_argument("--phase", default=None, help="Phase for a new live note.")
    parser.add_argument("--note", default=None, help="Text for a new live note.")
    parser.add_argument("--evidence", default="", help="Evidence for a new live note.")
    parser.add_argument(
        "--priority",
        "--top",
        dest="priority",
        action="append",
        default=[],
        help="Information pyramid row: priority|what to see first|evidence or next step.",
    )
    parser.add_argument(
        "--module",
        action="append",
        default=[],
        help="Module row: module|related code|responsibility|depends on|risk|how to verify.",
    )
    parser.add_argument("--touch", action="append", default=[], help="Touched file row: path|purpose|state.")
    parser.add_argument("--function", action="append", default=[], help="Function row: location|function|purpose|how to verify.")
    parser.add_argument("--segment", action="append", default=[], help="Code segment row: location|segment|what it does|beginner check.")
    parser.add_argument("--decision", action="append", default=[], help="Decision row: decision|reason|tradeoff.")
    parser.add_argument("--check", action="append", default=[], help="Validation row: check|result|notes.")
    parser.add_argument("--beginner-check", action="append", default=[], help="Beginner checklist row: what|how|expected.")
    parser.add_argument("--risk", action="append", default=[], help="Risk or open question bullet.")
    parser.add_argument("--final", default=None, help="Final summary Markdown.")
    return parser.parse_args()


# Windows CPython ships without the IANA tzdata database, so
# ZoneInfo("Asia/Shanghai") raises ZoneInfoNotFoundError. These fixed offsets
# (no DST) cover the project's likely zones so the worklog still renders there
# without pulling in the third-party tzdata wheel.
_FIXED_TZ_OFFSETS = {
    "Asia/Shanghai": 8 * 3600,
    "Asia/Taipei": 8 * 3600,
    "Asia/Hong_Kong": 8 * 3600,
    "Asia/Singapore": 8 * 3600,
    "Asia/Tokyo": 9 * 3600,
    "Asia/Kolkata": 5 * 3600 + 1800,
    "Australia/Sydney": 10 * 3600,
}


def load_tz(timezone_name: str) -> tzinfo:
    """Resolve an IANA timezone, falling back to a fixed offset on systems
    (notably Windows) that lack the tzdata database."""
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        offset = _FIXED_TZ_OFFSETS.get(timezone_name)
        if offset is not None:
            return timezone(timedelta(seconds=offset), name=timezone_name)
        # Unknown zone: fixed UTC+8 to match the project default rather than crash.
        return timezone(timedelta(hours=8), name="UTC+8")


def now_text(timezone: str) -> str:
    return datetime.now(load_tz(timezone)).strftime("%Y-%m-%d %H:%M:%S %Z")


def conversation_id() -> str:
    """Use the host Codex thread identity without reading conversation content."""
    for key in ("CODEX_THREAD_ID", "CODEX_CONVERSATION_ID", "CODEX_SESSION_ID"):
        value = os.environ.get(key, "").strip()
        if value:
            return value[:200]
    return DEFAULT_CONVERSATION_ID


def conversation_name() -> str:
    for key in ("CODEX_THREAD_TITLE", "CODEX_CONVERSATION_TITLE"):
        value = os.environ.get(key, "").strip()
        if value:
            return value[:200]
    return ""


def split_escaped_values(raw: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(raw):
        char = raw[index]
        if char == "\\" and index + 1 < len(raw) and raw[index + 1] in {"|", "\\"}:
            current.append(raw[index + 1])
            index += 2
            continue
        if char == "|":
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
        index += 1
    parts.append("".join(current).strip())
    return parts


def split_row(raw: str, size: int) -> list[str]:
    parts = split_escaped_values(raw)
    if len(parts) < size:
        parts.extend([""] * (size - len(parts)))
    return parts[:size]


def single_line(value: str) -> str:
    """Collapse all whitespace (including newlines) to single spaces."""
    return " ".join(value.split())


def escape_cell(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")


def parse_table(lines: list[str], headings: list[str], columns: int) -> list[list[str]]:
    start = find_heading(lines, headings)
    if start is None:
        return []
    rows: list[list[str]] = []
    for line in lines[start + 4 :]:
        if line.startswith("## "):
            break
        if not line.startswith("|"):
            continue
        raw_line = line.strip()
        raw_cells = raw_line[1:-1] if raw_line.endswith("|") else raw_line[1:]
        cells = [cell.replace("<br>", "\n") for cell in split_escaped_values(raw_cells)]
        if len(cells) == columns:
            rows.append(cells)
        elif len(cells) > columns:
            # Surplus cells (usually an unescaped '|' pasted into a cell) fold
            # back into the last column so the data survives the next render.
            rows.append(cells[: columns - 1] + ["|".join(cells[columns - 1:])])
        else:
            # Short rows are padded; render_table writes the padded cells back.
            rows.append(cells + [""] * (columns - len(cells)))
    return rows


def parse_risks(lines: list[str]) -> list[str]:
    start = find_heading(lines, HEADING_ALIASES["risks"])
    if start is None:
        return []
    risks: list[str] = []
    for line in lines[start + 2 :]:
        if line.startswith("## "):
            break
        if line.startswith("- "):
            value = line[2:].strip()
            if value not in {TEXT["en"]["none"], TEXT["zh"]["none"]}:
                risks.append(value)
    return risks


def parse_final(lines: list[str]) -> str:
    start = find_heading(lines, HEADING_ALIASES["final"])
    if start is None:
        return ""
    collected: list[str] = []
    for line in lines[start + 2 :]:
        if (
            line.startswith(END)
            or line.startswith(LEGACY_CODE_CCTV_END)
            or line.startswith(LEGACY_END)
            or line.startswith(OLDER_LEGACY_END)
        ):
            break
        collected.append(line)
    final = "\n".join(collected).strip()
    return final


def find_line(lines: list[str], value: str) -> int | None:
    for index, line in enumerate(lines):
        if line.strip() == value:
            return index
    return None


def find_heading(lines: list[str], headings: list[str]) -> int | None:
    for heading in headings:
        found = find_line(lines, f"## {heading}")
        if found is not None:
            return found
    return None


def first_prefixed_any(lines: list[str], prefixes: list[str]) -> str | None:
    for prefix in prefixes:
        found = first_prefixed(lines, prefix)
        if found is not None:
            return found
    return None


def load_worklog(path: Path, language: str) -> Worklog:
    labels = TEXT[language]
    if not path.exists():
        return Worklog(
            status=labels["default_status"],
            focus=labels["default_focus"],
            final_summary=labels["pending"],
        )
    text = path.read_text(encoding="utf-8")
    section = monitored_section(text)
    lines = section.splitlines()
    status = first_prefixed_any(lines, ["Status: ", "状态：", "状态: "]) or labels["default_status"]
    focus = first_prefixed_any(
        lines,
        ["Current focus: ", "当前关注：", "当前关注: "],
    ) or labels["default_focus"]
    final_summary = parse_final(lines) or labels["pending"]
    return Worklog(
        status=status,
        focus=focus,
        priority=parse_table(lines, HEADING_ALIASES["priority"], 3),
        modules=parse_table(lines, HEADING_ALIASES["modules"], 6),
        live_notes=parse_table(lines, HEADING_ALIASES["live_notes"], 4),
        files=parse_table(lines, HEADING_ALIASES["files"], 3),
        functions=parse_table(lines, HEADING_ALIASES["functions"], 4),
        segments=parse_table(lines, HEADING_ALIASES["segments"], 4),
        decisions=parse_table(lines, HEADING_ALIASES["decisions"], 3),
        validation=parse_table(lines, HEADING_ALIASES["validation"], 3),
        beginner_checks=parse_table(lines, HEADING_ALIASES["beginner_checks"], 3),
        risks=parse_risks(lines),
        final_summary=final_summary,
    )


def translate_cell(value: str, language: str) -> str:
    if language != "zh":
        return value
    translated = ZH_CELL_TRANSLATIONS.get(value, value)
    for source, target in ZH_SUBSTRING_TRANSLATIONS.items():
        translated = translated.replace(source, target)
    return translated


def translate_rows(rows: list[list[str]], language: str) -> list[list[str]]:
    return [[translate_cell(cell, language) for cell in row] for row in rows]


def normalize_worklog_language(worklog: Worklog, language: str) -> None:
    if language != "zh":
        return
    worklog.status = translate_cell(worklog.status, language)
    worklog.focus = translate_cell(worklog.focus, language)
    worklog.priority = translate_rows(worklog.priority, language)
    worklog.modules = translate_rows(worklog.modules, language)
    worklog.live_notes = translate_rows(worklog.live_notes, language)
    worklog.files = translate_rows(worklog.files, language)
    worklog.functions = translate_rows(worklog.functions, language)
    worklog.segments = translate_rows(worklog.segments, language)
    worklog.decisions = translate_rows(worklog.decisions, language)
    worklog.validation = translate_rows(worklog.validation, language)
    worklog.beginner_checks = translate_rows(worklog.beginner_checks, language)
    worklog.risks = [translate_cell(risk, language) for risk in worklog.risks]
    worklog.final_summary = translate_cell(worklog.final_summary, language)


def monitored_section(text: str) -> str:
    markers = section_markers(text)
    if markers is None:
        return text
    start_marker, end_marker = markers
    start = text.index(start_marker) + len(start_marker)
    end = text.index(end_marker, start)
    return text[start:end].strip()


def section_markers(text: str) -> tuple[str, str] | None:
    if START in text and END in text:
        return START, END
    if LEGACY_CODE_CCTV_START in text and LEGACY_CODE_CCTV_END in text:
        return LEGACY_CODE_CCTV_START, LEGACY_CODE_CCTV_END
    if LEGACY_START in text and LEGACY_END in text:
        return LEGACY_START, LEGACY_END
    if OLDER_LEGACY_START in text and OLDER_LEGACY_END in text:
        return OLDER_LEGACY_START, OLDER_LEGACY_END
    return None


def first_prefixed(lines: list[str], prefix: str) -> str | None:
    for line in lines:
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return None


def merge_unique(rows: list[list[str]], additions: list[list[str]]) -> list[list[str]]:
    seen = {tuple(row) for row in rows}
    for row in additions:
        key = tuple(row)
        if key not in seen:
            rows.append(row)
            seen.add(key)
    return rows


def render_table(headers: list[str], rows: list[list[str]]) -> str:
    output = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        output.append("| " + " | ".join(escape_cell(cell) for cell in row) + " |")
    return "\n".join(output)


def mermaid_label(value: str) -> str:
    # Escape backslash/quote so the value stays inside the quoted label, and
    # collapse newlines so it can never escape the ```mermaid code fence.
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def compact_label(value: str, fallback: str, limit: int = 72) -> str:
    cleaned = " ".join(value.split()).strip()
    if not cleaned:
        return fallback
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: limit - 3]}..."


def is_emptyish(value: str) -> bool:
    return value.strip().lower() in {"", "-", "none", "n/a", "no", "暂无", "无", "待补充"}


def split_dependencies(value: str) -> list[str]:
    if is_emptyish(value):
        return []
    return [part.strip() for part in re.split(r"[,，、;/；\n]+", value) if part.strip()]


def render_module_graph(modules: list[list[str]], language: str) -> str:
    labels = TEXT[language]
    if not modules:
        empty = mermaid_label(labels["none"])
        return f"""```mermaid
flowchart TD
    M0["{empty}"]
```
"""

    lines = ["```mermaid", "flowchart TD"]
    normalized_names: dict[str, str] = {}
    for index, row in enumerate(modules):
        module, *_ = row + [""] * (6 - len(row))
        normalized_names[module.casefold()] = f"M{index}"

    for index, row in enumerate(modules):
        module, related_code, responsibility, dependencies, risk, verify = (row + [""] * 6)[:6]
        module_id = f"M{index}"
        module_label = mermaid_label(f"模块：{compact_label(module, labels['none'], 48)}")
        code_label = mermaid_label(f"代码：{compact_label(related_code, labels['none'], 68)}")
        responsibility_label = mermaid_label(f"职责：{compact_label(responsibility, labels['none'], 68)}")
        verify_label = mermaid_label(f"核对：{compact_label(verify, labels['none'], 68)}")

        lines.append(f'    {module_id}["{module_label}"]')
        lines.append(f'    {module_id} --> {module_id}C["{code_label}"]')
        lines.append(f'    {module_id} --> {module_id}R["{responsibility_label}"]')
        lines.append(f'    {module_id} --> {module_id}V["{verify_label}"]')

        if not is_emptyish(risk):
            risk_label = mermaid_label(f"风险：{compact_label(risk, labels['none'], 62)}")
            lines.append(f'    {module_id} -.-> {module_id}K["{risk_label}"]')

        for dep_index, dependency in enumerate(split_dependencies(dependencies)):
            target_id = normalized_names.get(dependency.casefold())
            if target_id and target_id != module_id:
                lines.append(f"    {module_id} -.-> {target_id}")
            elif not target_id:
                dep_label = mermaid_label(f"依赖：{compact_label(dependency, labels['none'], 54)}")
                lines.append(f'    {module_id} -.-> {module_id}D{dep_index}["{dep_label}"]')

    lines.append("```")
    return "\n".join(lines) + "\n"


def render_flow(status: str, language: str) -> str:
    status_label = mermaid_label(single_line(status))
    node_goal, node_read, node_plan, node_edit, node_validate, node_summarize, node_status = [
        mermaid_label(value) for value in TEXT[language]["flow_nodes"]
    ]
    return f"""```mermaid
flowchart TD
    A["{node_goal}"] --> B["{node_read}"]
    B --> C["{node_plan}"]
    C --> D["{node_edit}"]
    D --> E["{node_validate}"]
    E --> F["{node_summarize}"]
    B -.-> S["{node_status}：{status_label}"]
```
"""


def render_worklog(worklog: Worklog, timestamp: str, language: str) -> str:
    labels = TEXT[language]
    risks = "\n".join(f"- {single_line(risk)}" for risk in worklog.risks) if worklog.risks else f"- {labels['none']}"
    return f"""{START}
# Code Defog

{labels["last_updated"]}：{timestamp}
{labels["status"]}：{single_line(worklog.status)}
{labels["focus"]}：{single_line(worklog.focus)}

## {labels["pyramid"]}

{render_table(labels["priority_headers"], worklog.priority)}

## {labels["module_map"]}

{render_table(labels["module_headers"], worklog.modules)}

{render_module_graph(worklog.modules, language)}
## {labels["flow"]}

{render_flow(worklog.status, language)}
## {labels["live_notes"]}

{render_table(labels["live_headers"], worklog.live_notes)}

## {labels["files"]}

{render_table(labels["file_headers"], worklog.files)}

## {labels["functions"]}

{render_table(labels["function_headers"], worklog.functions)}

## {labels["segments"]}

{render_table(labels["segment_headers"], worklog.segments)}

## {labels["decisions"]}

{render_table(labels["decision_headers"], worklog.decisions)}

## {labels["validation"]}

{render_table(labels["validation_headers"], worklog.validation)}

## {labels["beginner_checks"]}

{render_table(labels["beginner_check_headers"], worklog.beginner_checks)}

## {labels["risks"]}

{risks}

## {labels["final"]}

{worklog.final_summary.strip()}
{END}
"""


def replace_section(existing: str, rendered: str) -> str:
    markers = section_markers(existing)
    if markers is None:
        prefix = existing.rstrip()
        return f"{prefix}\n\n{rendered}" if prefix else rendered
    start_marker, end_marker = markers
    start = existing.index(start_marker)
    end = existing.index(end_marker, start) + len(end_marker)
    return existing[:start] + rendered.rstrip() + existing[end:]


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode = path.stat().st_mode & 0o777 if path.exists() else None
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path = Path(temporary_name)
        if existing_mode is not None:
            os.chmod(temporary_path, existing_mode)
        os.replace(temporary_path, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def main() -> None:
    args = parse_args()
    workspace = Path(args.workspace).expanduser().resolve()
    path = workspace / args.file
    worklog = load_worklog(path, args.language)
    timestamp = now_text(args.timezone)

    if args.status:
        worklog.status = args.status
    if args.focus:
        worklog.focus = args.focus
    if args.phase or args.note:
        worklog.live_notes.append(
            [
                timestamp,
                args.phase or "Update",
                args.note or ("已更新工作日志。" if args.language == "zh" else "Updated worklog."),
                args.evidence,
            ]
        )
    merge_unique(worklog.priority, [split_row(row, 3) for row in args.priority])
    merge_unique(worklog.modules, [split_row(row, 6) for row in args.module])
    merge_unique(worklog.files, [split_row(row, 3) for row in args.touch])
    merge_unique(worklog.functions, [split_row(row, 4) for row in args.function])
    merge_unique(worklog.segments, [split_row(row, 4) for row in args.segment])
    merge_unique(worklog.decisions, [split_row(row, 3) for row in args.decision])
    merge_unique(worklog.validation, [split_row(row, 3) for row in args.check])
    merge_unique(worklog.beginner_checks, [split_row(row, 3) for row in args.beginner_check])
    for risk in args.risk:
        if risk not in worklog.risks:
            worklog.risks.append(risk)
    if args.final is not None:
        worklog.final_summary = args.final

    normalize_worklog_language(worklog, args.language)
    rendered = render_worklog(worklog, timestamp, args.language)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    atomic_write_text(path, replace_section(existing, rendered))
    post_event(
        {
            "workspace": str(workspace),
            "workspace_name": workspace.name,
            "conversation_id": conversation_id(),
            "conversation_name": conversation_name(),
            "event_type": "file-change" if args.touch else "progress",
            "source": "update_worklog.py",
            "phase": args.phase or "",
            "status": worklog.status,
            "focus": worklog.focus,
            "note": args.note or "工作日志已更新。",
            "evidence": args.evidence,
            "files": [split_row(row, 3)[0] for row in args.touch],
        }
    )
    print(path)


if __name__ == "__main__":
    main()
