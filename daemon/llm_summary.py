"""LLM-powered project summary for the Code CCTV overview dashboard.

Generates a codecctv-style Chinese summary (信息金字塔: conclusion/risk/next
action first) from the deterministic aggregates returned by
``StateStore.project_summary()`` plus a best-effort slice of ``AI_WORKLOG.md``.

Design constraints (project iron rule: "模型叙述不是执行证据"):
- The deterministic ``stats`` the frontend charts come from SQLite, never from
  the model.
- This module is strictly fail-closed: without ``DEEPSEEK_API_KEY`` it returns
  ``{"status": "unavailable", ...}`` before touching the network, and every
  transport/parse failure maps to ``{"status": "error", ...}``.  It never
  fabricates a plausible-looking summary.
- Transport is stdlib ``urllib`` against DeepSeek's OpenAI-compatible
  ``/chat/completions``; no third-party HTTP dependency is added.
- A TTL cache (``get_llm_summary``) prevents SSE-triggered stat refreshes from
  hammering the API; only an explicit ``refresh=True`` (or TTL expiry) re-runs
  the model.
"""

from __future__ import annotations

import json
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import certifi
except ImportError:  # pragma: no cover - certifi is a declared dependency
    certifi = None  # type: ignore[assignment]

SUMMARY_TTL_S = 60
SUMMARY_ERROR_TTL_S = 15
WORKLOG_FILENAME = "AI_WORKLOG.md"
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"

SYSTEM_PROMPT = (
    "你是\"Code CCTV DevLoop\"受控软件研发项目的进度总结助手。"
    "你只依据给定的【确定性统计】与【工作日志片段】生成中文总结，"
    "绝不虚构统计之外的执行证据，不得把 mock 演练描述成真实生产发布，"
    "不确定的信息必须标注\"待确认\"。"
    "输出必须是一个合法的 JSON 对象，不要输出 JSON 之外的任何文字。"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _resolve_api_key() -> str:
    """DEEPSEEK_API_KEY env var, else repo-root .env.  Real env wins."""
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if key:
        return key
    from agent_runtime.envfile import load_dotenv

    load_dotenv()
    return os.environ.get("DEEPSEEK_API_KEY", "")


def _read_worklog_context(limit: int = 4000) -> str:
    """Best-effort slice of AI_WORKLOG.md (信息金字塔/风险优先)."""
    try:
        root = Path(__file__).resolve().parents[1]
        text = (root / WORKLOG_FILENAME).read_text(encoding="utf-8")
    except OSError:
        return ""
    return text[:limit]


def _post_chat(api_key: str, prompt: str, timeout: float = 30.0,
               system_prompt: str | None = None) -> str:
    """POST to DeepSeek's OpenAI-compatible chat/completions.

    *system_prompt* overrides the default when provided (used by the drive
    prompt, which grounds the summary on a browsed project).

    Returns the assistant's content text.  Raises OSError/HTTPError/ValueError
    on transport failures so the caller can map to ``status='error'``.
    """
    body = json.dumps({
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt or SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }).encode("utf-8")
    request = urllib.request.Request(
        DEEPSEEK_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    # Prefer certifi's CA bundle so HTTPS verification succeeds even when the
    # local Python openssl default path is missing (common on some macOS Python
    # builds).  Falls back to the default context when certifi is absent.
    context = None
    if certifi is not None:
        context = ssl.create_default_context(cafile=certifi.where())
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload["choices"][0]["message"]["content"]


def _extract_json(text: str) -> dict[str, Any] | None:
    """Defensive parse: `` ```json ... ``` `` fence, then bare {...} fallback.

    Mirrors the tolerant JSON extraction used by the AgentScope adapter.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        # Last resort: slice the first {...} region (handles leading prose).
        start, end = candidate.find("{"), candidate.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            value = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None


def _single_line(value: Any, default: str = "") -> str:
    if not isinstance(value, str):
        return default
    return " ".join(value.split())


def _string_list(value: Any, limit: int, default: str = "待确认") -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value[:limit]:
        text = _single_line(item, "")
        if not text:
            continue
        result.append(text)
    return result


def _normalize_summary(raw: dict[str, Any]) -> dict[str, Any]:
    """Coerce missing/mistyped LLM fields to safe defaults; never crash on
    malformed input.  Truncates every string to a sane bound."""
    return {
        "overall_status": _single_line(raw.get("overall_status"), "未知") or "未知",
        "top_priorities": _string_list(raw.get("top_priorities"), 8),
        "progress_by_phase": _phase_list(raw.get("progress_by_phase")),
        "division_of_labor": _division_list(raw.get("division_of_labor")),
        "risks": _string_list(raw.get("risks"), 12),
        "next_steps": _string_list(raw.get("next_steps"), 8),
    }


def _phase_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    phases: list[dict[str, Any]] = []
    for item in value[:12]:
        if not isinstance(item, dict):
            continue
        phase = _single_line(item.get("phase"), "")
        if not phase:
            continue
        try:
            progress = int(item.get("progress", 0))
        except (TypeError, ValueError):
            progress = 0
        progress = max(0, min(100, progress))
        status = _single_line(item.get("status"), "待确认") or "待确认"
        phases.append({"phase": phase, "progress": progress, "status": status})
    return phases


def _division_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    divisions: list[dict[str, Any]] = []
    for item in value[:12]:
        if not isinstance(item, dict):
            continue
        agent = _single_line(item.get("agent"), "")
        if not agent:
            continue
        try:
            share = int(item.get("share", 0))
        except (TypeError, ValueError):
            share = 0
        activity = _single_line(item.get("activity"), "") or "待确认"
        divisions.append({
            "agent": agent,
            "activity": activity,
            "share": max(0, min(100, share)),
        })
    return divisions


def build_summary_prompt(stats: dict[str, Any], worklog_text: str) -> str:
    """Chinese, codecctv info-pyramid style prompt requesting a JSON summary."""
    totals = stats.get("totals", {})
    if (totals.get("cases") or 0) == 0:
        return (
            "项目当前没有任何 Case。请直接输出："
            '{"overall_status":"暂无 Case，尚无进度可总结","top_priorities":[],'
            '"progress_by_phase":[],"division_of_labor":[],"risks":[],"next_steps":[]}'
        )
    return (
        "请用\"信息金字塔\"风格（结论/风险/下一步 优先）总结项目进度与 Agent 分工。\n"
        "要求：\n"
        "- 全程中文，简明、可核对；\n"
        "- 先说总体结论与风险，再列阶段进度、分工、下一步；\n"
        "- 任何没有被统计支撑的表述都标注\"待确认\"或直接说明\"无证据\"；\n"
        "- 数字必须来自下面 stats，不得自行编造。\n\n"
        "【确定性统计】\n"
        f"{json.dumps(stats, ensure_ascii=False)[:8000]}\n\n"
        "【工作日志上下文（信息金字塔/实时记录/风险，仅供参考，可能陈旧）】\n"
        f"{worklog_text[:3000]}\n\n"
        "只输出如下 JSON：\n"
        '{\n'
        '  "overall_status": "一句话总体状态（含关键风险/阻塞）",\n'
        '  "top_priorities": ["P0 <结论/风险/下一步> ...", "P1 ..."],\n'
        '  "progress_by_phase": [{"phase": "阶段名", "progress": 0-100 整数, "status": "待建设|进行中|已验证"}],\n'
        '  "division_of_labor": [{"agent": "triage|diagnosis|repair|verification 等", '
        '"activity": "该 Agent 实际做了什么", "share": 0-100 整数（相对投入占比）}],\n'
        '  "risks": ["<风险描述>（待确认）..."],\n'
        '  "next_steps": ["<下一步建议>..."]\n'
        "}"
    )


def generate_project_summary(
    stats: dict[str, Any], worklog_text: str | None = None,
) -> dict[str, Any]:
    """Fail-closed LLM summary over the deterministic *stats*."""
    api_key = _resolve_api_key()
    if not api_key:
        return {
            "status": "unavailable",
            "reason": "DEEPSEEK_API_KEY 未配置；LLM 总结不可用（统计图表仍为确定性数据）。",
        }
    if worklog_text is None:
        worklog_text = _read_worklog_context()
    prompt = build_summary_prompt(stats, worklog_text)
    try:
        content = _post_chat(api_key, prompt)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError,
            socket.timeout, json.JSONDecodeError) as exc:
        return {"status": "error", "reason": f"LLM 调用失败：{exc}"}
    raw = _extract_json(content)
    if raw is None:
        return {"status": "error", "reason": "LLM 返回内容无法解析为 JSON。"}
    return {
        "status": "ok",
        "generated_at": utc_now(),
        "model": DEEPSEEK_MODEL,
        "summary": _normalize_summary(raw),
    }


def get_llm_summary(
    stats: dict[str, Any], refresh: bool = False, cache: dict[str, Any] | None = None,
    key: str = "default",
) -> dict[str, Any]:
    """TTL-cached wrapper so SSE-triggered stat refreshes never hammer the API.

    * ok          → cached 60s
    * error       → cached 15s (avoid repeat timeouts on a down API)
    * unavailable → never cached (instant, and shows why immediately)
    * refresh=True → bypasses cache.
    * key         → per-project cache slot, so switching projects never returns
      another project's LLM narrative.
    """
    now = time.monotonic()
    llm_key = f"llm:{key}"
    ts_key = f"ts:{key}"
    if cache is not None and not refresh:
        cached = cache.get(llm_key)
        if cached is not None:
            ttl = SUMMARY_TTL_S if cached.get("status") == "ok" else SUMMARY_ERROR_TTL_S
            if now - cache.get(ts_key, 0) < ttl:
                return cached
    result = generate_project_summary(stats)
    if cache is not None and result.get("status") in ("ok", "error"):
        cache.update({ts_key: now, llm_key: result})
    return result


# ── 自动化驱动 (project drive) summary ─────────────────────────────────

DRIVE_SYSTEM_PROMPT = (
    "你是\"Code CCTV DevLoop\"的自动化项目诊断助手。"
    "你依据【项目浏览报告】与【确定性统计】生成中文项目总结。"
    "即使项目没有任何 Case、没有任何错误，也必须基于浏览内容输出总体结论、"
    "观察到的风险（包括静态扫描的 TODO/错误处理缺口）、Agent 分工与下一步建议。"
    "不得虚构浏览报告之外的事实，不确定的信息标注\"待确认\"。"
    "输出必须是一个合法的 JSON 对象，不要输出 JSON 之外的任何文字。"
)


def build_drive_prompt(browse: dict[str, Any], stats: dict[str, Any]) -> str:
    """Chinese info-pyramid prompt grounded on the browsed project (no
    zero-case short-circuit — the drive summarizes the project itself)."""
    browse_json = json.dumps(browse, ensure_ascii=False)[:8000]
    stats_json = json.dumps(stats, ensure_ascii=False)[:3000]
    return (
        "请用\"信息金字塔\"风格（结论/风险/下一步 优先）总结当前项目的自动化诊断结果。\n"
        "要求：\n"
        "- 全程中文，简明、可核对；\n"
        "- 即使 0 个 Case、无测试失败，也必须给出基于项目浏览的总体结论；\n"
        "- 先说总体结论与风险，再列阶段进度、Agent 分工、下一步；\n"
        "- 数字必须来自下面 browse / stats，不得自行编造；\n"
        "- 静态扫描发现的 TODO/FIXME 或错误处理缺口必须体现在 risks 中。\n\n"
        "【项目浏览报告】\n"
        f"{browse_json}\n\n"
        "【确定性统计（DevLoop Case，可能为空）】\n"
        f"{stats_json}\n\n"
        "只输出如下 JSON：\n"
        '{\n'
        '  "overall_status": "一句话总体结论（含项目概况与关键风险）",\n'
        '  "top_priorities": ["P0 <结论/风险/下一步> ...", "P1 ..."],\n'
        '  "progress_by_phase": [{"phase": "阶段名", "progress": 0-100 整数, "status": "待建设|进行中|已验证"}],\n'
        '  "division_of_labor": [{"agent": "browse|diagnosis|repair|verification 等", '
        '"activity": "该角色实际做了什么", "share": 0-100 整数}],\n'
        '  "risks": ["<风险描述，含静态扫描发现>（待确认）..."],\n'
        '  "next_steps": ["<下一步建议>..."]\n'
        "}"
    )


def generate_drive_summary(
    workspace: str, browse: dict[str, Any], stats: dict[str, Any],
) -> dict[str, Any]:
    """Fail-closed LLM summary for a project drive (no zero-case short-circuit)."""
    api_key = _resolve_api_key()
    if not api_key:
        return {
            "status": "unavailable",
            "reason": "DEEPSEEK_API_KEY 未配置；LLM 总结不可用（项目浏览报告仍为确定性数据）。",
        }
    prompt = build_drive_prompt(browse, stats)
    try:
        content = _post_chat(api_key, prompt, system_prompt=DRIVE_SYSTEM_PROMPT)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError,
            socket.timeout, json.JSONDecodeError) as exc:
        return {"status": "error", "reason": f"LLM 调用失败：{exc}"}
    raw = _extract_json(content)
    if raw is None:
        return {"status": "error", "reason": "LLM 返回内容无法解析为 JSON。"}
    return {
        "status": "ok",
        "generated_at": utc_now(),
        "model": DEEPSEEK_MODEL,
        "summary": _normalize_summary(raw),
    }
