"""Read-only Code Interpreter Agent entry point.

This agent accepts only a pre-built Node/Selection Dossier.  It has no file
system or subprocess capability of its own, so callers cannot turn a graph
selection into arbitrary project access or an execution request.
"""

from __future__ import annotations

from typing import Any

from daemon.code_semantics import interpret_code_dossier


def run(context: dict[str, Any]) -> dict[str, Any]:
    dossier = context.get("dossier")
    if not isinstance(dossier, dict):
        return {"status": "error", "reason": "代码解读 Agent 缺少节点证据包。"}
    return interpret_code_dossier(
        dossier,
        include_source=context.get("include_source") is True,
        provider_store=context.get("provider_store"),
    )
