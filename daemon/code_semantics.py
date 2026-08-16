"""Evidence-grounded, read-only explanations for code graph nodes.

The model receives a bounded deterministic dossier, never repository
credentials, Git remotes, environment variables, arbitrary paths, or the
whole project.  The code-map UI sends structural metadata only; source context
is available solely to explicit API clients that request it.
"""

from __future__ import annotations

import json
import socket
import urllib.error
from datetime import datetime, timezone
from typing import Any

from .llm_providers import LLMProviderStore, provider_is_ready
from .llm_summary import _extract_json, _post_chat, _selected_provider, _unavailable_reason


SEMANTIC_SCHEMA_VERSION = 1
MAX_TEXT = 900
MAX_ITEMS = 5
_CERTAINTIES = {"confirmed", "inferred", "pending_confirmation"}

SYSTEM_PROMPT = (
    "你是 Code Defog 的只读代码解读 Agent。你只能解释给定的节点证据包，"
    "绝不能把文件名、相邻关系或代码注释当成已经执行过的运行时证据。"
    "没有明确证据时使用 pending_confirmation 或 inferred。"
    "不执行命令、不建议绕过安全控制、不输出任何 JSON 之外的文字。"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _bounded_text(value: Any, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _string_list(value: Any, limit: int = MAX_ITEMS) -> list[str]:
    if not isinstance(value, list):
        return []
    output: list[str] = []
    for item in value:
        text = _bounded_text(item, 360)
        if text and text not in output:
            output.append(text)
        if len(output) >= limit:
            break
    return output


def _safe_dossier_context(dossier: dict[str, Any], *, include_source: bool) -> dict[str, Any]:
    """Project a Node Dossier into the exact context allowed into an LLM prompt."""
    node = dossier.get("node") if isinstance(dossier.get("node"), dict) else {}
    selection = dossier.get("selection") if isinstance(dossier.get("selection"), dict) else {}
    facts = dossier.get("facts") if isinstance(dossier.get("facts"), dict) else {}
    neighbors = dossier.get("neighbors") if isinstance(dossier.get("neighbors"), list) else []
    refs = dossier.get("evidence_refs") if isinstance(dossier.get("evidence_refs"), list) else []
    context: dict[str, Any] = {
        "schema_version": SEMANTIC_SCHEMA_VERSION,
        "node": {
            key: node.get(key)
            for key in ("id", "type", "label", "path", "language", "symbol_kind", "line_start", "line_end", "parse_status")
        },
        "selection": {
            key: selection.get(key)
            for key in ("path", "start_line", "end_line")
        },
        "facts": {
            key: facts.get(key)
            for key in ("language", "symbol_kind", "parse_status", "symbol_count", "neighbor_count")
        },
        "neighbors": [
            {
                key: item.get(key)
                for key in ("node_id", "label", "type", "path", "relation", "evidence", "direction", "edge_id")
            }
            for item in neighbors[:16]
            if isinstance(item, dict)
        ],
        "evidence_refs": [
            {
                key: item.get(key)
                for key in ("ref_id", "kind", "node_id", "edge_id", "relation", "evidence", "path", "line_start", "line_end")
            }
            for item in refs[:14]
            if isinstance(item, dict)
        ],
        "source_included": bool(include_source),
    }
    if include_source:
        source = dossier.get("source_context") if isinstance(dossier.get("source_context"), dict) else {}
        context["source_context"] = {
            key: source.get(key)
            for key in ("path", "start_line", "end_line", "text", "truncated")
        }
    return context


def build_code_interpreter_prompt(dossier: dict[str, Any], *, include_source: bool = False) -> str:
    """Create a compact, evidence-bounded prompt for the map robot."""
    context = _safe_dossier_context(dossier, include_source=include_source)
    context["neighbors"] = context["neighbors"][:8]
    context["evidence_refs"] = context["evidence_refs"][:8]
    return (
        "只根据下面节点证据，用中文说明它在项目中的作用。不得猜测未提供的代码体、"
        "运行结果或业务语义。只输出一个 JSON 对象，且只包含 role、certainty、flow、"
        "evidence_refs 四个字段：role 为一句话；certainty 只能为 confirmed、inferred 或 "
        "pending_confirmation；flow 最多 1 项；evidence_refs 只能使用证据包中的 ref_id。\n"
        f"【节点证据包】\n{json.dumps(context, ensure_ascii=False)[:9000]}"
    )


def _normalize_reply(raw: dict[str, Any], dossier: dict[str, Any]) -> dict[str, Any] | None:
    valid_refs = {
        item.get("ref_id") for item in dossier.get("evidence_refs", [])
        if isinstance(item, dict) and isinstance(item.get("ref_id"), str)
    }
    valid_neighbors = {
        item.get("node_id") for item in dossier.get("neighbors", [])
        if isinstance(item, dict) and isinstance(item.get("node_id"), str)
    }
    role = _bounded_text(raw.get("role"), 360)
    if not role:
        return None
    certainty = raw.get("certainty") if isinstance(raw.get("certainty"), str) else "pending_confirmation"
    if certainty not in _CERTAINTIES:
        certainty = "pending_confirmation"
    refs = [ref for ref in _string_list(raw.get("evidence_refs"), 10) if ref in valid_refs]
    collaborators: list[dict[str, Any]] = []
    raw_collaborators = raw.get("collaborators") if isinstance(raw.get("collaborators"), list) else []
    for item in raw_collaborators[:MAX_ITEMS]:
        if not isinstance(item, dict):
            continue
        node_id = item.get("node_id")
        if not isinstance(node_id, str) or node_id not in valid_neighbors:
            continue
        relationship = _bounded_text(item.get("relationship"), 360)
        item_refs = [ref for ref in _string_list(item.get("evidence_refs"), 5) if ref in valid_refs]
        if relationship:
            collaborators.append({"node_id": node_id, "relationship": relationship, "evidence_refs": item_refs})
    return {
        "role": role,
        "certainty": certainty,
        "responsibilities": _string_list(raw.get("responsibilities")),
        "inputs_outputs": _string_list(raw.get("inputs_outputs")),
        "collaborators": collaborators,
        "flow": _string_list(raw.get("flow")),
        "risks": _string_list(raw.get("risks")),
        "evidence_refs": refs,
        "limitations": _string_list(raw.get("limitations")),
    }


def interpret_code_dossier(
    dossier: dict[str, Any],
    *,
    include_source: bool = False,
    provider_store: LLMProviderStore | None = None,
) -> dict[str, Any]:
    """Ask the selected model to interpret a prepared dossier, fail closed."""
    provider = _selected_provider(provider_store)
    if not provider_is_ready(provider):
        return {"status": "unavailable", "reason": _unavailable_reason(provider, "代码解读 Agent")}
    prompt = build_code_interpreter_prompt(dossier, include_source=include_source)
    try:
        content = _post_chat(
            str(provider["api_key"]), prompt, system_prompt=SYSTEM_PROMPT,
            provider=provider, timeout=25.0, max_tokens=1024,
        )
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, socket.timeout,
            json.JSONDecodeError, ValueError) as error:
        return {"status": "error", "reason": f"代码解读 Agent 调用失败：{error}"}
    raw = _extract_json(content)
    reply = _normalize_reply(raw, dossier) if isinstance(raw, dict) else None
    if reply is None:
        return {"status": "error", "reason": "代码解读 Agent 返回内容不符合受证据约束的 JSON 合约。"}
    return {
        "status": "ok",
        "generated_at": utc_now(),
        "provider": provider.get("id"),
        "model": provider.get("model"),
        "source_included": bool(include_source),
        "fingerprint": dossier.get("fingerprint"),
        "semantic": reply,
    }
