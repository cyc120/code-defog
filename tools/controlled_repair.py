"""Narrow, auditable repair tooling for the Direction 3 demo target.

This is intentionally not a generic code-writing tool. It accepts one explicit
demo mode, copies the known demo target into a Store-controlled sandbox, and
applies only the reviewed Case A patch. The source checkout is never modified.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any


DEMO_REPAIR_MODE = "demo_sandbox"
POLICY_VERSION = "demo-repair-v1"
_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEMO_TARGET = (_REPO_ROOT / "demo_target").resolve()
_BUGGY_BLOCK = '''    return {
        "projects": config["projects"],
        "required_field": config["required_field"],
    }
'''
_PATCHED_BLOCK = '''    return {
        "projects": config.get("projects", []),
        "required_field": config.get("required_field"),
    }
'''


class ControlledRepairError(RuntimeError):
    """Raised when a repair request does not satisfy the demo policy."""


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _safe_component(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "-_" else "_" for char in value)
    return cleaned[:80] or "unknown-case"


def _record_tool_run(
    store: Any,
    *,
    case_id: str,
    tool_name: str,
    command_template: str,
    actual_argv: str,
    working_directory: Path,
    input_sha256: str,
    output_sha256: str,
    result_ref: str | None = None,
) -> str:
    return store.record_tool_run({
        "case_id": case_id,
        "agent_id": "repair",
        "tool_name": tool_name,
        "command_template": command_template,
        "actual_argv": actual_argv,
        "working_directory": str(working_directory),
        "policy_version": POLICY_VERSION,
        "input_sha256": input_sha256,
        "output_sha256": output_sha256,
        "exit_code": 0,
        "result_ref": result_ref or "",
    })


def apply_case_a_patch(context: dict[str, Any], store: Any) -> dict[str, Any]:
    """Create an isolated Case A repair sandbox and record its evidence.

    The method rejects every source location except this repository's known
    ``demo_target`` and does not accept caller-provided patch content.
    """
    if context.get("repair_mode") != DEMO_REPAIR_MODE:
        raise ControlledRepairError("controlled demo repair requires repair_mode=demo_sandbox")

    case_id = str(context.get("case_id", "")).strip()
    if not case_id:
        raise ControlledRepairError("case_id is required")

    requested_source = Path(str(context.get("repository_ref", ""))).expanduser()
    try:
        source_dir = requested_source.resolve(strict=True)
    except OSError as exc:
        raise ControlledRepairError(f"demo source path is unavailable: {exc}") from exc
    if source_dir != _DEMO_TARGET:
        raise ControlledRepairError("controlled demo repair only permits the bundled demo_target")

    source_cli = source_dir / "cli.py"
    source_bytes = source_cli.read_bytes()
    # Normalize line endings only for reviewed textual matching and diffing.
    # The source digest remains over the original bytes copied into sandbox.
    source_text = source_bytes.decode("utf-8").replace("\r\n", "\n")
    if _BUGGY_BLOCK not in source_text:
        raise ControlledRepairError("demo source does not match the reviewed Case A patch precondition")

    patched_text = source_text.replace(_BUGGY_BLOCK, _PATCHED_BLOCK, 1)
    patched_bytes = patched_text.encode("utf-8")
    source_sha256 = _sha256_bytes(source_bytes)
    patched_sha256 = _sha256_bytes(patched_bytes)
    diff = "".join(difflib.unified_diff(
        source_text.splitlines(keepends=True),
        patched_text.splitlines(keepends=True),
        fromfile="a/cli.py",
        tofile="b/cli.py",
    ))
    patch_ref = f"patch-{_sha256_bytes((diff + patched_sha256).encode('utf-8'))[:24]}"

    sandbox_root = (Path(store.path).parent / "sandboxes").resolve()
    sandbox_dir = (sandbox_root / _safe_component(case_id) / patch_ref).resolve()
    if sandbox_root not in sandbox_dir.parents:
        raise ControlledRepairError("calculated sandbox path escapes the Store root")
    if sandbox_dir.exists():
        raise ControlledRepairError("a sandbox already exists for this patch reference")

    sandbox_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_dir, sandbox_dir, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    copied_cli = sandbox_dir / "cli.py"
    copied_sha256 = _sha256_bytes(copied_cli.read_bytes())
    copy_run_id = _record_tool_run(
        store,
        case_id=case_id,
        tool_name="sandbox_copy",
        command_template="copy demo_target to isolated sandbox",
        actual_argv=f"copytree {source_dir} {sandbox_dir}",
        working_directory=source_dir.parent,
        input_sha256=source_sha256,
        output_sha256=copied_sha256,
    )

    temporary_path = copied_cli.with_name(f".{copied_cli.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temporary_path, "x", encoding="utf-8", newline="\n") as handle:
            handle.write(patched_text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, copied_cli)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise

    if _sha256_bytes(copied_cli.read_bytes()) != patched_sha256:
        raise ControlledRepairError("sandbox patch hash verification failed")

    metadata = {
        "policy_version": POLICY_VERSION,
        "patch_ref": patch_ref,
        "source_repository_ref": str(source_dir),
        "sandbox_repository_ref": str(sandbox_dir),
        "files_changed": ["cli.py"],
        "source_cli_sha256": source_sha256,
        "patched_cli_sha256": patched_sha256,
        "diff": diff,
    }
    artifact_id = store.record_artifact(
        case_id,
        "patch_metadata",
        f"{patch_ref}.json",
        json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8"),
    )
    patch_run_id = _record_tool_run(
        store,
        case_id=case_id,
        tool_name="apply_case_a_patch",
        command_template="apply reviewed Case A patch to sandbox cli.py",
        actual_argv=f"apply_case_a_patch {copied_cli}",
        working_directory=sandbox_dir,
        input_sha256=copied_sha256,
        output_sha256=patched_sha256,
        result_ref=artifact_id,
    )
    return {
        "patch_ref": patch_ref,
        "sandbox_repository_ref": str(sandbox_dir),
        "files_changed": ["cli.py"],
        "patch_artifact_ref": artifact_id,
        "tool_run_ids": [copy_run_id, patch_run_id],
        "source_cli_sha256": source_sha256,
        "patched_cli_sha256": patched_sha256,
    }
