# Deep Review: Code Defog HTTP Server Layer

Scope: `daemon/server.py` (1290 lines), `daemon/serve.py` (208), `daemon/dashboard.py` (109), `daemon/service_discovery.py` (260), plus targeted verification in `store.py`, `llm_providers.py`, `llm_summary.py`, `drive.py`, `project_discovery.py`.

## 1. HTTP / Security

### [HIGH] Service token is served unauthenticated at `GET /ui/config` — DNS-rebinding / Host-header attack chain
**server.py:379-399** (token at line 393), routing at **server.py:471-483**.

`/ui/config` returns `self.server.token` with no authentication of any kind. `cors=False` (line 399) blocks cross-origin *reads*, but does nothing against a classic DNS-rebinding attack: an attacker page loads `http://attacker-controlled-domain:<port>/ui/config`; the browser resolves the domain to `127.0.0.1`, the request is *same-origin* from the browser's perspective, no CORS applies, and the daemon never validates the `Host` header (BaseHTTPRequestHandler does no Host checking). The port is trivially discoverable unauthenticated via `/ui/services` on both this server (server.py:481-483) and the tokenless dashboard (dashboard.py:73-75). Result: any website the user visits can steal the full service token and then drive every authenticated API. The "localhost + random port" defense in the comment at server.py:471-474 is not sufficient against rebinding and does not hold at all if `--host` is changed (see next finding).

**Fix:** (a) Require the human approval key (or a one-time boot token printed to stderr like the approval key) for `/ui/config`; (b) validate the `Host` header in every request and reject non-loopback `Host` values; (c) consider verifying `Origin`/`Sec-Fetch-Site` for the token-returning endpoint.

### [HIGH] `POST /api/projects/{workspace}/drive` accepts any directory — arbitrary file read + test execution + LLM exfiltration
**server.py:667-677** (routing) and **server.py:1046-1102** (handler); the only check is `Path(workspace).is_dir()` at **server.py:1054**. The workspace is NOT required to be a monitored project (contrast `_registered_project()` used by code-graph routes, server.py:928-935, 960-978). `run_drive` then reads the project tree (drive.py:377-391), runs the project's test suite in a subprocess (drive.py:216), and sends browsed content to the configured LLM provider (drive.py:493). Combined with finding 1 (token theft), a local attacker can point the daemon at `/etc`, another user's home directory, or any readable path: the daemon reads those files, executes their test suite in the daemon's security context, and ships file contents to an LLM endpoint.

**Fix:** Require the workspace to be a registered monitored project (`_registered_project()` or `store.get_monitored_project`) before `begin_review_run_if_idle`; add a maximum-tree-size guard; scope `run_drive`'s reads to the resolved project path.

### [MEDIUM] Changing the LLM `base_url` retains the stored API key — key exfiltration to an attacker-controlled host
**server.py:813-826** (`save_llm_provider`) → **llm_providers.py:311-329** (`save_and_activate`). `base_url` may be set to any absolute `https` URL (http restricted to exact loopback names at llm_providers.py:170-171), and when the request supplies no `api_key`, the previously stored key is silently kept (llm_providers.py:322-325). Every subsequent summary/assistant/drive LLM call then sends the real key to the new host. Note the connection-test path already has exactly the right guard — `resolve_candidate` refuses to reuse a stored key against a non-preset host (llm_providers.py:268-309, host check 292-301) — but `save_and_activate` does not.

**Fix:** Mirror `resolve_candidate`'s rule in `save_and_activate`: when the resolved `base_url` host differs from the provider's preset hosts, require an explicitly supplied `api_key` (or refuse the change). This needs only a service token to exploit today.

### [MEDIUM] `Access-Control-Allow-Origin: *` on all JSON responses + permissive preflight
**server.py:344-345** (`send_json` default `cors=True`), **server.py:449-456** (`do_OPTIONS` allows the token/approval headers from any origin). Every authenticated endpoint answers `*`; the preflight explicitly whitelists `X-Code-Defog-Token`, `X-Code-Defog-Token-Type`, and the approval key headers for any origin. This doesn't leak by itself (browser won't send the token cross-origin), but once a token is obtained by any means (finding 1), exfiltration from the victim's browser is trivially CORS-permitted; `/health` (server.py:462-469) already returns its payload to *any* website via CORS.

**Fix:** Return a strict origin (loopback origins only) or no CORS header for authenticated endpoints; restrict preflight to the token header actually used; drop `*` from `/health`.

### [MEDIUM] No enforcement that the daemon binds loopback; `--host` can expose the whole API
**serve.py:67** (`--host` default `127.0.0.1` but free-form), **serve.py:149-156**. The code comments repeatedly claim "the daemon binds localhost" (server.py:471-474, 384-385), but nothing validates it. `discovery_agent.register` is only called for loopback hosts (serve.py:161), yet the server itself will happily bind `0.0.0.0` or a LAN address, making `/ui/config` (token) and every API reachable from the network with zero authentication. `CodeDefogServer` never receives or checks the host.

**Fix:** Reject non-loopback bind addresses in `parse_args`/`main` unless an explicit `--allow-non-loopback` opt-in is given (with a startup warning), and assert loopback in `CodeDefogServer.__init__`.

### [LOW] `read_json_body` echoes raw parser errors and trusts `Content-Length` blindly
**server.py:411-426**. Line 424-425 returns `{"error": str(error)}` with the raw `json`/`Unicode` error text to the client (minor internal-info disclosure). Line 420 reads exactly `length` bytes; a client can declare a large `Content-Length` and stall the request thread indefinitely (no read timeout on the socket), and a body larger than `MAX_BODY_BYTES` is rejected without draining, forcing connection close (acceptable) — but there is no cap on concurrent stalled threads (see resource finding).

**Fix:** Return a generic error message; add a per-request read timeout; read with `read1`/bounded loop.

### [LOW] Grant-consumption path in `handle_case_action` does not require the service token
**server.py:1238-1244**. For `ALL_GRANTED_ACTIONS` the handler checks only the client-controlled `X-Code-Defog-Token-Type: approval` header; real security rests entirely on the one-time `approval_token` validated in `store.perform_case_action` (store.py:837-871 — solid: hash, expiry, one-shot, case/action binding, state-machine check). This is defensible by design, but the endpoint accepts the request with an absent/garbage service token as long as a valid approval token is supplied.

**Fix:** Add `require_service_auth()` in the grant branch too (defense in depth; the approval token remains the true credential).

## 2. Input validation

### [MEDIUM] Unvalidated workspace strings on DELETE and GET project routes
**server.py:707-729** (`do_DELETE` — `unquote` then straight to `store.unregister_monitored_project`, only a non-empty check) and **server.py:1104-1121** (`get_project_drive`/`get_project_reviews` query by arbitrary workspace with no monitored-project check, unlike code-graph routes). Harmless today because these only touch the DB, but inconsistent and one step away from file access; a typo'd or hostile path reaches the store layer as a key.

**Fix:** Route all `{workspace}` handlers through one normalized/validated lookup (`_registered_project`) with a shared path-decoding helper.

### [MEDIUM] Unbounded `limit` and unbounded thread-per-connection
**server.py:789-793**: `limit` from the query string is `int()`-parsed with no clamp — negative/`-1` means unlimited in SQLite, and large values return the entire table. **server.py:44-46 + 749-770**: `ThreadingHTTPServer` spawns an unbounded thread per connection, and each SSE client pins one thread for its lifetime; a local process can exhaust threads by opening many SSE connections.

**Fix:** Clamp `limit` to 1..200; cap concurrent connections (bounded `ThreadingMixIn`/custom `_socketserver` limit or a semaphore around `process_request`).

### [LOW] `case_id`/`record_id` extracted by string split with no validation
**server.py:532-538, 617-644** — `route.split("/")[3]` is fine for well-formed paths (empty ids → 404), but ids are never validated against a charset; harmless given parameterized SQL.

## 3. Thread safety

### [MEDIUM] `summary_cache` is a plain dict mutated across request threads without a lock
**server.py:78** (declaration), **server.py:135-141** (`clear_llm_caches` calls `self.summary_cache.clear()`), **server.py:904-905** (`project_summary` passes it to `get_llm_summary`), **llm_summary.py:331-342** (`cache.get(...)`, `cache.update({ts_key: now, llm_key: result})`). Concurrent `project_summary` calls (e.g., dashboard + SSE-triggered refreshes) racing with a `POST /api/llm/providers` → `clear_llm_caches` can raise `RuntimeError: dictionary changed size during iteration` or produce torn reads, 500ing `project_summary`. Every other cache in the server is properly locked (`assistant_cache_lock`, `code_graph_cache_lock`, `code_semantic_cache_lock`); this one was missed.

**Fix:** Guard `summary_cache` with a `Lock` (or reuse the pattern from `assistant_cache`), and make `clear_llm_caches` take the same lock.

### [LOW] `publish()` can raise `queue.Full` uncaught under concurrent publishers
**server.py:231-242**. In the `except queue.Full` branch, `subscriber.get_nowait()` frees one slot then `subscriber.put_nowait(message)` runs again — if two publisher threads drain/refill the same 8-slot queue concurrently, the second `put_nowait` can still hit `Full`, and that exception is not caught. Direct callers of `server.publish` (server.py:557, 571, 746, 825, 1181) have no `try/except`, so a mid-response exception kills the request thread. (The store's `_publish_case_event` is protected — store.py:628-632 — but the server's own calls are not.)

**Fix:** Catch `queue.Full` around the whole per-subscriber put and drop the message (slow subscriber policy), or protect each subscriber queue with its own lock.

### [LOW] `store.publish_callback` runs while the store lock may be held
**store.py:621-632** — `_publish_case_event` is invoked from inside locked store methods; it calls `server.publish` which only takes `subscriber_lock` (no reverse acquisition anywhere), so there is no deadlock today, but any future blocking in the callback would stall all DB operations.

**Fix:** Document the lock ordering (store lock → subscriber lock) and keep `publish` non-blocking (it is).

## 4. SSE implementation

### [LOW] SSE: no `retry:` field / event ids, but heartbeat and cleanup are correct
**server.py:749-770**. Good: heartbeat comment at line 763-764, `unsubscribe` in `finally` at line 769-770 (no subscriber leak), bounded queue (maxsize=8), drop-oldest publish policy, auth required (`/api/stream` sits behind `require_service_auth`, server.py:485-528). Gaps: no `retry:` hint (defaults vary by browser, usually fine) and no event `id:`/`Last-Event-ID` support, so reconnections re-read full state and can replay events. Also the loop blocks the request thread for the connection's lifetime (ties into the unbounded-thread finding).

**Fix:** Emit `retry: 1000` and an incrementing `id:` per event; document/accept the thread-per-SSE model or move SSE onto a shared reader (e.g., a single fan-out thread per server).

## 5. Resource management

### [LOW] Unbounded daemon-thread spawn per Case close, errors silently swallowed
**serve.py:135-137** — a new `threading.Thread` per retrospective, unbounded; `_run_retrospective` swallows every exception with bare `pass` (serve.py:39-41), so failures are invisible.

**Fix:** A bounded worker pool (or `ThreadPoolExecutor(max_workers=N)`), and log the exception before swallowing.

### [LOW] Shutdown ordering: store closed while spawned threads may still run
**serve.py:196-204** — `server.server_close()`/`store.close()` run while drive/retrospective threads may still be mid-flight (they are daemon threads); a late `store` touch can raise on a closed connection.

**Fix:** Track spawned threads and join them (bounded) before closing the store, or guard store methods against a closed connection.

## 6. Error handling

### [MEDIUM] Unhandled exceptions in many handlers → connection reset / empty responses instead of JSON 500s
Store calls can raise `sqlite3.Error`; handlers like `list_cases` (server.py:781-794), `get_case` (1123-1128), `project_summary` stats (888-903), `list_monitored_projects` (923-926), and `projects_discover` (913-921) have no `try/except`. An unhandled exception propagates to `BaseHTTPRequestHandler.handle_one_request` → the connection closes with no response; the client sees a reset/empty reply with no status code (misleading), and only a stderr traceback remains (`log_message` is suppressed, server.py:309-310).

**Fix:** Wrap the `do_GET`/`do_POST`/`do_DELETE` dispatch bodies (or override `handle_one_request`) with a catch-all that emits a `500 {"error": "internal error"}` JSON and logs the traceback.

### [LOW] `get_harness` masks the exception entirely
**server.py:802-806** — acceptable, but the real error is lost; log it.

## 7. Code quality

### [MEDIUM] 1290-line monolith with duplicated route parsing and duplicated endpoint logic
**server.py** as a whole. The `/api/projects/{workspace}/...` prefix block is copy-pasted 6× with inline `from urllib.parse import unquote` (server.py:506, 512, 518, 670, 683, 696; also `parse_qs` inlined at 785 and 898), each with a bespoke `route[len(...):-len(...)]` slice. `serve_ui`, `send_json`, `/ui/config`, `/ui/services`, `/health` are duplicated between server.py and dashboard.py (dashboard.py:38-79). `import getpass` appears both at module top (server.py:6) and inline (server.py:386).

**Fix:** Extract a `RouteTable`/dispatch dictionary mapping `(method, pattern) → handler` with a shared `workspace` parameter extractor; move the UI/dashboard endpoints into a small shared mixin; hoist imports to module level.

### [LOW] Type hints are loose throughout
`Any` is used for `orchestrator`, `harness`, `llm_summary_fn`, `llm_chat_fn`, `code_interpreter_fn`, `store` (server.py:48-61, store.py signatures). The JSON payloads (`dict[str, Any]`) would benefit from `TypedDict`s, and `publish_callback`/subscriber message shapes from a protocol type.

### [LOW] Dead/compat code
`CodeCCTVServer = CodeDefogServer` (server.py:1287-1290) is intentional; `TOKEN_TYPE_SERVICE` (server.py:35) is referenced. `summary_cache` typing (server.py:78) is a plain `dict[str, Any]` — tighten once locked.

## Positives worth preserving (verified)

- Approval-key flow is sound: the approval secret never appears in `/ui/config` or the service descriptor (server.py:70-73); grant issuance requires the human approval key (server.py:639-644); `perform_case_action` validates token hash, expiry, one-shot, case/action binding, and state machine (store.py:837-871).
- `hmac.compare_digest` used for both tokens (server.py:326, 333).
- LLM secrets are well handled: keys stored 0600 outside the auditable DB (llm_providers.py:89-94, 126-149), redacted in `public_config` (llm_providers.py:254-262), never echoed by the test endpoint (llm_summary.py:645-667), TLS verification via certifi context (llm_summary.py:140-146).
- `base_url` validation blocks credential/query/fragment URLs and restricts `http` to exact loopback names (llm_providers.py:165-171) — solid SSRF posture; service discovery additionally verifies loopback hosts and probes/validates instance ids before showing a service (service_discovery.py:92-116, 171-192).
- Atomic file writes everywhere (`write_json`, descriptors, provider state) via `mkstemp`+`fsync`+`os.replace`; caches are bounded and evict oldest; SSE cleanup is correct.

---

## Top 5 most impactful improvements

1. **Kill the token-theft chain (HIGH).** Authenticate `GET /ui/config` (require the approval key or a boot-time token), validate the `Host` header on every request, and enforce loopback-only binding in `serve.py`/`CodeDefogServer`. Today: one malicious website visit → service token → full API (server.py:379-399, 471-483; serve.py:67).
2. **Require monitored-project registration for `POST /api/projects/{workspace}/drive` (HIGH).** This closes arbitrary-directory read, subprocess test execution, and LLM exfiltration by a token holder (server.py:1046-1102, 1054).
3. **Block silent API-key repointing in `save_and_activate` (MEDIUM).** Require an explicit `api_key` when `base_url`'s host leaves the provider's preset hosts — the guard already exists for connection tests; extend it to saves (llm_providers.py:268-309 vs 311-329).
4. **Lock `summary_cache` and add a dispatch-level exception guard (MEDIUM).** Eliminates the concurrent-dict race in `project_summary` (server.py:904-905, 135-141) and turns silent connection resets into proper 500 JSON responses.
5. **Bound the request surface (MEDIUM).** Clamp `limit` in `list_cases`, add socket read timeouts and a concurrent-connection cap, catch the `publish()` `queue.Full` race, and add SSE `retry:`/event ids (server.py:231-242, 789-793, 749-770).
