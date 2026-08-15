# Code Defog Daemon — Deep Review (drive/monitor/discovery/graph/LLM)

Scope: daemon/drive.py, project_monitor.py, project_discovery.py, repo_identity.py, code_graph.py, code_semantics.py, llm_summary.py, llm_providers.py, paths.py + helpers (watch_worklog.py, scan_code_map.py, envfile.py).

## Verified-good
- No shell injection in subprocess calls (list argv, shell=False everywhere).
- TLS + key handling in llm_providers.py: 0600/0700, fsync, atomic replace; http restricted to loopback; stored-key reuse gated to preset hosts; public_config strips keys.
- TLS verification via certifi in llm_summary._post_chat; error strings never include Authorization.
- Graph path traversal guarded (resolve + containment check, followlinks=False).

## drive.py
- [HIGH] drive.py:73,80 + llm_summary.py:582 + drive.py:647 — git remote credentials (https://user:token@) leak into persisted reports, LLM prompt, and UI. Same in project_discovery.py:52 and repo_identity.py:77 (canonical_ref). Fix: strip URL userinfo at collection points.
- [HIGH] drive.py:104,125,391 — three full-tree walks per drive, unbounded; whole-file reads (read_text then slice). scan_code_map.iter_files does unbounded rglob + global sort. Fix: one bounded walk; read first N bytes; drop global sort.
- [HIGH] drive.py:216-222 — test probe timeout kills only direct child; grandchildren survive (pytest servers, npm lifecycle). Fix: start_new_session=True + killpg on timeout. Note: running project tests can genuinely mutate the workspace.
- [MEDIUM] drive.py:655-663 — run_id can be unbound in exception handler → NameError masks original failure.
- [MEDIUM] drive.py:539 — blocking harness.dispatch_review with no timeout/cancellation; drive can hang forever; no cancellation path.
- [MEDIUM] drive.py:90-95 — Path.resolve() raises RuntimeError on symlink loops (uncaught).
- [LOW] drive.py:207 — run_test_probe uses unresolved Path(workspace).
- [LOW] drive.py:196-200 — _safe_read reads whole file then slices.

## project_monitor.py
- [HIGH] project_monitor.py:120-140 + watch_worklog.py:116-134 — full-tree re-walk every 5s per watched project with per-file resolve() and no file cap; dominant long-run CPU cost. Fix: OS watcher (watchdog/FSEvents/inotify) or directory-mtime skip + cap + larger interval.
- [MEDIUM] project_monitor.py:73-91 — stop→start race within poll_interval silently kills the watcher (thread.is_alive() still true; old thread exits later).
- [MEDIUM] project_monitor.py:232-237,256-271 — advancing base_commit on git errors can silently skip commits; two git spawns per poll.
- [LOW] project_monitor.py:104 — state_file computed but never used (dead code); _save_scan_state stores only {count, stamp}, so every restart re-baselines.
- [LOW] project_monitor.py:162-173 — _emit swallows exceptions with no logging (invisible event loss).

## project_discovery.py
- [MEDIUM] project_discovery.py:127-134 — comment says nested repos are skipped, but code doesn't prune dirnames → git submodules/vendored repos consume slots and spawn git probes.
- [MEDIUM] project_discovery.py:49-57,138-148 — up to 600 sequential git spawns per discovery (3 per candidate × 200, 2s timeout each) → up to ~20 min worst case. Fix: ThreadPoolExecutor + batched git calls.
- [MEDIUM] project_discovery.py:199-201,206-219 — substring keyword matching ("go" matches google-chrome) → spurious lsof calls; cap and use exact basename match; prefer /proc/<pid>/cwd on Linux.
- [LOW] Windows tasklist CSV parsing fragile (use csv module).

## repo_identity.py
- [HIGH] repo_identity.py:77 — credentials embedded in git_remote flow into canonical_ref (incident dedup identity, persisted). Fix: strip userinfo.
- [MEDIUM] repo_identity.py:26-27 — unbounded module-level TTL caches (never evict). Fix: LRU with maxsize.
- [LOW] repo_identity.py:69 — uncaught RuntimeError from resolve() on symlink loops.

## code_graph.py
- [MEDIUM-HIGH] code_graph.py:236,258 — full source text of up to 160 files retained in memory (up to ~128MB); text only needed for content_hash/line_end. Fix: incremental hash, drop text from stored tuple.
- [MEDIUM] code_graph.py:83-101 — _iter_source_files walks entire tree for a 160-file cap, with per-file resolve(). Fix: per-directory budget, use walked path.
- [LOW] code_graph.py:74-80 — TOCTOU between stat() and read; read bounded chunk instead.

## code_semantics.py
- [LOW] imports private helpers (_extract_json, _post_chat...) from llm_summary — extract shared llm_transport module.
- [LOW] redundant socket.timeout in exception tuple; no cancellation for 30s LLM call.

## llm_summary.py
- [MEDIUM] llm_summary.py:118-148 — no max_tokens (unbounded response read fully) and no retry/backoff on 429/5xx.
- [MEDIUM] llm_summary.py:315-343 — cache has no lock/single-flight; concurrent refreshes duplicate paid LLM calls.
- [LOW] _read_worklog_context reads whole file then truncates; _extract_json last-resort slice lossy (acceptable fallback).

## llm_providers.py
- [LOW] resolve_candidate TOCTOU between two lock acquisitions (concurrent save_and_activate could change base_url).
- [LOW] _read_state trusts stored model/base_url lengths (cap on read).
- Otherwise solid (atomic writes, fsync, 0600/0700, http loopback-only, host gating, key-free public views).

## paths.py — no significant findings.

## Top 5
1. Strip credentials from git remotes everywhere (repo_identity.py:77, drive.py:73/80, project_discovery.py:52) — one redaction helper.
2. Replace full-tree polling with change-watcher / cheap skip (project_monitor.py + watch_worklog.py).
3. Bound memory in browse/graph paths (single bounded walk, size-limited reads, incremental hashing).
4. Make test probe process-safe (process-group kill on timeout) + timeout around harness.dispatch_review.
5. Fix discovery scaling + watcher lifecycle (prune descent, parallelize git probes, fix stop→start race) + bound repo_identity caches.
