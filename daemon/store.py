#!/usr/bin/env python3
"""SQLite-backed project summaries for the Code CCTV service."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


DEFAULT_RETENTION = 2000
DEFAULT_CONVERSATION_ID = "default"
PRUNE_EVERY_INGESTS = 50


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clean_text(value: Any, limit: int = 1200) -> str:
    if value is None:
        return ""
    text = " ".join(str(value).split())
    return text[:limit]


def clean_files(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [clean_text(item, 500) for item in value if clean_text(item, 500)]


class StateStore:
    def __init__(self, path: Path, retention: int = DEFAULT_RETENTION) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.retention = max(retention, 100)
        self._ingests_since_prune = 0
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
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
        self.migrate_schema()
        self.connection.commit()

    def migrate_schema(self) -> None:
        """Upgrade the original workspace-only schema without losing local history.

        All destructive steps run inside one transaction: a crash mid-migration
        rolls back to the untouched schema so the next start retries cleanly
        instead of stranding rows in ``projects_legacy``.
        """
        event_columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(events)").fetchall()
        }
        project_columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(projects)").fetchall()
        }
        needs_event_column = "conversation_id" not in event_columns
        needs_project_rebuild = "conversation_id" not in project_columns
        needs_name_column = not needs_project_rebuild and "conversation_name" not in project_columns

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
                        """
                        CREATE TABLE projects (
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
                        )
                        """
                    )
                    self.connection.execute(
                        """
                        INSERT INTO projects (
                            workspace, conversation_id, conversation_name, name,
                            status, phase, focus, note, evidence, event_type,
                            updated_at, event_count
                        )
                        SELECT workspace, 'default', '', name, status, phase, focus,
                               note, evidence, event_type, updated_at, event_count
                        FROM projects_legacy
                        """
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
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS events_session_timestamp "
            "ON events(workspace, conversation_id, timestamp DESC)"
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS projects_updated "
            "ON projects(updated_at DESC)"
        )

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
                """
                INSERT INTO events (
                    id, workspace, conversation_id, event_type, source, timestamp, phase,
                    status, focus, note, evidence, files_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    workspace,
                    conversation,
                    event_type,
                    source,
                    timestamp,
                    phase,
                    status,
                    focus,
                    note,
                    evidence,
                    json.dumps(files, ensure_ascii=False),
                ),
            )
            self.connection.execute(
                """
                INSERT INTO projects (
                    workspace, conversation_id, conversation_name, name,
                    status, phase, focus, note, evidence,
                    event_type, updated_at, event_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(workspace, conversation_id) DO UPDATE SET
                    conversation_name=CASE
                        WHEN excluded.conversation_name <> '' THEN excluded.conversation_name
                        ELSE projects.conversation_name
                    END,
                    name=excluded.name,
                    status=excluded.status,
                    phase=excluded.phase,
                    focus=excluded.focus,
                    note=excluded.note,
                    evidence=excluded.evidence,
                    event_type=excluded.event_type,
                    updated_at=excluded.updated_at,
                    event_count=projects.event_count + 1
                """,
                (
                    workspace,
                    conversation,
                    conversation_title,
                    name,
                    status,
                    phase,
                    focus,
                    note,
                    evidence,
                    event_type,
                    timestamp,
                ),
            )
            self._ingests_since_prune += 1
            if self._ingests_since_prune >= PRUNE_EVERY_INGESTS:
                self._ingests_since_prune = 0
                # Prune per conversation so a busy session cannot evict the
                # history of idle ones. Each session keeps its newest
                # `retention` events.
                self.connection.execute(
                    """
                    DELETE FROM events
                    WHERE id IN (
                        SELECT id FROM (
                            SELECT id, ROW_NUMBER() OVER (
                                PARTITION BY workspace, conversation_id
                                ORDER BY timestamp DESC
                            ) AS event_rank
                            FROM events
                        )
                        WHERE event_rank > ?
                    )
                    """,
                    (self.retention,),
                )
            self.connection.commit()
            return self.state_locked()

    def state(self) -> dict[str, Any]:
        with self.lock:
            return self.state_locked()

    def info(self) -> dict[str, Any]:
        """Management stats about the local state store."""
        with self.lock:
            sessions = self.connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
            events = self.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        try:
            db_bytes = self.path.stat().st_size
        except OSError:
            db_bytes = 0
        return {
            "retention": self.retention,
            "state_path": str(self.path),
            "total_sessions": sessions,
            "total_events": events,
            "db_bytes": db_bytes,
        }

    def delete_session(self, workspace: str, conversation_id: str) -> dict[str, Any]:
        """Remove one monitored session and all of its events."""
        with self.lock:
            self.connection.execute(
                "DELETE FROM events WHERE workspace = ? AND conversation_id = ?",
                (workspace, conversation_id),
            )
            self.connection.execute(
                "DELETE FROM projects WHERE workspace = ? AND conversation_id = ?",
                (workspace, conversation_id),
            )
            self.connection.commit()
            return self.state_locked()

    def clear_all(self) -> dict[str, Any]:
        """Remove every monitored session and event."""
        with self.lock:
            self.connection.execute("DELETE FROM events")
            self.connection.execute("DELETE FROM projects")
            self.connection.commit()
            return self.state_locked()

    def state_locked(self) -> dict[str, Any]:
        rows = self.connection.execute(
            """
            SELECT workspace, name, status, phase, focus, note, evidence,
                   conversation_id, conversation_name, event_type, updated_at, event_count
            FROM projects
            ORDER BY updated_at DESC, conversation_id
            """
        ).fetchall()
        now = datetime.now(timezone.utc)
        recent_events = self.recent_events_by_session(rows)
        projects: list[dict[str, Any]] = []
        for row in rows:
            project = dict(row)
            project["active"] = self.is_active(row["updated_at"], now) or self.is_watching(row["status"])
            project["recent_events"] = [
                self.event_dict(event)
                for event in recent_events.get((row["workspace"], row["conversation_id"]), [])
            ]
            projects.append(project)

        active = sum(1 for project in projects if project["active"])
        blocked = sum(1 for project in projects if "阻塞" in project["status"] or "blocked" in project["status"].lower())
        return {
            "generated_at": utc_now(),
            "summary": {
                "total_projects": len(projects),
                "active_projects": active,
                "blocked_projects": blocked,
                "event_count": sum(project["event_count"] for project in projects),
            },
            "projects": projects,
        }

    def recent_events_by_session(
        self,
        rows: list[sqlite3.Row],
    ) -> dict[tuple[str, str], list[sqlite3.Row]]:
        """Load the 8 newest events per conversation in one pass instead of N+1."""
        event_query = """
            SELECT id, workspace, conversation_id, event_type, source, timestamp,
                   phase, status, focus, note, evidence, files_json
            FROM (
                SELECT events.*, ROW_NUMBER() OVER (
                    PARTITION BY workspace, conversation_id
                    ORDER BY timestamp DESC
                ) AS event_rank
                FROM events
            )
            WHERE event_rank <= 8
        """
        try:
            event_rows = self.connection.execute(event_query).fetchall()
        except sqlite3.OperationalError:
            # Older SQLite builds without window functions: fall back to one
            # bounded query per conversation (same result, slightly slower).
            event_rows = []
            for row in rows:
                event_rows.extend(
                    self.connection.execute(
                        """
                        SELECT id, workspace, conversation_id, event_type, source,
                               timestamp, phase, status, focus, note, evidence, files_json
                        FROM events
                        WHERE workspace = ? AND conversation_id = ?
                        ORDER BY timestamp DESC
                        LIMIT 8
                        """,
                        (row["workspace"], row["conversation_id"]),
                    ).fetchall()
                )
        grouped: dict[tuple[str, str], list[sqlite3.Row]] = {}
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
        lowered = status.casefold()
        return "监听" in status or "watch" in lowered or "running" in lowered

    @staticmethod
    def event_dict(row: sqlite3.Row) -> dict[str, Any]:
        try:
            files = json.loads(row["files_json"])
        except json.JSONDecodeError:
            files = []
        return {
            "id": row["id"],
            "conversation_id": row["conversation_id"],
            "event_type": row["event_type"],
            "source": row["source"],
            "timestamp": row["timestamp"],
            "phase": row["phase"],
            "status": row["status"],
            "focus": row["focus"],
            "note": row["note"],
            "evidence": row["evidence"],
            "files": files if isinstance(files, list) else [],
        }

    def close(self) -> None:
        with self.lock:
            self.connection.close()
