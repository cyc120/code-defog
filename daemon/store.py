#!/usr/bin/env python3
"""SQLite-backed state store for Code CCTV — extended with DevLoop Case management."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Guard: avoid circular import at module level; state_machine is imported
# inside transition_case() so tests can import store.py standalone.
_is_valid_transition = None


def _get_validator():
    global _is_valid_transition
    if _is_valid_transition is None:
        from agent_runtime.state_machine import is_valid_transition as ivt
        _is_valid_transition = ivt
    return _is_valid_transition


DEFAULT_RETENTION = 2000
DEFAULT_CONVERSATION_ID = "default"
PRUNE_EVERY_INGESTS = 50
DEFAULT_IDEMPOTENCY_WINDOW_S = 300
DEFAULT_INCIDENT_WINDOW_S = 86400
DEFAULT_ASSOCIATION_DEADLINE_S = 14400
DEFAULT_APPROVAL_EXPIRY_S = 1800
APPROVAL_ACTIONS = frozenset({"approve_plan", "approve_release"})
# Reject actions also consume an approval Grant (same auth model as approve)
REJECT_ACTIONS = frozenset({"reject_plan", "reject_release"})
ALL_GRANTED_ACTIONS = APPROVAL_ACTIONS | REJECT_ACTIONS


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def utc_now_unix() -> int:
    return int(time.time())


# States that auto-trigger the async retrospective after transition.
# state_machine.TERMINAL_STATES stays {CLOSED} for state-machine purposes;
# ROLLED_BACK is included here because its only valid next state is CLOSED,
# so it is effectively terminal for reporting purposes.
RETROSPECTIVE_TRIGGER_STATES = frozenset({"CLOSED", "ROLLED_BACK"})


def clean_text(value: Any, limit: int = 1200) -> str:
    if value is None:
        return ""
    text = " ".join(str(value).split())
    return text[:limit]


def clean_files(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [clean_text(item, 500) for item in value if clean_text(item, 500)]


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── DevLoop fingerprint helpers ────────────────────────────────────────────


def compute_delivery_id(source_type: str, source_uri: str, client_nonce: str) -> str:
    return sha256(f"{source_type}|{source_uri}|{client_nonce}")


def compute_incident_signature(
    repository_ref: str,
    exception_type: str | None,
    message_pattern: str | None,
    key_frames: list[str] | None,
) -> str:
    exc = (exception_type or "").strip()
    msg = (message_pattern or "").strip().lower().replace(" ", "")
    frames = "|".join(sorted(key_frames or []))
    return sha256(f"{repository_ref}|{exc}|{msg}|{frames}")


def compute_content_hash(raw_content: str) -> str:
    return sha256(raw_content)


def compute_approval_token_hash(token: str) -> str:
    return sha256(token)


# ── StateStore ─────────────────────────────────────────────────────────────


class StateStore:
    def __init__(self, path: Path, retention: int = DEFAULT_RETENTION) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.artifact_root = self.path.parent / "artifacts"
        self.retention = max(retention, 100)
        self._ingests_since_prune = 0
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        if sys.platform != "win32":
            self.path.chmod(0o600)
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                workspace TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                conversation_name TEXT NOT NULL,
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                phase TEXT NOT NULL,
                focus TEXT NOT NULL,
                note TEXT NOT NULL,
                evidence TEXT NOT NULL,
                event_type TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                event_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (workspace, conversation_id)
            );
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                workspace TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                source TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                phase TEXT NOT NULL,
                status TEXT NOT NULL,
                focus TEXT NOT NULL,
                note TEXT NOT NULL,
                evidence TEXT NOT NULL,
                files_json TEXT NOT NULL
            );
            """
        )
        self._ensure_devloop_tables()
        self.migrate_schema()
        self.connection.commit()

        # Callback for SSE publishing — set by the server after construction.
        self.publish_callback: Any = None
        # Callback fired (outside the lock, on a daemon thread) when a Case
        # reaches a retrospective-trigger state.  Wired in serve.py.
        self.retrospective_hook: Any = None
        # Per-case generation locks serialise the idempotency check + write in
        # generate_retrospective, preventing a ROLLED_BACK→CLOSED double-trigger
        # (or manual HTTP + async hook racing) from duplicating artifacts/records.
        self.retrospective_locks: dict[str, threading.Lock] = {}

    def _ensure_devloop_tables(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS cases (
                case_id TEXT PRIMARY KEY,
                incident_signature TEXT,
                status TEXT NOT NULL DEFAULT 'RECEIVED',
                priority TEXT NOT NULL DEFAULT 'medium',
                risk_level TEXT NOT NULL DEFAULT 'low',
                repository_ref TEXT NOT NULL DEFAULT '',
                repo_abs_path TEXT NOT NULL DEFAULT '',
                base_commit TEXT,
                patch_ref TEXT,
                sandbox_ref TEXT,
                pending_action TEXT,
                trace_id TEXT,
                title TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                closed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_cases_status ON cases(status);
            CREATE INDEX IF NOT EXISTS idx_cases_signature ON cases(incident_signature);

            -- User-selected projects the ProjectMonitor watches. Distinct from the
            -- ingest-session `projects` table (workspace+conversation_id summary).
            CREATE TABLE IF NOT EXISTS monitored_projects (
                workspace TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                git_remote TEXT NOT NULL DEFAULT '',
                branch TEXT NOT NULL DEFAULT '',
                base_commit TEXT,
                watcher_config TEXT NOT NULL DEFAULT '{}',
                selected_at TEXT NOT NULL,
                last_seen TEXT,
                last_scan_state TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                last_error TEXT,
                canonical_ref TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_monitored_projects_status
                ON monitored_projects(status);

            -- Automated drive runs (browse + test + static-scan + LLM summary).
            -- Workspace-scoped (unlike agent_runs which requires a case_id).
            CREATE TABLE IF NOT EXISTS drive_runs (
                run_id TEXT PRIMARY KEY,
                workspace TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'running',
                started_at TEXT NOT NULL,
                finished_at TEXT,
                duration_s REAL,
                browse_json TEXT,
                llm_json TEXT,
                error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_drive_runs_workspace
                ON drive_runs(workspace, started_at DESC);

            -- case_sources: case_id is NULL for pending (not-yet-associated) observations
            CREATE TABLE IF NOT EXISTS case_sources (
                observation_id TEXT PRIMARY KEY,
                case_id TEXT,
                source_type TEXT NOT NULL,
                source_uri TEXT NOT NULL,
                delivery_id TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                incident_signature TEXT,
                extracted_signals_json TEXT NOT NULL DEFAULT '{}',
                association_state TEXT NOT NULL DEFAULT 'pending',
                candidate_cases TEXT,
                association_confidence REAL,
                association_deadline TEXT,
                received_at TEXT NOT NULL,
                FOREIGN KEY (case_id) REFERENCES cases(case_id)
            );
            CREATE INDEX IF NOT EXISTS idx_case_sources_delivery
                ON case_sources(delivery_id, received_at);
            CREATE INDEX IF NOT EXISTS idx_case_sources_pending
                ON case_sources(association_state, association_deadline);
            CREATE INDEX IF NOT EXISTS idx_case_sources_signature
                ON case_sources(incident_signature, received_at);

            CREATE TABLE IF NOT EXISTS agent_runs (
                run_id TEXT PRIMARY KEY,
                case_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                input_ref TEXT,
                output_ref TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                trace_id TEXT,
                started_at TEXT,
                finished_at TEXT,
                FOREIGN KEY (case_id) REFERENCES cases(case_id)
            );
            CREATE INDEX IF NOT EXISTS idx_agent_runs_case ON agent_runs(case_id);

            -- tool_runs: immutable audit records, inserted only on completion
            CREATE TABLE IF NOT EXISTS tool_runs (
                run_id TEXT PRIMARY KEY,
                case_id TEXT NOT NULL,
                chain_sequence INTEGER NOT NULL DEFAULT 0,
                agent_id TEXT NOT NULL,
                approval_id TEXT,
                tool_name TEXT NOT NULL,
                command_template TEXT NOT NULL,
                actual_argv TEXT NOT NULL,
                working_directory TEXT NOT NULL,
                policy_version TEXT NOT NULL DEFAULT '',
                input_sha256 TEXT NOT NULL,
                output_sha256 TEXT NOT NULL,
                exit_code INTEGER NOT NULL,
                chain_hash TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                result_ref TEXT,
                FOREIGN KEY (case_id) REFERENCES cases(case_id)
            );
            CREATE INDEX IF NOT EXISTS idx_tool_runs_case ON tool_runs(case_id);
            CREATE INDEX IF NOT EXISTS idx_tool_runs_chain ON tool_runs(chain_hash);
            CREATE INDEX IF NOT EXISTS idx_tool_runs_sequence ON tool_runs(case_id, chain_sequence);

            CREATE TABLE IF NOT EXISTS artifacts (
                artifact_id TEXT PRIMARY KEY,
                case_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                uri TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (case_id) REFERENCES cases(case_id)
            );

            CREATE TABLE IF NOT EXISTS approval_grants (
                grant_id TEXT PRIMARY KEY,
                case_id TEXT NOT NULL,
                action TEXT NOT NULL,
                target_ref TEXT NOT NULL,
                approver TEXT NOT NULL,
                token_hash TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                issued_at TEXT NOT NULL,
                FOREIGN KEY (case_id) REFERENCES cases(case_id)
            );
            CREATE INDEX IF NOT EXISTS idx_approval_grants_token
                ON approval_grants(token_hash);

            CREATE TABLE IF NOT EXISTS approvals (
                approval_id TEXT PRIMARY KEY,
                case_id TEXT NOT NULL,
                grant_id TEXT,
                action TEXT NOT NULL,
                decision TEXT NOT NULL,
                approver TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                target_ref TEXT NOT NULL,
                token_hash TEXT,
                expires_at TEXT,
                resolved_at TEXT NOT NULL,
                FOREIGN KEY (case_id) REFERENCES cases(case_id)
            );
            CREATE INDEX IF NOT EXISTS idx_approvals_case ON approvals(case_id);

            CREATE TABLE IF NOT EXISTS knowledge_records (
                record_id TEXT PRIMARY KEY,
                case_id TEXT,
                status TEXT NOT NULL DEFAULT 'pending_review',
                content_ref TEXT NOT NULL,
                reuse_tags TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                reviewed_at TEXT,
                reviewed_by TEXT,
                review_note TEXT,
                FOREIGN KEY (case_id) REFERENCES cases(case_id)
            );
            """
        )

    def migrate_schema(self) -> None:
        event_columns = {row[1] for row in self.connection.execute("PRAGMA table_info(events)").fetchall()}
        project_columns = {row[1] for row in self.connection.execute("PRAGMA table_info(projects)").fetchall()}
        case_columns = {row[1] for row in self.connection.execute("PRAGMA table_info(cases)").fetchall()}
        needs_event_column = "conversation_id" not in event_columns
        needs_project_rebuild = "conversation_id" not in project_columns
        needs_name_column = not needs_project_rebuild and "conversation_name" not in project_columns
        tool_run_cols = {row[1] for row in self.connection.execute("PRAGMA table_info(tool_runs)").fetchall()}
        needs_patch_ref = "patch_ref" not in case_columns if case_columns else False
        needs_sandbox_ref = "sandbox_ref" not in case_columns if case_columns else False
        needs_repo_abs_path = "repo_abs_path" not in case_columns if case_columns else False
        needs_chain_sequence = "chain_sequence" not in tool_run_cols if tool_run_cols else False
        knowledge_cols = {row[1] for row in self.connection.execute("PRAGMA table_info(knowledge_records)").fetchall()}
        needs_review_note = "review_note" not in knowledge_cols if knowledge_cols else False

        if needs_event_column or needs_project_rebuild or needs_name_column:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                if needs_event_column:
                    self.connection.execute(
                        "ALTER TABLE events ADD COLUMN conversation_id TEXT NOT NULL DEFAULT 'default'"
                    )
                if needs_project_rebuild:
                    self.connection.execute("ALTER TABLE projects RENAME TO projects_legacy")
                    self.connection.execute(
                        """CREATE TABLE projects (
                            workspace TEXT NOT NULL, conversation_id TEXT NOT NULL,
                            conversation_name TEXT NOT NULL, name TEXT NOT NULL,
                            status TEXT NOT NULL, phase TEXT NOT NULL, focus TEXT NOT NULL,
                            note TEXT NOT NULL, evidence TEXT NOT NULL, event_type TEXT NOT NULL,
                            updated_at TEXT NOT NULL, event_count INTEGER NOT NULL DEFAULT 0,
                            PRIMARY KEY (workspace, conversation_id))"""
                    )
                    self.connection.execute(
                        """INSERT INTO projects (workspace, conversation_id, conversation_name, name,
                           status, phase, focus, note, evidence, event_type, updated_at, event_count)
                           SELECT workspace, 'default', '', name, status, phase, focus,
                                  note, evidence, event_type, updated_at, event_count
                           FROM projects_legacy"""
                    )
                    self.connection.execute("DROP TABLE projects_legacy")
                elif needs_name_column:
                    self.connection.execute(
                        "ALTER TABLE projects ADD COLUMN conversation_name TEXT NOT NULL DEFAULT ''"
                    )
            except Exception:
                self.connection.execute("ROLLBACK")
                raise
            self.connection.execute("COMMIT")

        # DevLoop schema migrations
        if needs_patch_ref:
            self.connection.execute("ALTER TABLE cases ADD COLUMN patch_ref TEXT")
        if needs_sandbox_ref:
            self.connection.execute("ALTER TABLE cases ADD COLUMN sandbox_ref TEXT")
        if needs_repo_abs_path:
            self.connection.execute("ALTER TABLE cases ADD COLUMN repo_abs_path TEXT NOT NULL DEFAULT ''")
        if needs_chain_sequence:
            self.connection.execute("ALTER TABLE tool_runs ADD COLUMN chain_sequence INTEGER NOT NULL DEFAULT 0")
            self.connection.execute("CREATE INDEX IF NOT EXISTS idx_tool_runs_sequence ON tool_runs(case_id, chain_sequence)")
        if needs_review_note:
            self.connection.execute("ALTER TABLE knowledge_records ADD COLUMN review_note TEXT")

        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS events_session_timestamp ON events(workspace, conversation_id, timestamp DESC)"
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS projects_updated ON projects(updated_at DESC)"
        )

    # ── Original Code CCTV ingest / state ──────────────────────────────────

    def ingest(self, payload: dict[str, Any]) -> dict[str, Any]:
        workspace_raw = clean_text(payload.get("workspace"), 1000)
        if not workspace_raw:
            raise ValueError("event.workspace is required")
        workspace = str(Path(workspace_raw).expanduser().resolve())
        conversation = clean_text(payload.get("conversation_id"), 200) or DEFAULT_CONVERSATION_ID
        conversation_title = clean_text(payload.get("conversation_name"), 200)
        name = clean_text(payload.get("workspace_name"), 200) or Path(workspace).name or workspace
        event_type = clean_text(payload.get("event_type"), 80) or "progress"
        source = clean_text(payload.get("source"), 80) or "code-cctv"
        timestamp = clean_text(payload.get("timestamp"), 80) or utc_now()
        phase = clean_text(payload.get("phase"), 120)
        status = clean_text(payload.get("status"), 120) or "侦察中"
        focus = clean_text(payload.get("focus"), 500)
        note = clean_text(payload.get("note"), 1200)
        evidence = clean_text(payload.get("evidence"), 1200)
        files = clean_files(payload.get("files"))
        event_id = uuid.uuid4().hex

        with self.lock:
            self.connection.execute(
                """INSERT INTO events (id, workspace, conversation_id, event_type, source, timestamp,
                   phase, status, focus, note, evidence, files_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (event_id, workspace, conversation, event_type, source, timestamp, phase,
                 status, focus, note, evidence, json.dumps(files, ensure_ascii=False)),
            )
            self.connection.execute(
                """INSERT INTO projects (workspace, conversation_id, conversation_name, name,
                   status, phase, focus, note, evidence, event_type, updated_at, event_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                   ON CONFLICT(workspace, conversation_id) DO UPDATE SET
                   conversation_name=CASE WHEN excluded.conversation_name <> '' THEN excluded.conversation_name ELSE projects.conversation_name END,
                   name=excluded.name, status=excluded.status, phase=excluded.phase,
                   focus=excluded.focus, note=excluded.note, evidence=excluded.evidence,
                   event_type=excluded.event_type, updated_at=excluded.updated_at,
                   event_count=projects.event_count + 1""",
                (workspace, conversation, conversation_title, name, status, phase, focus,
                 note, evidence, event_type, timestamp),
            )
            self._ingests_since_prune += 1
            if self._ingests_since_prune >= PRUNE_EVERY_INGESTS:
                self._ingests_since_prune = 0
                self.connection.execute(
                    """DELETE FROM events WHERE id IN (
                        SELECT id FROM (SELECT id, ROW_NUMBER() OVER (
                            PARTITION BY workspace, conversation_id ORDER BY timestamp DESC
                        ) AS event_rank FROM events) WHERE event_rank > ?)""",
                    (self.retention,),
                )
            self.connection.commit()
            return self.state_locked()

    def state(self) -> dict[str, Any]:
        with self.lock:
            return self.state_locked()

    def info(self) -> dict[str, Any]:
        with self.lock:
            sessions = self.connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
            events = self.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            cases = self.connection.execute("SELECT COUNT(*) FROM cases").fetchone()[0]
        try:
            db_bytes = self.path.stat().st_size
        except OSError:
            db_bytes = 0
        return {"retention": self.retention, "state_path": str(self.path),
                "total_sessions": sessions, "total_events": events,
                "total_cases": cases, "db_bytes": db_bytes}

    def delete_session(self, workspace: str, conversation_id: str) -> dict[str, Any]:
        with self.lock:
            self.connection.execute("DELETE FROM events WHERE workspace = ? AND conversation_id = ?",
                                    (workspace, conversation_id))
            self.connection.execute("DELETE FROM projects WHERE workspace = ? AND conversation_id = ?",
                                    (workspace, conversation_id))
            self.connection.commit()
            return self.state_locked()

    def clear_all(self) -> dict[str, Any]:
        with self.lock:
            self.connection.execute("DELETE FROM events")
            self.connection.execute("DELETE FROM projects")
            for tbl in ("case_sources", "agent_runs", "tool_runs", "artifacts",
                         "approval_grants", "approvals", "knowledge_records", "cases",
                         "drive_runs"):
                self.connection.execute(f"DELETE FROM {tbl}")
            self.connection.commit()
            return self.state_locked()

    def state_locked(self) -> dict[str, Any]:
        rows = self.connection.execute(
            """SELECT workspace, name, status, phase, focus, note, evidence,
               conversation_id, conversation_name, event_type, updated_at, event_count
               FROM projects ORDER BY updated_at DESC, conversation_id"""
        ).fetchall()
        now = datetime.now(timezone.utc)
        recent_events = self.recent_events_by_session(rows)
        projects: list[dict[str, Any]] = []
        for row in rows:
            project = dict(row)
            project["active"] = self.is_active(row["updated_at"], now) or self.is_watching(row["status"])
            project["recent_events"] = [
                self.event_dict(event) for event in
                recent_events.get((row["workspace"], row["conversation_id"]), [])
            ]
            projects.append(project)
        active = sum(1 for p in projects if p["active"])
        blocked = sum(1 for p in projects if "阻塞" in p["status"] or "blocked" in p["status"].lower())
        return {
            "generated_at": utc_now(),
            "summary": {"total_projects": len(projects), "active_projects": active,
                         "blocked_projects": blocked,
                         "event_count": sum(p["event_count"] for p in projects)},
            "projects": projects,
        }

    def recent_events_by_session(self, rows):
        event_query = """SELECT id, workspace, conversation_id, event_type, source, timestamp,
            phase, status, focus, note, evidence, files_json FROM (
            SELECT events.*, ROW_NUMBER() OVER (PARTITION BY workspace, conversation_id
            ORDER BY timestamp DESC) AS event_rank FROM events) WHERE event_rank <= 8"""
        try:
            event_rows = self.connection.execute(event_query).fetchall()
        except sqlite3.OperationalError:
            event_rows = []
            for row in rows:
                event_rows.extend(self.connection.execute(
                    "SELECT id, workspace, conversation_id, event_type, source, timestamp, "
                    "phase, status, focus, note, evidence, files_json FROM events "
                    "WHERE workspace = ? AND conversation_id = ? ORDER BY timestamp DESC LIMIT 8",
                    (row["workspace"], row["conversation_id"])).fetchall())
        grouped: dict[tuple[str, str], list] = {}
        for event in event_rows:
            grouped.setdefault((event["workspace"], event["conversation_id"]), []).append(event)
        return grouped

    @staticmethod
    def is_active(value: str, now: datetime) -> bool:
        try:
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return now - timestamp <= timedelta(seconds=120)

    @staticmethod
    def is_watching(status: str) -> bool:
        return "监听" in status or "watch" in status.casefold() or "running" in status.casefold()

    @staticmethod
    def event_dict(row):
        try:
            files = json.loads(row["files_json"])
        except json.JSONDecodeError:
            files = []
        return {"id": row["id"], "conversation_id": row["conversation_id"],
                "event_type": row["event_type"], "source": row["source"],
                "timestamp": row["timestamp"], "phase": row["phase"],
                "status": row["status"], "focus": row["focus"],
                "note": row["note"], "evidence": row["evidence"],
                "files": files if isinstance(files, list) else []}

    def _publish_case_event(self, event_type: str, case: dict[str, Any],
                            extra: dict[str, Any] | None = None) -> None:
        """Push a Case-level SSE event if a publish callback is registered.

        ``extra`` merges into the payload (backward compatible — existing
        callers pass nothing).
        """
        if self.publish_callback:
            try:
                self.publish_callback({"type": event_type, "case": case, **(extra or {})})
            except Exception:
                pass

    # ═══════════════════════════════════════════════════════════════════════
    # DevLoop: Case ingestion & management
    # ═══════════════════════════════════════════════════════════════════════

    def create_or_find_case(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            return self._create_or_find_case_locked(payload)

    def _create_or_find_case_locked(self, payload: dict[str, Any]) -> dict[str, Any]:
        source_type = clean_text(payload.get("source_type"), 40) or "unknown"
        source_uri = clean_text(payload.get("source_uri"), 500) or "unknown"
        client_nonce = clean_text(payload.get("client_nonce"), 64) or uuid.uuid4().hex
        raw_content = clean_text(payload.get("raw_content"), 10000) or ""
        repository_ref = clean_text(payload.get("repository_ref"), 1000)
        signals_raw = payload.get("extracted_signals", {})
        if not isinstance(signals_raw, dict):
            signals_raw = {}
        exception_type = clean_text(signals_raw.get("exception_type"), 200) or None
        message_pattern = clean_text(signals_raw.get("message_pattern"), 500) or None
        key_frames = signals_raw.get("key_frames", [])
        if not isinstance(key_frames, list):
            key_frames = []
        keywords = signals_raw.get("keywords", [])
        if not isinstance(keywords, list):
            keywords = []
        if not repository_ref:
            repository_ref = clean_text(signals_raw.get("repository_ref"), 1000) or "unknown"

        # Canonicalize repository identity: one real repo must not fragment into
        # many Cases under different spellings (see daemon/repo_identity.py).
        from .repo_identity import canonical_repo_identity, resolve_base_commit

        identity = canonical_repo_identity(repository_ref)
        canonical_ref = identity["canonical_ref"] or repository_ref
        repo_abs_path = identity["abs_path"]

        now = utc_now()

        # ── Step 1: delivery idempotency ─────────────────────────────────
        delivery_id = compute_delivery_id(source_type, source_uri, client_nonce)
        existing = self.connection.execute(
            "SELECT observation_id FROM case_sources WHERE delivery_id = ? AND received_at > ?",
            (delivery_id, (datetime.now(timezone.utc)
             - timedelta(seconds=DEFAULT_IDEMPOTENCY_WINDOW_S)).isoformat().replace("+00:00", "Z")),
        ).fetchone()
        if existing:
            obs = self.connection.execute(
                "SELECT * FROM case_sources WHERE observation_id = ?", (existing["observation_id"],)
            ).fetchone()
            return {"duplicate": True, "observation_id": existing["observation_id"],
                    "case_id": obs["case_id"] if obs else None}

        content_hash = compute_content_hash(raw_content)
        observation_id = uuid.uuid4().hex

        # ── Step 2: extract signals and compute incident_signature ───────
        incident_sig = None
        signals_complete = bool(exception_type and message_pattern and key_frames)
        if signals_complete:
            incident_sig = compute_incident_signature(
                canonical_ref, exception_type, message_pattern, key_frames)

        signals_json = json.dumps({
            "exception_type": exception_type, "message_pattern": message_pattern,
            "key_frames": key_frames, "keywords": keywords,
            "repository_ref": canonical_ref,
            # Backward-compat: the demo repair path reads this to select the
            # controlled Case A sandbox.  Not a real-project security gate —
            # that lands with the policy milestone.
            "repair_mode": clean_text(signals_raw.get("repair_mode"), 80),
        }, ensure_ascii=False)

        # ── Step 3: find or create Case ──────────────────────────────────
        matched_case_id: str | None = None
        association_state = "pending"
        candidate_cases = None
        association_confidence = None
        association_deadline = None

        if incident_sig:
            cutoff = (datetime.now(timezone.utc)
                      - timedelta(seconds=DEFAULT_INCIDENT_WINDOW_S)).isoformat().replace("+00:00", "Z")
            match = self.connection.execute(
                """SELECT c.case_id FROM cases c
                   WHERE c.incident_signature = ? AND c.updated_at > ?
                   AND c.status NOT IN ('CLOSED', 'ESCALATED')
                   ORDER BY c.created_at ASC LIMIT 1""",
                (incident_sig, cutoff),
            ).fetchone()
            if match:
                matched_case_id = match["case_id"]
                association_state = "linked"
                association_confidence = 1.0
        elif repository_ref and repository_ref != "unknown" and keywords:
            candidate_rows = self.connection.execute(
                """SELECT c.case_id FROM cases c
                   WHERE c.repository_ref = ? AND c.status NOT IN ('CLOSED', 'ESCALATED')
                   AND c.updated_at > ?
                   ORDER BY c.created_at DESC LIMIT 5""",
                (repository_ref,
                 (datetime.now(timezone.utc)
                  - timedelta(seconds=DEFAULT_ASSOCIATION_DEADLINE_S)).isoformat().replace("+00:00", "Z")),
            ).fetchall()
            if candidate_rows:
                cand_list = [r["case_id"] for r in candidate_rows]
                association_confidence = 0.5
                candidate_cases = json.dumps(cand_list, ensure_ascii=False)
                association_deadline = (
                    datetime.now(timezone.utc) + timedelta(seconds=DEFAULT_ASSOCIATION_DEADLINE_S)
                ).isoformat().replace("+00:00", "Z")

        # ── Step 3b: only create a Case when we have complete signals
        #    or a high-confidence match.  Incomplete signals stay pending with
        #    case_id = NULL and will be promoted by resolve_pending_sources().
        if matched_case_id is None:
            if signals_complete:
                # Complete signals → create Case immediately
                matched_case_id = f"case-{uuid.uuid4().hex[:12]}"
                trace_id = f"trace-{uuid.uuid4().hex[:16]}"
                # Resolve the reviewed base commit once, at intake, so approval
                # target_ref validation has a real git SHA (not test-only SQL).
                base_commit = resolve_base_commit(repo_abs_path) if identity["is_git"] else None
                self.connection.execute(
                    """INSERT INTO cases (case_id, incident_signature, status, priority,
                       risk_level, repository_ref, repo_abs_path, base_commit,
                       trace_id, title, created_at, updated_at)
                       VALUES (?, ?, 'RECEIVED', 'medium', 'low', ?, ?, ?, ?, ?, ?, ?)""",
                    (matched_case_id, incident_sig, canonical_ref, repo_abs_path, base_commit,
                     trace_id,
                     clean_text(payload.get("title"), 200) or f"Case from {source_type}",
                     now, now),
                )
                association_state = "linked"
                if self.publish_callback:
                    case = self._case_dict(matched_case_id)
                    self._publish_case_event("case_created", case)
            else:
                # Incomplete signals → leave case_id NULL, pending association
                matched_case_id = None
                association_state = "pending"

        # ── Step 4: persist source observation ──────────────────────────
        self.connection.execute(
            """INSERT INTO case_sources (
                observation_id, case_id, source_type, source_uri, delivery_id,
                content_hash, incident_signature, extracted_signals_json,
                association_state, candidate_cases, association_confidence,
                association_deadline, received_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (observation_id, matched_case_id, source_type, source_uri, delivery_id,
             content_hash, incident_sig, signals_json,
             association_state, candidate_cases, association_confidence,
             association_deadline, now),
        )

        if matched_case_id:
            self.connection.execute(
                "UPDATE cases SET updated_at = ?, incident_signature = COALESCE(incident_signature, ?) "
                "WHERE case_id = ?",
                (now, incident_sig, matched_case_id),
            )

        self.connection.commit()

        if matched_case_id:
            return self._case_dict(matched_case_id)
        return {"pending": True, "observation_id": observation_id,
                "association_deadline": association_deadline}

    def get_case(self, case_id: str) -> dict[str, Any] | None:
        with self.lock:
            return self._case_dict(case_id)

    def list_cases(self, status=None, repository_ref=None, limit=50) -> list[dict[str, Any]]:
        with self.lock:
            query = "SELECT case_id FROM cases WHERE 1=1"
            params: list[Any] = []
            if status:
                query += " AND status = ?"
                params.append(status)
            if repository_ref:
                query += " AND repository_ref = ?"
                params.append(repository_ref)
            query += " ORDER BY updated_at DESC LIMIT ?"
            params.append(limit)
            rows = self.connection.execute(query, params).fetchall()
            return [c for row in rows if (c := self._case_dict(row["case_id"])) is not None]

    def perform_case_action(self, case_id: str, action: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Execute an action on a Case.  *approve_plan*, *approve_release*,
        *reject_plan*, and *reject_release* all require a valid approval Grant
        (same auth model).  *cancel* requires *service_token* (enforced by server)."""
        with self.lock:
            case = self.connection.execute(
                "SELECT * FROM cases WHERE case_id = ?", (case_id,)
            ).fetchone()
            if case is None:
                return None
            now = utc_now()

            if action in ALL_GRANTED_ACTIONS:
                # ── Grant-required path (approve or reject) ──────────
                approval_token = clean_text(payload.get("approval_token"), 128)
                if not approval_token:
                    return {"error": "approval_token is required", "status": 401}
                token_hash = compute_approval_token_hash(approval_token)
                grant = self.connection.execute(
                    """SELECT * FROM approval_grants
                       WHERE token_hash = ? AND case_id = ? AND action = ? AND used = 0""",
                    (token_hash, case_id, action),
                ).fetchone()
                if grant is None:
                    return {"error": "invalid or already used approval token", "status": 401}
                if grant["expires_at"] < now:
                    return {"error": "approval token expired", "status": 401}
                target_ref = clean_text(payload.get("target_ref"), 200)
                if target_ref and target_ref != grant["target_ref"]:
                    return {"error": "target_ref mismatch", "status": 409}

                # ── Validate case state hasn't changed since grant issue ──
                expected = {
                    "approve_plan":   ("PLAN_APPROVAL",   "approve_plan"),
                    "approve_release":("RELEASE_APPROVAL", "approve_release"),
                    "reject_plan":    ("PLAN_APPROVAL",   "approve_plan"),
                    "reject_release": ("RELEASE_APPROVAL", "approve_release"),
                }
                exp_state, exp_pending = expected[action]
                if case["status"] != exp_state:
                    return {"error": f"case state changed: expected {exp_state}, current {case['status']}", "status": 409}
                if case["pending_action"] != exp_pending:
                    return {"error": f"case pending_action changed: expected {exp_pending}, current {case['pending_action']}", "status": 409}

                # Consume grant
                self.connection.execute(
                    "UPDATE approval_grants SET used = 1 WHERE grant_id = ?", (grant["grant_id"],))

                decision = "approved" if action in APPROVAL_ACTIONS else "rejected"
                approval_id = uuid.uuid4().hex
                self.connection.execute(
                    """INSERT INTO approvals (approval_id, case_id, grant_id, action, decision,
                       approver, reason, target_ref, token_hash, expires_at, resolved_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (approval_id, case_id, grant["grant_id"], action, decision,
                     grant["approver"], clean_text(payload.get("reason"), 500),
                     grant["target_ref"], token_hash, grant["expires_at"], now),
                )

                # State transition
                if action == "approve_plan":
                    new_status = "REPAIRING"
                elif action == "approve_release":
                    new_status = "RELEASED"
                elif action == "reject_plan":
                    new_status = "ESCALATED"
                elif action == "reject_release":
                    new_status = "ESCALATED"
                else:
                    new_status = case["status"]

                self.connection.execute(
                    "UPDATE cases SET status = ?, pending_action = NULL, updated_at = ? WHERE case_id = ?",
                    (new_status, now, case_id),
                )

            elif action == "cancel":
                reason = clean_text(payload.get("reason"), 500)
                approval_id = uuid.uuid4().hex
                self.connection.execute(
                    """INSERT INTO approvals (approval_id, case_id, action, decision,
                       approver, reason, target_ref, resolved_at)
                       VALUES (?, ?, 'cancel', 'cancelled', ?, ?, ?, ?)""",
                    (approval_id, case_id,
                     clean_text(payload.get("approver"), 100) or "system",
                     reason, clean_text(payload.get("target_ref"), 200) or "", now),
                )
                self.connection.execute(
                    "UPDATE cases SET status = 'ESCALATED', pending_action = NULL, updated_at = ? WHERE case_id = ?",
                    (now, case_id),
                )
            else:
                return {"error": f"unknown action: {action}", "status": 400}

            self.connection.commit()
            result = self._case_dict(case_id)
            self._publish_case_event("case_action", result)
            return result

    def transition_case(self, case_id: str, new_status: str,
                        pending_action: str | None = None) -> dict[str, Any] | None:
        """State transition (used by orchestration layer).  Validates against
        the state machine — invalid transitions are rejected."""
        trigger_retrospective = False
        with self.lock:
            case = self.connection.execute(
                "SELECT * FROM cases WHERE case_id = ?", (case_id,)
            ).fetchone()
            if case is None:
                return None

            current = case["status"]
            ivt = _get_validator()
            if not ivt(current, new_status):
                return {"error": f"invalid transition: {current} -> {new_status}"}

            now = utc_now()
            # Only CLOSED is truly terminal.  ESCALATED can reopen to
            # REPAIRING, so it must not leave a stale closed_at behind.
            if new_status == "CLOSED":
                self.connection.execute(
                    """UPDATE cases SET status = ?, pending_action = ?,
                       updated_at = ?, closed_at = ?
                       WHERE case_id = ?""",
                    (new_status, pending_action, now, now, case_id),
                )
            else:
                # Always clear closed_at on non-terminal transitions
                self.connection.execute(
                    """UPDATE cases SET status = ?, pending_action = ?,
                       updated_at = ?, closed_at = NULL
                       WHERE case_id = ?""",
                    (new_status, pending_action, now, case_id),
                )
            self.connection.commit()
            result = self._case_dict(case_id)
            self._publish_case_event("case_transition", result)
            trigger_retrospective = (
                new_status in RETROSPECTIVE_TRIGGER_STATES
                and result is not None
                and "error" not in result
            )
        # Fire the async retrospective hook outside the lock so report
        # generation never blocks the transition or its SSE broadcast.
        if trigger_retrospective:
            self._maybe_trigger_retrospective(case_id)
        return result

    # ── DevLoop: approval grant management ─────────────────────────────────

    def issue_approval_grant(self, case_id: str, action: str, target_ref: str,
                             approver: str, expires_at: str | None = None) -> dict[str, Any] | None:
        """Issue a server-signed one-time approval/rejection Grant.

        Validates that the Case is in the expected state and that *target_ref*
        matches the relevant version (base_commit for plan actions, patch_ref
        for release actions).

        Called from the local IPC channel — NOT an HTTP endpoint.
        """
        with self.lock:
            case = self.connection.execute(
                "SELECT * FROM cases WHERE case_id = ?", (case_id,)
            ).fetchone()
            if case is None:
                return None
            if action not in ALL_GRANTED_ACTIONS:
                return {"error": f"unknown grant action: {action}"}

            # ── Validate state and pending_action ──────────────────────
            expected = {
                "approve_plan":   ("PLAN_APPROVAL",    "approve_plan",    "base_commit"),
                "approve_release":("RELEASE_APPROVAL",  "approve_release", "patch_ref"),
                "reject_plan":    ("PLAN_APPROVAL",    "approve_plan",    "base_commit"),
                "reject_release": ("RELEASE_APPROVAL",  "approve_release", "patch_ref"),
            }
            exp_state, exp_pending, version_field = expected[action]
            if case["status"] != exp_state:
                return {"error": f"case must be in {exp_state} to issue {action} grant, current: {case['status']}"}
            if case["pending_action"] != exp_pending:
                return {"error": f"case pending_action must be {exp_pending}, current: {case['pending_action']}"}
            # Validate target_ref against the version field
            version_val = (case[version_field] or "").strip()
            if not version_val:
                return {"error": f"case.{version_field} is not set — cannot issue {action} grant"}
            if target_ref.strip() != version_val:
                return {"error": f"target_ref mismatch: grant is for '{target_ref}', case.{version_field} is '{version_val}'"}

            now = utc_now()
            if expires_at is None:
                expires_at = (datetime.now(timezone.utc)
                              + timedelta(seconds=DEFAULT_APPROVAL_EXPIRY_S)).isoformat().replace("+00:00", "Z")

            approval_token = f"at-{secrets.token_hex(32)}"
            token_hash = compute_approval_token_hash(approval_token)
            grant_id = f"grant-{uuid.uuid4().hex[:12]}"

            self.connection.execute(
                """INSERT INTO approval_grants (grant_id, case_id, action, target_ref, approver,
                   token_hash, expires_at, issued_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (grant_id, case_id, action, target_ref, approver, token_hash, expires_at, now),
            )
            self.connection.commit()
            return {"grant_id": grant_id, "approval_token": approval_token,
                    "case_id": case_id, "action": action,
                    "target_ref": target_ref, "expires_at": expires_at}

    def resolve_pending_sources(self) -> list[dict[str, Any]]:
        """Find source observations past their association deadline that have
        no linked Case, and promote each to an independent Case.

        Returns the list of newly created Case dicts.
        """
        with self.lock:
            now = utc_now()
            orphaned = self.connection.execute(
                """SELECT cs.observation_id, cs.source_type, cs.source_uri,
                   cs.incident_signature, cs.extracted_signals_json
                   FROM case_sources cs
                   WHERE cs.association_state = 'pending'
                   AND cs.association_deadline < ?
                   AND cs.case_id IS NULL""",
                (now,),
            ).fetchall()
            created = []
            for row in orphaned:
                new_case_id = f"case-{uuid.uuid4().hex[:12]}"
                trace_id = f"trace-{uuid.uuid4().hex[:16]}"
                # Decode signals to get repository_ref
                try:
                    signals = json.loads(row["extracted_signals_json"])
                except json.JSONDecodeError:
                    signals = {}
                repo_ref = clean_text(signals.get("repository_ref") or "", 1000) or "unknown"
                title = f"Case from {row['source_type']}: {row['source_uri'][:80]}"
                self.connection.execute(
                    """INSERT INTO cases (case_id, incident_signature, status, priority,
                       risk_level, repository_ref, trace_id, title, created_at, updated_at)
                       VALUES (?, ?, 'RECEIVED', 'medium', 'low', ?, ?, ?, ?, ?)""",
                    (new_case_id, row["incident_signature"], repo_ref, trace_id, title, now, now),
                )
                # Link the pending observation to the new Case
                self.connection.execute(
                    """UPDATE case_sources SET case_id = ?, association_state = 'orphaned'
                       WHERE observation_id = ?""",
                    (new_case_id, row["observation_id"]),
                )
                case = self._case_dict(new_case_id)
                created.append(case)
                self._publish_case_event("case_created", case)
            self.connection.commit()
            return created

    # ── DevLoop: evidence / tool runs ──────────────────────────────────────

    def set_patch_context(self, case_id: str, patch_ref: str, sandbox_ref: str) -> dict[str, Any] | None:
        """Persist the patch identity and its isolated workspace reference."""
        with self.lock:
            self.connection.execute(
                "UPDATE cases SET patch_ref = ?, sandbox_ref = ?, updated_at = ? WHERE case_id = ?",
                (clean_text(patch_ref, 200), clean_text(sandbox_ref, 1000), utc_now(), case_id),
            )
            self.connection.commit()
            return self._case_dict(case_id)

    def record_tool_run(self, payload: dict[str, Any]) -> str:
        """Insert a COMPLETE immutable tool run record in one atomic write.

        All fields — including exit_code, output_sha256, chain_hash — must be
        present at insertion time.  The record is never updated afterward.

        Uses a monotonic *chain_sequence* per Case for stable hash chain
        ordering even under concurrent same-second writes.
        """
        with self.lock:
            run_id = f"tool-{uuid.uuid4().hex[:12]}"
            now = utc_now()
            case_id = payload["case_id"]

            # Allocate the next chain_sequence within this Case
            last_seq = self.connection.execute(
                "SELECT MAX(chain_sequence) FROM tool_runs WHERE case_id = ?",
                (case_id,),
            ).fetchone()[0]
            chain_sequence = (last_seq or 0) + 1

            # chain_hash = SHA256(previous_chain_hash || canonical_this_record)
            prev = self.connection.execute(
                "SELECT chain_hash FROM tool_runs WHERE case_id = ? AND chain_sequence = ?",
                (case_id, chain_sequence - 1),
            ).fetchone()
            prev_hash = prev["chain_hash"] if prev else case_id

            started_at = clean_text(payload.get("started_at"), 80) or now
            canonical = json.dumps({
                "run_id": run_id, "case_id": case_id, "chain_sequence": chain_sequence,
                "agent_id": payload.get("agent_id", ""),
                "tool_name": payload.get("tool_name", ""),
                "actual_argv": payload.get("actual_argv", ""),
                "working_directory": payload.get("working_directory", ""),
                "input_sha256": payload.get("input_sha256", ""),
                "output_sha256": payload.get("output_sha256", ""),
                "exit_code": payload.get("exit_code"),
                "started_at": started_at,
                "finished_at": now,
            }, sort_keys=True, ensure_ascii=False)
            chain_hash = sha256(f"{prev_hash}|{canonical}")

            self.connection.execute(
                """INSERT INTO tool_runs (
                    run_id, case_id, chain_sequence, agent_id, approval_id, tool_name,
                    command_template, actual_argv, working_directory,
                    policy_version, input_sha256, output_sha256,
                    exit_code, chain_hash, started_at, finished_at, result_ref
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, case_id, chain_sequence,
                 clean_text(payload.get("agent_id"), 80),
                 clean_text(payload.get("approval_id"), 64) or None,
                 clean_text(payload.get("tool_name"), 80),
                 clean_text(payload.get("command_template"), 500),
                 clean_text(payload.get("actual_argv"), 2000),
                 clean_text(payload.get("working_directory"), 1000),
                 clean_text(payload.get("policy_version"), 80),
                 clean_text(payload.get("input_sha256"), 64),
                 clean_text(payload.get("output_sha256"), 64),
                 payload.get("exit_code", -1),
                 chain_hash, started_at, now,
                 clean_text(payload.get("result_ref"), 200) or None),
            )
            self.connection.commit()
            return run_id

    def _artifact_target(self, artifact_id: str, case_id: str, suggested_uri: str) -> tuple[str, Path]:
        """Return a Store-controlled relative URI and its absolute target path."""
        safe_case_id = "".join(
            char if char.isalnum() or char in "-_" else "_" for char in case_id
        )[:80] or "unknown-case"
        suffix = Path(clean_text(suggested_uri, 240)).suffix.lower()
        if not suffix or len(suffix) > 16 or not suffix[1:].isalnum():
            suffix = ".bin"
        relative_path = Path("artifacts") / safe_case_id / f"{artifact_id}{suffix}"
        return relative_path.as_posix(), self.path.parent / relative_path

    def record_artifact(self, case_id: str, kind: str, uri: str, file_content: bytes) -> str:
        """Persist artifact bytes atomically and record a hash-checked reference.

        ``uri`` is a suggested extension/name only. The Store generates the
        final relative URI to prevent callers from escaping its data directory.
        """
        if not isinstance(file_content, (bytes, bytearray)):
            raise TypeError("file_content must be bytes")

        artifact_id = f"art-{uuid.uuid4().hex[:12]}"
        content = bytes(file_content)
        art_sha256 = hashlib.sha256(content).hexdigest()
        stored_uri, target_path = self._artifact_target(artifact_id, case_id, uri)
        temporary_path = target_path.with_name(
            f".{target_path.name}.{uuid.uuid4().hex}.tmp"
        )

        with self.lock:
            try:
                target_path.parent.mkdir(parents=True, exist_ok=True)
                with open(temporary_path, "xb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                if sys.platform != "win32":
                    temporary_path.chmod(0o600)
                os.replace(temporary_path, target_path)

                # Verify the final bytes before their database reference commits.
                if hashlib.sha256(target_path.read_bytes()).hexdigest() != art_sha256:
                    raise OSError("artifact hash mismatch after write")

                self.connection.execute(
                    "INSERT INTO artifacts (artifact_id, case_id, kind, uri, sha256, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (artifact_id, case_id, kind, stored_uri, art_sha256, utc_now()),
                )
                self.connection.commit()
                return artifact_id
            except Exception:
                temporary_path.unlink(missing_ok=True)
                # artifact_id is unique, so this cleanup cannot remove another record's file.
                target_path.unlink(missing_ok=True)
                raise

    def read_artifact(self, artifact_id: str) -> bytes | None:
        """Read a persisted artifact and verify it against the recorded hash."""
        with self.lock:
            row = self.connection.execute(
                "SELECT uri, sha256 FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
            if row is None:
                return None

            relative_path = Path(row["uri"])
            if relative_path.is_absolute() or not relative_path.parts or relative_path.parts[0] != "artifacts":
                raise ValueError(f"artifact {artifact_id} has an unsafe URI")
            target_path = (self.path.parent / relative_path).resolve()
            artifact_root = self.artifact_root.resolve()
            if artifact_root not in target_path.parents:
                raise ValueError(f"artifact {artifact_id} escapes artifact root")
            if not target_path.is_file():
                return None

            content = target_path.read_bytes()
            if hashlib.sha256(content).hexdigest() != row["sha256"]:
                raise ValueError(f"artifact {artifact_id} failed hash verification")
            return content

    def get_case_evidence(self, case_id: str) -> dict[str, Any] | None:
        with self.lock:
            case = self._case_dict(case_id)
            if case is None:
                return None
            return {
                "case": case,
                "sources": [dict(r) for r in self.connection.execute(
                    "SELECT * FROM case_sources WHERE case_id = ? ORDER BY received_at", (case_id,)).fetchall()],
                "agent_runs": [dict(r) for r in self.connection.execute(
                    "SELECT * FROM agent_runs WHERE case_id = ? ORDER BY started_at", (case_id,)).fetchall()],
                "tool_runs": [dict(r) for r in self.connection.execute(
                    "SELECT * FROM tool_runs WHERE case_id = ? ORDER BY chain_sequence", (case_id,)).fetchall()],
                "approvals": [dict(r) for r in self.connection.execute(
                    "SELECT * FROM approvals WHERE case_id = ? ORDER BY resolved_at", (case_id,)).fetchall()],
                "artifacts": [dict(r) for r in self.connection.execute(
                    "SELECT * FROM artifacts WHERE case_id = ? ORDER BY created_at", (case_id,)).fetchall()],
                "knowledge_records": self.list_knowledge_records(case_id=case_id),
                "retrospective": self.get_retrospective(case_id),
            }

    # ── Monitored projects (user-selected, watched by ProjectMonitor) ─────

    def register_monitored_project(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Register (or update) a project to be monitored.

        *workspace* is the absolute project path; *kind* is ``git`` (has a
        ``.git`` marker) or ``process`` (a running-process working directory).
        Repository identity is canonicalized and the current git HEAD stored as
        *base_commit* when available.
        """
        from .repo_identity import canonical_repo_identity, resolve_base_commit

        workspace = clean_text(payload.get("workspace"), 2000)
        if not workspace:
            raise ValueError("workspace is required")
        abs_path = str(Path(workspace).expanduser().resolve())
        if not Path(abs_path).is_dir():
            raise ValueError(f"workspace is not a directory: {abs_path}")

        identity = canonical_repo_identity(abs_path)
        kind = clean_text(payload.get("kind"), 20) or ("git" if identity["is_git"] else "process")
        name = clean_text(payload.get("name"), 200) or Path(abs_path).name
        git_remote = identity["git_remote"] or clean_text(payload.get("git_remote"), 500)
        branch = clean_text(payload.get("branch"), 200)
        base_commit = resolve_base_commit(abs_path) if identity["is_git"] else clean_text(
            payload.get("base_commit"), 200) or None
        watcher_config = payload.get("watcher_config")
        if not isinstance(watcher_config, dict):
            watcher_config = {}
        now = utc_now()

        with self.lock:
            existing = self.connection.execute(
                "SELECT * FROM monitored_projects WHERE workspace = ?", (abs_path,)
            ).fetchone()
            if existing:
                self.connection.execute(
                    """UPDATE monitored_projects SET name = ?, kind = ?, git_remote = ?,
                       branch = ?, base_commit = ?, watcher_config = ?, last_seen = ?,
                       status = ?, last_error = NULL, canonical_ref = ? WHERE workspace = ?""",
                    (name, kind, git_remote, branch, base_commit,
                     json.dumps(watcher_config, ensure_ascii=False), now,
                     clean_text(payload.get("status"), 20) or existing["status"],
                     identity["canonical_ref"], abs_path),
                )
            else:
                self.connection.execute(
                    """INSERT INTO monitored_projects (workspace, name, kind, git_remote,
                       branch, base_commit, watcher_config, selected_at, last_seen, status,
                       canonical_ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (abs_path, name, kind, git_remote, branch, base_commit,
                     json.dumps(watcher_config, ensure_ascii=False), now, now,
                     clean_text(payload.get("status"), 20) or "pending",
                     identity["canonical_ref"]),
                )
            self.connection.commit()
            return self.get_monitored_project(abs_path) or {}

    def unregister_monitored_project(self, workspace: str) -> bool:
        abs_path = str(Path(workspace).expanduser().resolve())
        with self.lock:
            cursor = self.connection.execute(
                "DELETE FROM monitored_projects WHERE workspace = ?", (abs_path,))
            self.connection.commit()
            return cursor.rowcount > 0

    def list_monitored_projects(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM monitored_projects ORDER BY selected_at DESC").fetchall()
            return [dict(r) for r in rows]

    def get_monitored_project(self, workspace: str) -> dict[str, Any] | None:
        abs_path = str(Path(workspace).expanduser().resolve())
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM monitored_projects WHERE workspace = ?", (abs_path,)).fetchone()
            return dict(row) if row else None

    def update_monitored_project_status(
        self, workspace: str, status: str, last_error: str | None = None,
    ) -> None:
        abs_path = str(Path(workspace).expanduser().resolve())
        with self.lock:
            self.connection.execute(
                "UPDATE monitored_projects SET status = ?, last_error = ?, last_seen = ? "
                "WHERE workspace = ?",
                (status, last_error, utc_now(), abs_path))
            self.connection.commit()

    def set_monitored_project_base_commit(self, workspace: str, commit: str | None) -> None:
        abs_path = str(Path(workspace).expanduser().resolve())
        with self.lock:
            self.connection.execute(
                "UPDATE monitored_projects SET base_commit = ? WHERE workspace = ?",
                (commit, abs_path))
            self.connection.commit()

    def set_monitored_project_scan_state(self, workspace: str, state_json: str) -> None:
        abs_path = str(Path(workspace).expanduser().resolve())
        with self.lock:
            self.connection.execute(
                "UPDATE monitored_projects SET last_scan_state = ? WHERE workspace = ?",
                (state_json, abs_path))
            self.connection.commit()

    # ── Automated drive runs ─────────────────────────────────────────────

    def begin_drive_run(self, workspace: str) -> str:
        run_id = f"drive-{uuid.uuid4().hex[:12]}"
        abs_path = str(Path(workspace).expanduser().resolve())
        with self.lock:
            self.connection.execute(
                "INSERT INTO drive_runs (run_id, workspace, status, started_at) "
                "VALUES (?, ?, 'running', ?)",
                (run_id, abs_path, utc_now()))
            self.connection.commit()
        return run_id

    def finish_drive_run(
        self, run_id: str, status: str, duration_s: float,
        browse: dict[str, Any] | None, llm: dict[str, Any] | None,
        error: str | None,
    ) -> None:
        with self.lock:
            self.connection.execute(
                "UPDATE drive_runs SET status = ?, finished_at = ?, duration_s = ?, "
                "browse_json = ?, llm_json = ?, error = ? WHERE run_id = ?",
                (status, utc_now(), duration_s,
                 json.dumps(browse, ensure_ascii=False) if browse else None,
                 json.dumps(llm, ensure_ascii=False) if llm else None,
                 error, run_id))
            self.connection.commit()

    def _drive_run_dict(self, row: Any) -> dict[str, Any]:
        data = dict(row)
        for key, field in (("browse_json", "browse"), ("llm_json", "llm")):
            raw = data.get(key)
            data[field] = json.loads(raw) if raw else None
            data.pop(key, None)
        return data

    def get_latest_drive_run(self, workspace: str) -> dict[str, Any] | None:
        abs_path = str(Path(workspace).expanduser().resolve())
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM drive_runs WHERE workspace = ? ORDER BY started_at DESC LIMIT 1",
                (abs_path,)).fetchone()
            return self._drive_run_dict(row) if row else None

    def list_drive_runs(self, workspace: str, limit: int = 10) -> list[dict[str, Any]]:
        abs_path = str(Path(workspace).expanduser().resolve())
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM drive_runs WHERE workspace = ? ORDER BY started_at DESC LIMIT ?",
                (abs_path, limit)).fetchall()
            return [self._drive_run_dict(r) for r in rows]

    def project_summary(self, workspace: str | None = None, days: int = 14) -> dict[str, Any]:
        """Deterministic, evidence-derived aggregates for the project dashboard.

        When *workspace* is given, all rollups are scoped to Cases whose
        ``repo_abs_path`` equals it (and the activity timeline's event series
        scoped to ``events.workspace``).  When ``None``, aggregates across all
        projects (legacy/global behaviour).

        Never invokes an LLM and never reads model prose.  Safe to call from a
        request handler: it takes the store lock internally and returns plain
        JSON-friendly structures, so the caller can run an optional LLM step
        *outside* the lock.
        """
        with self.lock:
            def rollup(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
                return [dict(r) for r in self.connection.execute(sql, params).fetchall()]

            # For a scoped summary, every per-case rollup joins through `cases`
            # filtered by repo_abs_path; the events timeline uses events.workspace.
            case_filter = ""
            event_filter = ""
            params: tuple[str, ...] = ()
            if workspace:
                case_filter = " WHERE c.repo_abs_path = ?"
                event_filter = " WHERE workspace = ?"
                params = (workspace,)

            def scoped(sql: str, join_case: bool = False) -> str:
                # Insert the WHERE clause before GROUP BY (SQL requires WHERE
                # before GROUP BY).  `sql` uses table alias `c` for cases.
                where = case_filter if join_case else event_filter
                if "GROUP BY" in sql:
                    head, _, tail = sql.partition("GROUP BY")
                    return head + where + " GROUP BY" + tail
                return sql + where

            if workspace:
                case_counts_by_status = rollup(
                    scoped("SELECT c.status AS status, COUNT(*) AS count FROM cases c"
                           " GROUP BY c.status ORDER BY count DESC", True), params)
                case_counts_by_priority = rollup(
                    scoped("SELECT c.priority AS priority, COUNT(*) AS count FROM cases c"
                           " GROUP BY c.priority ORDER BY count DESC", True), params)
                case_counts_by_risk = rollup(
                    scoped("SELECT c.risk_level AS risk_level, COUNT(*) AS count FROM cases c"
                           " GROUP BY c.risk_level ORDER BY count DESC", True), params)
                agent_run_counts = rollup(
                    scoped("SELECT r.agent_id AS agent_id, r.status AS status, COUNT(*) AS count "
                           "FROM agent_runs r JOIN cases c ON c.case_id = r.case_id"
                           " GROUP BY r.agent_id, r.status ORDER BY r.agent_id, r.status", True),
                    params)
                tool_counts = rollup(
                    scoped("SELECT r.tool_name AS tool_name, COUNT(*) AS count, "
                           "SUM(r.exit_code = 0) AS exit_zero "
                           "FROM tool_runs r JOIN cases c ON c.case_id = r.case_id"
                           " GROUP BY r.tool_name ORDER BY count DESC", True), params)
                approval_counts = rollup(
                    scoped("SELECT r.decision AS decision, COUNT(*) AS count "
                           "FROM approvals r JOIN cases c ON c.case_id = r.case_id"
                           " GROUP BY r.decision ORDER BY count DESC", True), params)
                knowledge_counts = rollup(
                    scoped("SELECT r.status AS status, COUNT(*) AS count "
                           "FROM knowledge_records r JOIN cases c ON c.case_id = r.case_id"
                           " GROUP BY r.status ORDER BY count DESC", True), params)
                source_counts = rollup(
                    scoped("SELECT r.association_state AS association_state, COUNT(*) AS count "
                           "FROM case_sources r JOIN cases c ON c.case_id = r.case_id"
                           " GROUP BY r.association_state ORDER BY count DESC", True), params)
                case_day_counts = {r["day"]: r["count"] for r in rollup(
                    scoped("SELECT substr(c.updated_at, 1, 10) AS day, COUNT(*) AS count "
                           "FROM cases c GROUP BY day", True), params)}
                artifacts_count = self.connection.execute(
                    "SELECT COUNT(*) FROM artifacts a JOIN cases c ON c.case_id = a.case_id "
                    + case_filter, params).fetchone()[0]
            else:
                case_counts_by_status = rollup(
                    "SELECT status AS status, COUNT(*) AS count FROM cases "
                    "GROUP BY status ORDER BY count DESC")
                case_counts_by_priority = rollup(
                    "SELECT priority AS priority, COUNT(*) AS count FROM cases "
                    "GROUP BY priority ORDER BY count DESC")
                case_counts_by_risk = rollup(
                    "SELECT risk_level AS risk_level, COUNT(*) AS count FROM cases "
                    "GROUP BY risk_level ORDER BY count DESC")
                agent_run_counts = rollup(
                    "SELECT agent_id AS agent_id, status AS status, COUNT(*) AS count "
                    "FROM agent_runs GROUP BY agent_id, status ORDER BY agent_id, status")
                tool_counts = rollup(
                    "SELECT tool_name AS tool_name, COUNT(*) AS count, "
                    "SUM(exit_code = 0) AS exit_zero FROM tool_runs "
                    "GROUP BY tool_name ORDER BY count DESC")
                approval_counts = rollup(
                    "SELECT decision AS decision, COUNT(*) AS count FROM approvals "
                    "GROUP BY decision ORDER BY count DESC")
                knowledge_counts = rollup(
                    "SELECT status AS status, COUNT(*) AS count FROM knowledge_records "
                    "GROUP BY status ORDER BY count DESC")
                source_counts = rollup(
                    "SELECT association_state AS association_state, COUNT(*) AS count "
                    "FROM case_sources GROUP BY association_state ORDER BY count DESC")
                case_day_counts = {r["day"]: r["count"] for r in rollup(
                    "SELECT substr(updated_at, 1, 10) AS day, COUNT(*) AS count "
                    "FROM cases GROUP BY day")}
                artifacts_count = self.connection.execute(
                    "SELECT COUNT(*) FROM artifacts").fetchone()[0]

            # Activity timeline.  Case-update series comes from cases (already
            # scoped above); event series uses events.workspace when scoped.
            if workspace:
                event_day_counts = {r["day"]: r["count"] for r in rollup(
                    "SELECT substr(timestamp, 1, 10) AS day, COUNT(*) AS count "
                    "FROM events WHERE workspace = ? GROUP BY day", params)}
            else:
                event_day_counts = {r["day"]: r["count"] for r in rollup(
                    "SELECT substr(timestamp, 1, 10) AS day, COUNT(*) AS count "
                    "FROM events GROUP BY day")}
            today = datetime.now(timezone.utc).date()
            activity_timeline: list[dict[str, Any]] = []
            for offset in range(days - 1, -1, -1):
                day = (today - timedelta(days=offset)).isoformat()
                activity_timeline.append({
                    "day": day,
                    "cases_updated": int(case_day_counts.get(day, 0)),
                    "events": int(event_day_counts.get(day, 0)),
                })

            totals = {
                "cases": sum(r["count"] for r in case_counts_by_status),
                "active_cases": sum(
                    r["count"] for r in case_counts_by_status
                    if r["status"] not in ("CLOSED", "ESCALATED")),
                "agent_runs": sum(r["count"] for r in agent_run_counts),
                "tool_runs": sum(r["count"] for r in tool_counts),
                "approvals": sum(r["count"] for r in approval_counts),
                "knowledge_records": sum(r["count"] for r in knowledge_counts),
                "sources": sum(r["count"] for r in source_counts),
                "artifacts": artifacts_count,
            }

            return {
                "generated_at": utc_now(),
                "totals": totals,
                "case_counts_by_status": case_counts_by_status,
                "case_counts_by_priority": case_counts_by_priority,
                "case_counts_by_risk": case_counts_by_risk,
                "agent_run_counts": agent_run_counts,
                "tool_counts": tool_counts,
                "approval_counts": approval_counts,
                "knowledge_counts": knowledge_counts,
                "source_counts": source_counts,
                "activity_timeline": activity_timeline,
            }

    def get_latest_completed_agent_output(
        self, case_id: str, agent_id: str,
    ) -> dict[str, Any] | None:
        """Return the newest valid, completed output for an Agent.

        Mock runs use module paths such as ``agents.diagnosis`` while the
        AgentScope adapter stores the short identity ``diagnosis``.  Both are
        evidence records for the same handoff and must survive a process
        restart.  ``structured_output`` is flattened for callers while the
        raw audit record remains unchanged in ``agent_runs.output_ref``.
        """
        aliases = (agent_id, f"agents.{agent_id}")
        with self.lock:
            rows = self.connection.execute(
                """SELECT run_id, output_ref FROM agent_runs
                   WHERE case_id = ? AND agent_id IN (?, ?) AND status = 'completed'
                   ORDER BY COALESCE(finished_at, started_at) DESC, rowid DESC""",
                (case_id, *aliases),
            ).fetchall()

        for row in rows:
            try:
                output = json.loads(row["output_ref"] or "")
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(output, dict) or output.get("status") not in (None, "completed"):
                continue

            structured = output.get("structured_output")
            if isinstance(structured, dict):
                output = {**output, **structured}
            output["agent_run_id"] = row["run_id"]
            return output
        return None

    # ── DevLoop: knowledge records & retrospective ─────────────────────────

    def record_knowledge_records(
        self, case_id: str, content_ref: str, entries: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Persist extracted knowledge entries as pending-review records.

        Each entry becomes one ``knowledge_records`` row with
        ``status='pending_review'``; ``content_ref`` is
        ``f"{manifest_artifact_id}#{index}"`` pointing into the knowledge
        manifest artifact.  Returns the inserted records.
        """
        records: list[dict[str, Any]] = []
        now = utc_now()
        with self.lock:
            for index, entry in enumerate(entries):
                record_id = f"krec-{uuid.uuid4().hex[:12]}"
                tags = entry.get("tags") or []
                tags_json = json.dumps(tags, ensure_ascii=False) if isinstance(tags, list) else "[]"
                self.connection.execute(
                    """INSERT INTO knowledge_records
                       (record_id, case_id, status, content_ref, reuse_tags, created_at)
                       VALUES (?, ?, 'pending_review', ?, ?, ?)""",
                    (record_id, case_id, f"{content_ref}#{index}", tags_json, now),
                )
                records.append({
                    "record_id": record_id,
                    "case_id": case_id,
                    "status": "pending_review",
                    "content_ref": f"{content_ref}#{index}",
                    "reuse_tags": tags if isinstance(tags, list) else [],
                    "created_at": now,
                })
            self.connection.commit()
        return records

    def list_knowledge_records(
        self, case_id: str | None = None, status: str | None = None,
    ) -> list[dict[str, Any]]:
        """List knowledge records, optionally filtered by case and/or status."""
        query = "SELECT * FROM knowledge_records"
        clauses: list[str] = []
        params: list[Any] = []
        if case_id:
            clauses.append("case_id = ?")
            params.append(case_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC, record_id"
        with self.lock:
            rows = self.connection.execute(query, params).fetchall()
        records = []
        for row in rows:
            record = dict(row)
            try:
                record["reuse_tags"] = json.loads(record["reuse_tags"] or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                record["reuse_tags"] = []
            records.append(record)
        return records

    def get_knowledge_record(self, record_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM knowledge_records WHERE record_id = ?", (record_id,)
            ).fetchone()
        if row is None:
            return None
        record = dict(row)
        try:
            record["reuse_tags"] = json.loads(record["reuse_tags"] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            record["reuse_tags"] = []
        return record

    def review_knowledge_record(
        self, record_id: str, reviewed_by: str, decision: str, note: str = "",
    ) -> dict[str, Any] | None:
        """Review a knowledge record: decision ∈ {'verified', 'rejected'}.

        Verified entries become reusable by later Agents; rejected entries
        stay recorded but excluded from reuse.  Returns the updated record,
        ``None`` for an unknown record, or ``{"error": ...}`` for a bad decision.
        """
        if decision not in ("verified", "rejected"):
            return {"error": f"decision must be 'verified' or 'rejected', got: {decision!r}"}
        now = utc_now()
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM knowledge_records WHERE record_id = ?", (record_id,)
            ).fetchone()
            if row is None:
                return None
            self.connection.execute(
                """UPDATE knowledge_records
                   SET status = ?, reviewed_at = ?, reviewed_by = ?, review_note = ?
                   WHERE record_id = ?""",
                (decision, now, clean_text(reviewed_by, 100), clean_text(note, 500), record_id),
            )
            self.connection.commit()
        record = dict(row)
        record["status"] = decision
        record["reviewed_at"] = now
        record["reviewed_by"] = clean_text(reviewed_by, 100)
        record["review_note"] = clean_text(note, 500)
        try:
            record["reuse_tags"] = json.loads(record["reuse_tags"] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            record["reuse_tags"] = []
        return record

    def get_case_artifact(self, case_id: str, kind: str) -> dict[str, Any] | None:
        """Return the most recent artifact of a given kind for a Case."""
        with self.lock:
            row = self.connection.execute(
                """SELECT * FROM artifacts WHERE case_id = ? AND kind = ?
                   ORDER BY created_at DESC LIMIT 1""",
                (case_id, kind),
            ).fetchone()
        return dict(row) if row else None

    def get_retrospective(self, case_id: str) -> dict[str, Any] | None:
        """Return a Case's retrospective (report + manifest + records).

        Returns None when no retrospective has been generated.  Artifact
        content is read (and hash-verified) via ``read_artifact``.
        """
        report = self.get_case_artifact(case_id, "retrospective_report")
        manifest = self.get_case_artifact(case_id, "knowledge_manifest")
        if report is None and manifest is None:
            return None
        result: dict[str, Any] = {}
        if report is not None:
            report_row = dict(report)
            content = self.read_artifact(report["artifact_id"])
            report_row["content"] = content.decode("utf-8") if content is not None else None
            result["report"] = report_row
        if manifest is not None:
            manifest_row = dict(manifest)
            manifest_row["entries"] = []
            manifest_row["index"] = {}
            content = self.read_artifact(manifest["artifact_id"])
            if content is not None:
                try:
                    parsed = json.loads(content.decode("utf-8"))
                    if isinstance(parsed, dict):
                        manifest_row["entries"] = parsed.get("entries", [])
                        manifest_row["index"] = parsed.get("index", {})
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
            result["manifest"] = manifest_row
        result["knowledge_records"] = self.list_knowledge_records(case_id=case_id)
        return result

    def publish_retrospective(self, case_id: str, retrospective: dict[str, Any]) -> None:
        """Broadcast the generated retrospective over SSE."""
        with self.lock:
            case = self._case_dict(case_id)
        self._publish_case_event(
            "case_retrospective", case, {"retrospective": retrospective},
        )

    def _maybe_trigger_retrospective(self, case_id: str) -> None:
        """Invoke the injected retrospective hook if one is registered."""
        hook = self.retrospective_hook
        if hook is None:
            return
        try:
            hook(case_id)
        except Exception:
            pass

    def retrospective_lock(self, case_id: str) -> threading.Lock:
        """Return the per-Case lock serialising retrospective generation.

        The lock guards the check-then-write in generate_retrospective so two
        concurrent triggers (e.g. ROLLED_BACK then CLOSED, or the async hook
        racing a manual HTTP request) cannot both pass the idempotency guard
        and insert duplicate artifacts / knowledge_records.
        """
        with self.lock:
            lock = self.retrospective_locks.get(case_id)
            if lock is None:
                lock = threading.Lock()
                self.retrospective_locks[case_id] = lock
            return lock

    def _case_dict(self, case_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
        if row is None:
            return None
        case = dict(row)
        case["source_count"] = self.connection.execute(
            "SELECT COUNT(*) FROM case_sources WHERE case_id = ?", (case_id,)).fetchone()[0]
        return case

    def close(self) -> None:
        with self.lock:
            try:
                self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            try:
                self.connection.execute("PRAGMA optimize")
            except Exception:
                pass
            self.connection.close()
            # On Windows, WAL journal files may still need a moment to flush
            if sys.platform == "win32":
                import time as _time
                _time.sleep(0.05)
