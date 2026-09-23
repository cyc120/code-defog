#!/usr/bin/env python3
"""Local CLI coding-agent execution adapter (Codex CLI / Claude Code).

This is a third, explicitly *local* execution backend for the DevLoop
Harness, next to the in-process mock and the AgentScope experiment path.
It shells out to a real CLI coding agent (``codex exec`` / ``claude -p``,
or a custom command template) inside a Store-controlled sandbox copy of the
target repository.

Security model (identical in shape to the demo repair path):

- The original repository is never modified.  Every dispatch runs against a
  fresh copy under ``<store>/sandboxes/<case>/cli-<id>``.
- Model output is evidence, never authority.  A repair only becomes
  releasable when the Orchestrator sees ``repair_mode=cli_sandbox`` AND a
  sandbox path that passes ``_sandbox_ref_allowed`` AND the deterministic
  quality gate passes AND a human approves the release.  The adapter cannot
  skip any of those.
- Every CLI invocation is persisted as an immutable ``tool_runs`` record
  (hash-chained) plus a bounded transcript artifact.

Honest boundary: the CLI subprocess runs with the invoking user's full
local privileges.  The Store sandbox protects the *original repository*,
not the machine.  Provider permission flags (``--sandbox workspace-write``
/ ``--permission-mode acceptEdits``) are the first line of defence, not a
security boundary.  This runtime is a local experiment path; it is not, and
must not be described as, an AgentTeams deployment.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .harness import TASK_BY_STATE
from .teams_adapter import (
    build_task_prompt,
    extract_json_block,
    harness_metadata,
    promoted_fields,
    validate_structured,
)

# Signal value that marks a repair as materialised by this adapter inside a
# Store-controlled sandbox.  The Orchestrator trusts it exactly as much as
# it trusts the demo path's "demo_sandbox" — see TRUSTED_SANDBOX_MODES.
CLI_SANDBOX_MODE = "cli_sandbox"

POLICY_VERSION = "cli-exec-v1"

_COPY_IGNORE = (
    "__pycache__", "*.pyc", ".git", "node_modules", ".venv", "venv",
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
)
_MAX_SNAPSHOT_FILES = 5000
_MAX_SNAPSHOT_FILE_BYTES = 1_000_000


@dataclass(frozen=True)
class CLIProvider:
    """Command shape and default permission flags for one CLI agent."""

    name: str
    argv: tuple[str, ...]
    safe_args: tuple[str, ...] = ()
    model_flag: str = "--model"


PROVIDERS: dict[str, CLIProvider] = {
    # codex exec <prompt> — workspace-write keeps edits inside cwd (the
    # sandbox); --skip-git-repo-check avoids refusing the sandbox copy.
    "codex": CLIProvider(
        name="codex",
        argv=("codex", "exec"),
        safe_args=("--sandbox", "workspace-write", "--skip-git-repo-check"),
    ),
    # claude -p <prompt> — acceptEdits allows file edits inside cwd without
    # prompting for each one; dangerous operations still require approval.
    "claude": CLIProvider(
        name="claude",
        argv=("claude", "-p"),
        safe_args=("--permission-mode", "acceptEdits"),
    ),
}


@dataclass(frozen=True)
class CLITeamsConfig:
    """Runtime configuration for the local CLI agent team."""

    provider: str = "codex"            # codex | claude | custom
    command: str = ""                  # custom command template, e.g. "my-agent run"
    model: str = ""                    # passed via the provider's model flag when set
    timeout_s: float = 300.0           # hard per-dispatch timeout
    extra_args: tuple[str, ...] = ()   # appended verbatim before the prompt
    max_output_bytes: int = 200_000    # bound for transcript/stdout retention


def _safe_component(value: str) -> str:
    """Same sanitisation as tools/controlled_repair._safe_component."""
    cleaned = "".join(char if char.isalnum() or char in "-_" else "_" for char in value)
    return cleaned[:80] or "unknown-case"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_lines(path: Path) -> list[str]:
    """Best-effort text read; missing or binary files yield no lines."""
    try:
        if not path.is_file() or path.stat().st_size > _MAX_SNAPSHOT_FILE_BYTES:
            return []
        return path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except OSError:
        return []


def _snapshot(root: Path) -> dict[str, str]:
    """Map of relative path -> sha256 for bounded text-ish files under root."""
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if len(files) >= _MAX_SNAPSHOT_FILES:
            break
        try:
            if path.is_symlink() or not path.is_file():
                continue
            if path.stat().st_size > _MAX_SNAPSHOT_FILE_BYTES:
                continue
            relative = path.relative_to(root).as_posix()
            if relative.startswith(".git/") or "__pycache__" in relative:
                continue
            files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return files


def _kill_process_group(exc: subprocess.TimeoutExpired) -> None:
    """Kill the whole process group on timeout (mirrors daemon/drive.py)."""
    try:
        os.killpg(os.getpgid(exc.process.pid), signal.SIGKILL)
    except (OSError, AttributeError, ProcessLookupError):
        pass


class CLITeamsAdapter:
    """Dispatch DevLoop Case tasks to a local CLI coding agent.

    Implements the ``AgentExecutionAdapter`` protocol from
    ``agent_runtime/harness.py``.  Project-review and code-interpreter tasks
    are deliberately NOT served in this runtime: they fail structurally
    instead of silently falling back to another backend.
    """

    def __init__(self, store: Any, config: CLITeamsConfig | None = None) -> None:
        self._store = store
        self._config = config or CLITeamsConfig()

    @property
    def mode(self) -> str:
        return "cli"

    @property
    def config(self) -> CLITeamsConfig:
        return self._config

    # ── AgentExecutionAdapter ─────────────────────────────────────────────

    def dispatch_task(
        self, case_id: str, state: str, context: dict[str, Any],
    ) -> dict[str, Any]:
        task = TASK_BY_STATE.get(state)
        if task is None:
            return {"error": f"no agent mapped for state: {state}"}
        agent_key = task.agent_id
        trace_id = str(context.get("trace_id") or f"clitrace-{uuid.uuid4().hex[:16]}")
        run_id = self._store.begin_agent_run(case_id, agent_key, trace_id)
        try:
            result = self._run_state(case_id, state, agent_key, context)
        except Exception as exc:  # never let a dispatch kill the caller
            result = {"status": "failed", "failure_reason": f"exception: {exc}"}
        if result.get("status") not in ("completed", "failed"):
            result = {
                "status": "failed",
                "failure_reason": "cli adapter produced an invalid status",
            }
        result = {**result, **harness_metadata(context)}
        self._store.finish_agent_run(run_id, result["status"], json.dumps(result, ensure_ascii=False))
        return result

    def dispatch_review_task(
        self, review_run_id: str, task_key: str, context: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "status": "failed",
            "failure_reason": (
                "cli runtime does not serve project-review tasks in v1; "
                "run Review Runs under mock or agentscope"
            ),
            **harness_metadata(context),
        }

    def dispatch_code_interpreter_task(self, context: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "failed",
            "failure_reason": (
                "cli runtime does not serve code-interpreter tasks in v1; "
                "use mock or agentscope for code-map interpretation"
            ),
            **harness_metadata(context),
        }

    # ── state runners ─────────────────────────────────────────────────────

    def _run_state(
        self, case_id: str, state: str, agent_key: str, context: dict[str, Any],
    ) -> dict[str, Any]:
        if state == "VERIFYING":
            # Verification stays deterministic: the quality gate is a real
            # subprocess against the sandbox, never a model opinion.
            from agents.verification import run as verification_run

            verified = verification_run({**context, "_state_store": self._store})
            if not isinstance(verified, dict):
                raise RuntimeError("verification agent returned a non-object result")
            verified.setdefault("status", "completed")
            return verified

        prompt = build_task_prompt(state, context)
        sandbox = self._ensure_sandbox(case_id, context)
        source = self._sandbox_source(context)
        before = _snapshot(sandbox)
        argv = self._build_argv(prompt)

        try:
            completed = subprocess.run(
                argv,
                cwd=str(sandbox),
                capture_output=True,
                text=True,
                timeout=self._config.timeout_s,
                check=False,
                start_new_session=True,
            )
        except subprocess.TimeoutExpired as exc:
            _kill_process_group(exc)
            self._record_cli_evidence(
                case_id, agent_key, argv, sandbox, prompt,
                (exc.stdout or "") if isinstance(exc.stdout, str) else "",
                (exc.stderr or "") if isinstance(exc.stderr, str) else "",
                None,
            )
            return {
                "status": "failed",
                "failure_reason": f"cli_timeout: exceeded {self._config.timeout_s:g}s",
                "sandbox_repository_ref": str(sandbox),
            }
        except OSError as exc:
            return {
                "status": "failed",
                "failure_reason": f"cli_launch_failed: {exc}",
            }

        stdout = (completed.stdout or "")[: self._config.max_output_bytes]
        stderr = (completed.stderr or "")[: self._config.max_output_bytes]
        self._record_cli_evidence(
            case_id, agent_key, argv, sandbox, prompt, stdout, stderr, completed.returncode,
        )

        if completed.returncode != 0:
            return {
                "status": "failed",
                "failure_reason": f"cli_exit_{completed.returncode}",
                "raw_text": stdout[-4000:],
                "sandbox_repository_ref": str(sandbox),
            }

        structured = extract_json_block(stdout)
        error = validate_structured(agent_key, structured)
        if error:
            return {
                "status": "failed",
                "failure_reason": f"invalid_structured_output: {error}",
                "raw_text": stdout[-4000:],
                "sandbox_repository_ref": str(sandbox),
            }

        result: dict[str, Any] = {
            "agent": agent_key,
            "case_id": case_id,
            "action": structured.get("action", ""),
            "status": "completed",
            "structured_output": structured,
            "raw_text": stdout[-4000:],
            "result_summary": str(structured.get("action", ""))[:500],
            "execution_mode": "cli",
            "cli_provider": self._config.provider,
            "sandbox_repository_ref": str(sandbox),
        }
        for name in promoted_fields.get(agent_key, ()):
            if name in structured:
                result[name] = structured[name]

        if state == "REPAIRING":
            result.update(self._repair_outcome(case_id, sandbox, source, before))
        return result

    # ── repair outcome ────────────────────────────────────────────────────

    def _repair_outcome(
        self, case_id: str, sandbox: Path, source: Path | None, before: dict[str, str],
    ) -> dict[str, Any]:
        """Materialise the sandbox diff as evidence and derive patch_ref.

        Returns the repair fields the Orchestrator persists when — and only
        when — the case carries ``repair_mode=cli_sandbox``.  Without a real
        diff, patch_ref stays empty so the Orchestrator escalates instead of
        anchoring a release on an empty repair.
        """
        after = _snapshot(sandbox)
        changed = [
            relative
            for relative in sorted(set(before) | set(after))
            if before.get(relative) != after.get(relative)
        ]
        parts: list[str] = []
        for relative in changed:
            old_lines = _read_lines(source / relative) if source else []
            new_lines = _read_lines(sandbox / relative)
            parts.append("".join(difflib.unified_diff(
                old_lines, new_lines, fromfile=f"a/{relative}", tofile=f"b/{relative}",
            )))
        diff = "".join(parts)
        if not diff:
            return {
                "patch_ref": "",
                "files_changed": [],
                "branch": f"cli/{case_id}",
                "note": "Local CLI repair produced no changes in the sandbox.",
            }
        patch_ref = f"patch-{_sha256_text(diff + '|' + '|'.join(changed))[:24]}"
        artifact_id = ""
        try:
            artifact_id = self._store.record_artifact(
                case_id, "cli_patch", f"{patch_ref}.diff", diff.encode("utf-8"),
            )
        except OSError:
            artifact_id = ""
        return {
            "patch_ref": patch_ref,
            "files_changed": changed,
            "branch": f"cli/{case_id}",
            "patch_artifact_ref": artifact_id,
            "note": "Local CLI repair materialised in a Store-controlled sandbox.",
        }

    # ── sandbox handling ──────────────────────────────────────────────────

    def _sandbox_root(self) -> Path:
        return (Path(self._store.path).parent / "sandboxes").resolve()

    def _sandbox_source(self, context: dict[str, Any]) -> Path | None:
        raw = str(context.get("repository_ref") or "").strip()
        if not raw or raw == "unknown":
            return None
        candidate = Path(raw).expanduser()
        return candidate if candidate.is_dir() else None

    def _ensure_sandbox(self, case_id: str, context: dict[str, Any]) -> Path:
        """Create a fresh sandbox copy of the target repository for this run."""
        sandbox_root = self._sandbox_root()
        run_dir = (sandbox_root / _safe_component(case_id) / f"cli-{uuid.uuid4().hex[:12]}").resolve()
        if sandbox_root not in run_dir.parents:
            raise RuntimeError("calculated sandbox path escapes the Store root")
        source = self._sandbox_source(context)
        if source is not None:
            shutil.copytree(source, run_dir, ignore=shutil.ignore_patterns(*_COPY_IGNORE))
        else:
            run_dir.mkdir(parents=True)
        return run_dir

    # ── evidence ──────────────────────────────────────────────────────────

    def _record_cli_evidence(
        self,
        case_id: str,
        agent_key: str,
        argv: list[str],
        sandbox: Path,
        prompt: str,
        stdout: str,
        stderr: str,
        exit_code: int | None,
    ) -> None:
        """Persist the CLI invocation as an immutable tool run + transcript."""
        # record_tool_run hashes the RAW payload but stores clean_text(value)
        # (whitespace-collapsed, length-capped).  A raw multi-line argv would
        # therefore desynchronise the stored row from its own chain hash, so
        # the rendered argv is normalised and bounded the same way here.  The
        # full verbatim argv and prompt live in the transcript artifact.
        rendered_argv = " ".join(shlex.quote(part) for part in argv)
        rendered_argv = " ".join(rendered_argv.split())
        if len(rendered_argv) > 1900:
            rendered_argv = rendered_argv[:1900] + " …[argv truncated; see transcript artifact]"
        transcript = (
            f"$ {rendered_argv}\n\n=== PROMPT ===\n{prompt}\n\n"
            f"=== STDOUT ===\n{stdout}\n\n=== STDERR ===\n{stderr}\n"
        ).encode("utf-8")[: self._config.max_output_bytes]
        artifact_id = ""
        try:
            artifact_id = self._store.record_artifact(
                case_id, "cli_transcript", "cli-transcript.txt", transcript,
            )
        except OSError:
            artifact_id = ""
        self._store.record_tool_run({
            "case_id": case_id,
            "agent_id": agent_key,
            "tool_name": f"cli_{self._config.provider}",
            "command_template": "cli agent exec <bounded prompt>",
            "actual_argv": rendered_argv,
            "working_directory": str(sandbox),
            "policy_version": POLICY_VERSION,
            "input_sha256": _sha256_text(prompt),
            "output_sha256": _sha256_text(stdout + stderr),
            "exit_code": exit_code if exit_code is not None else -1,
            "result_ref": artifact_id,
        })

    # ── argv construction ─────────────────────────────────────────────────

    def _build_argv(self, prompt: str) -> list[str]:
        config = self._config
        if config.provider == "custom":
            if not config.command.strip():
                raise RuntimeError("custom CLI provider requires a command template")
            argv = shlex.split(config.command)
        else:
            provider = PROVIDERS.get(config.provider)
            if provider is None:
                raise RuntimeError(f"unknown CLI provider: {config.provider}")
            argv = [*provider.argv, *provider.safe_args]
            if config.model:
                argv += [provider.model_flag, config.model]
        argv += list(config.extra_args)
        argv.append(prompt)
        return argv
