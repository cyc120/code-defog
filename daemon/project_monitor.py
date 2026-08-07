"""Per-project monitoring service.

For each user-selected project (registered in ``monitored_projects``), a daemon
thread watches the working tree with the ``watch_worklog`` snapshot/diff engine
and POSTs ``/api/events`` (workspace = project path) when files change, plus a
git-log incremental watcher for new commits.

Design constraints:
- File-change detection reuses ``scripts/watch_worklog.snapshot``/``diff_snapshots``
  (pure, importable).
- Events flow through ``scripts/event_client.post_event`` → ``POST /api/events``
  → ``store.ingest`` (workspace already normalizes to an absolute path).
- A per-project lock + in-flight flag prevents watcher races; permission errors
  mark the project ``error`` and the loop keeps running.
- This milestone monitors only (file changes + git commits).  It does not mutate
  real projects — that is a later milestone.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from . import paths

try:
    from scripts.watch_worklog import snapshot as _snapshot, diff_snapshots as _diff
    from scripts.watch_worklog import Change as _Change
except Exception:  # pragma: no cover - scripts not importable in some contexts
    _snapshot = None  # type: ignore[assignment]
    _diff = None  # type: ignore[assignment]
    _Change = None  # type: ignore[assignment]


def _default_post_event(payload: dict[str, Any]) -> bool:
    from scripts.event_client import post_event

    return post_event(payload, timeout=0.6)


class ProjectMonitor:
    """Start/stop per-project watcher threads."""

    def __init__(
        self,
        store: Any,
        post_event: Callable[[dict[str, Any]], bool] | None = None,
        poll_interval: float = 5.0,
        git_poll_interval: float = 30.0,
    ) -> None:
        self.store = store
        self.post_event = post_event or _default_post_event
        self.poll_interval = max(1.0, poll_interval)
        self.git_poll_interval = max(5.0, git_poll_interval)
        self._threads: dict[str, threading.Thread] = {}
        self._stop_events: dict[str, threading.Event] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._state_dir = paths.monitor_state_dir()

    # ── Lifecycle ─────────────────────────────────────────────────────

    def start(self) -> None:
        """Start watchers for every currently-registered project."""
        for project in self.store.list_monitored_projects():
            self.start_project(project["workspace"])

    def start_project(self, workspace: str) -> None:
        if workspace in self._threads and self._threads[workspace].is_alive():
            return
        stop = threading.Event()
        lock = threading.Lock()
        self._stop_events[workspace] = stop
        self._locks[workspace] = lock
        thread = threading.Thread(
            target=self._watcher_loop, args=(workspace, stop, lock), daemon=True)
        self._threads[workspace] = thread
        thread.start()

    def stop_project(self, workspace: str) -> None:
        stop = self._stop_events.pop(workspace, None)
        if stop:
            stop.set()
        thread = self._threads.pop(workspace, None)
        if thread:
            thread.join(timeout=2.0)

    def stop(self) -> None:
        for workspace in list(self._threads):
            self.stop_project(workspace)

    # ── Watcher loop ──────────────────────────────────────────────────

    def _watcher_loop(self, workspace: str, stop: threading.Event, lock: threading.Lock) -> None:
        project = self.store.get_monitored_project(workspace)
        if project is None:
            return
        abs_path = Path(workspace)
        state_file = self._state_dir / f"sha256-{uuid.uuid4().hex[:16]}.json"
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self.store.update_monitored_project_status(workspace, "watching")

        try:
            baseline = self._snapshot_abs(abs_path)
            self._save_scan_state(workspace, baseline)
            previous = baseline
        except OSError as exc:
            self.store.update_monitored_project_status(workspace, "error", str(exc))
            return

        last_git_check = 0.0
        while not stop.is_set():
            time.sleep(self.poll_interval)
            if stop.is_set():
                break
            if not lock.acquire(blocking=False):
                continue  # previous poll still running; skip
            try:
                try:
                    current = self._snapshot_abs(abs_path)
                except OSError as exc:
                    self.store.update_monitored_project_status(workspace, "error", str(exc))
                    continue
                changes = _diff(previous, current) if _diff else []
                if changes:
                    self._emit_file_changes(project, changes)
                    self._save_scan_state(workspace, current)
                    previous = current
                self.store.update_monitored_project_status(workspace, "watching")
            finally:
                lock.release()

            now = time.monotonic()
            if now - last_git_check >= self.git_poll_interval:
                last_git_check = now
                self._emit_git_commits(workspace, project)

    def _snapshot_abs(self, abs_path: Path) -> dict[str, dict[str, int]]:
        """Snapshot wrapper that tolerates a missing watch_worklog import."""
        if _snapshot is None:
            return {}
        return _snapshot(abs_path, ".code-cctv-monitor", Path("/dev/null"))

    def _save_scan_state(self, workspace: str, state: dict[str, dict[str, int]]) -> None:
        try:
            self.store.set_monitored_project_scan_state(
                workspace, json.dumps({"count": len(state), "stamp": time.time()}, ensure_ascii=False))
        except Exception:
            pass

    # ── Emit ──────────────────────────────────────────────────────────

    def _emit(self, payload: dict[str, Any]) -> bool:
        """Deliver an event: store.ingest by default, or a captured post_event
        when one is injected (tests).  Writing directly to the store avoids the
        HTTP-loopback config-path mismatch that ``event_client`` would hit when
        the daemon runs with a non-default ``--config``."""
        if self.post_event is not None and self.post_event is not _default_post_event:
            return bool(self.post_event(payload))
        try:
            self.store.ingest(payload)
            return True
        except Exception:
            return False

    def _emit_file_changes(self, project: dict[str, Any], changes: list[Any]) -> None:
        summary = f"检测到 {len(changes)} 个文件变化"
        paths_list = [c.path for c in changes[:40]]
        self._emit({
            "workspace": project["workspace"],
            "workspace_name": project["name"],
            "conversation_id": "monitor",
            "event_type": "file_change",
            "source": "project_monitor",
            "phase": "监控",
            "status": "监听中",
            "focus": f"文件变化 {len(changes)} 个",
            "note": summary,
            "evidence": ", ".join(paths_list)[:2000],
            "files": paths_list,
        })

    def _emit_git_commits(self, workspace: str, project: dict[str, Any]) -> None:
        """Emit ONLY NEW commits since the project's last seen HEAD.

        Uses ``git log <last_seen>..HEAD`` (or the full recent log on first
        run) so the same commit is never reported twice.  After emitting, the
        project's *base_commit* is advanced to the current HEAD so the next
        poll is a true increment.
        """
        import subprocess

        abs_path = Path(workspace)
        last_seen = (project.get("base_commit") or "").strip()
        try:
            if last_seen:
                proc = subprocess.run(
                    ["git", "-C", str(abs_path), "log", "-n", "20", "--format=%h %ad %an %s",
                     "--date=short", f"{last_seen}..HEAD"],
                    capture_output=True, text=True, timeout=5.0, check=False)
            else:
                proc = subprocess.run(
                    ["git", "-C", str(abs_path), "log", "-n", "20", "--format=%h %ad %an %s",
                     "--date=short", "HEAD"],
                    capture_output=True, text=True, timeout=5.0, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return
        if proc.returncode != 0 or not proc.stdout.strip():
            # No new commits (or git unavailable) — record nothing and advance
            # last_seen so we don't re-scan forever.
            if last_seen:
                self._advance_head(workspace)
            return
        lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()][:20]
        # Advance last_seen so the same commits are never re-emitted.
        self._advance_head(workspace)
        self._emit({
            "workspace": workspace,
            "workspace_name": project["name"],
            "conversation_id": "monitor",
            "event_type": "git_commit",
            "source": "project_monitor",
            "phase": "监控",
            "status": "监听中",
            "focus": f"git 提交 {len(lines)} 条",
            "note": "新提交：\n" + "\n".join(lines),
            "evidence": "\n".join(lines)[:2000],
            "files": [],
        })

    def _advance_head(self, workspace: str) -> None:
        """Record the current git HEAD as the project's base_commit."""
        import subprocess

        try:
            proc = subprocess.run(
                ["git", "-C", str(workspace), "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=5.0, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return
        head = proc.stdout.strip() if proc.returncode == 0 else ""
        if head:
            try:
                self.store.set_monitored_project_base_commit(workspace, head)
            except Exception:
                pass
