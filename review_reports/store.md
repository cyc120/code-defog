# Deep Review: daemon/store.py (StateStore) + daemon/paths.py

Scope read fully; cross-checked state_machine.py, repo_identity.py, server.py, retrospective/retrospective.py, test usage.

## 1. SQL correctness, schema & migrations
- [HIGH] store.py:114-156 — FOREIGN KEY constraints declared but never enforced (no PRAGMA foreign_keys=ON). Orphaned rows can be created; clear_all hand-maintains delete order to compensate. Fix: PRAGMA foreign_keys=ON after connect (empirically verified: PRAGMA returns 0; orphan inserts accepted).
- [MEDIUM] store.py:385-453 — migrations are ad-hoc column probes, no PRAGMA user_version tracking; DevLoop ALTERs run outside one transaction. Fix: single BEGIN IMMEDIATE/COMMIT + user_version.
- [MEDIUM] store.py:500-506 — prune keeps newest rows per session, but projects.event_count never decremented → inflated stats. Fix: decrement or derive from events.
- [LOW] store.py:543 — f-string DELETE with hardcoded tuple; safe but fragile.

## 2. Concurrency / transactions
- [HIGH] store.py:758 — resolve_base_commit() git subprocess (2s timeout) runs inside the global self.lock; can stall every store op. Fix: resolve before lock.
- [HIGH] store.py:1235-1260, 1264-1284 — artifact fsync + read-back hash verification under global lock; large blobs block all DB ops. Fix: hoist file I/O out of the lock.
- [MEDIUM-LOW] store.py:925-928,967-971,770-772,1080 — SSE publish invoked while holding lock (latent stall; currently fast put_nowait).
- [LOW] store.py:114 — no PRAGMA busy_timeout; single shared connection (sound otherwise).
- [LOW] store.py:166,2059-2072 — retrospective_locks dict grows without bound (per-case lock never evicted).

## 3. Performance
- [HIGH] store.py:810-823 + 2074-2081 — list_cases N+1: up to 2×limit+1 queries per call. Fix: batch fetch + one GROUP BY for source_count.
- [HIGH] store.py:281-288,329-337,370-381 — missing indexes on hot paths: case_sources(case_id), artifacts(case_id, kind, created_at), knowledge_records(case_id, status), cases(repository_ref), cases(status, updated_at).
- [MEDIUM] store.py:574-592 + 547-572 — state()/state_locked() on every ingest runs a global ROW_NUMBER() window over all events + JSON-decodes files_json. Fix: restrict to active sessions.
- [MEDIUM] store.py:500-506 — prune is a full-table window sort every 50 ingests. Fix: per-session indexed DELETE.
- [MEDIUM] store.py:1658-1662 — _review_run_dict N+1 in list_review_runs.
- [LOW] store.py:467,597 — client-supplied timestamp unvalidated (breaks ordering).

## 4. Security
- [MEDIUM] store.py:1169-1200 — hash chain canonical payload omits command_template/policy_version/approval_id/result_ref (highest-value tamper targets); exit_code None vs -1 mismatch between canonical and row. Fix: include all stored columns, normalize exit_code.
- [MEDIUM] store.py:326,1140-1205 — chain is write-only; no verifier exists; terminal hash not anchored on cases. Fix: verify_tool_chain() + anchor on cases.
- [LOW] store.py:100-101,1024-1025 — unsalted SHA-256 for approval tokens (OK for 256-bit random today); token_hash surfaced in get_case_evidence — stop returning it.
- [LOW] store.py:1207-1284 — artifact path handling solid; nits: artifacts dir umask, WAL sidecar perms.
- [LOW] workspace paths resolved but unconstrained (acceptable for single-user threat model).

## 5. Robustness
- [MEDIUM] store.py:1038-1082 — resolve_pending_sources creates duplicate Cases per signature (no dedupe); promoted cases never set repo_abs_path/base_commit → invisible to project_summary scoping.
- [MEDIUM] store.py:1117 — run_id uses 8 hex chars (32-bit; birthday collision ~50% at 77k runs). Fix: 12+ hex.
- [MEDIUM] store.py:1436 — finish_drive_run stores NULL for empty dicts (falsy check) vs finish_review_run is not None. Fix: is not None.
- [LOW] store.py:1558 — int(order) can raise ValueError; duplicate task_key raises IntegrityError.
- [LOW] store.py:1683 — int(limit) unguarded; list_cases has no limit cap.
- [LOW] store.py:1445 — _drive_run_dict JSON-decodes without try/except (use _json_field).
- [LOW] clean_text collapses newlines (stack traces lose formatting); clean_files double-cleans.
- [LOW] action→expected mapping duplicated at 857-862 vs 1001-1006 — hoist to module constant.

## 6. Dead code / quality
- utc_now_unix unused; sqlite3.OperationalError fallback in recent_events_by_session dead on modern SQLite; missing type hints on two helpers; clear_all omits monitored_projects (intentional but undocumented).

## Top 5
1. PRAGMA foreign_keys=ON + missing indexes (biggest correctness + hot-path win).
2. Kill list_cases N+1.
3. Move blocking work out of the global lock (git subprocess, artifact I/O, SSE publish).
4. Make the tool-run hash chain honest (include omitted columns, normalize exit_code, add verifier + anchor on cases).
5. Fix retention drift + unbounded growth (event_count, per-session prune, retention for audit tables).
