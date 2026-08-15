# Code Defog — Deep Review: Agent Runtime & Agents

Reviewed: agent_runtime/ (orchestrator.py, harness.py, state_machine.py, teams_adapter.py, agentteams_preflight.py, case_context.py, review_context.py, envfile.py, identities.yaml) and agents/ (triage, diagnosis, repair, verification, project_review, code_interpreter), plus supporting daemon/store.py, tools/controlled_repair.py, demo_target/quality_gate.py, daemon/server.py, daemon/llm_summary.py, daemon/code_semantics.py, daemon/drive.py.

Line numbers are exact against the current files.

---

## 1. State machine & lifecycle correctness

### 1.1 [HIGH] orchestrator.py:190-211 — LLM-asserted quality gate + LLM patch_ref can drive RELEASE_APPROVAL when no sandbox exists
The authoritative deterministic gate override only fires when context has a truthy sandbox_ref (teams_adapter.py:558-565). Otherwise the Verification Agent's quality_gate_passed is the LLM's own self-assessment — the prompt (teams_adapter.py:638-643) asks the model to "run quality gates", but the Agent's toolkit is empty (teams_adapter.py:253-257), so it cannot actually run anything — and the orchestrator accepts it at line 199. The patch_ref fallback at line 200 (agent_result.get("patch_ref") or case.get("patch_ref")) is likewise the LLM's claimed string from the repair step. RELEASE_APPROVAL then issues a grant anchored on that LLM patch_ref (store.py:1003-1017), and a human approve_release -> RELEASED. The model is the sole judge of its own fix.
- Why it matters: a hallucinated "passed" verdict with a fabricated patch_ref can reach a real release approval. Unreachable in the shipped default (mock mode returns quality_gate_passed: None = unchecked), but live in --runtime-mode agentscope.
- Fix: require a deterministic execution for RELEASE_APPROVAL: only when quality_gate_passed is True AND sandbox_ref was set by a Store-validated repair. Treat a missing sandbox_ref as quality_gate_passed: None (manual handling), never True. Never fall back to case.get("patch_ref") for the release anchor.

### 1.2 [MEDIUM] orchestrator.py:146-155 — transition_case error result is not checked; concurrent transitions feed an error dict into agent context
advance() validates is_valid_transition on a case read at line 137 (lock released), then calls store.transition_case (line 149) which re-reads under the lock (store.py:935-945) and returns {"error": ...} when the state changed in between (server is ThreadingHTTPServer; an approval handler and an intake handler can race). The error dict is not checked, so line 155 calls _build_agent_context(case_id, {"error": ...}) -> CaseContext.from_dict (case_context.py:39-43) raises TypeError (missing required case_id/status) -> unhandled exception reaches the HTTP handler. In a milder race, the agent still dispatches on a transition that failed.
- Fix: after line 149: if isinstance(result, dict) and "error" in result: return result (treat as terminal for this call).

### 1.3 [MEDIUM] orchestrator.py:121-129, 154, 167-177 — failed / no-op agent runs leave cases stuck in active states with no retry, no escalation, and no crash recovery
- A failed TRIAGED/DIAGNOSED/REPAIRING run (harness exception, invalid structured output, empty patch_ref) is not "completed", so no transition occurs and the case stays in the active state. The test comment (tests/test_daemon.py:1205) claims "for retry or escalation", but no code path retries or escalates: run_active_state is called only from the approval endpoint (server.py:1256); there is no timer/sweeper; on_source_received returns the case with no failure marker to the HTTP client (silent failure).
- Repair no-op is easy to hit: mock-mode repair (repair.py:33-41) returns patch_ref: "" with no status -> adapter stamps status="completed" (teams_adapter.py:398) -> orchestrator line 168 sees empty patch_ref and stops, leaving the case at REPAIRING forever.
- Crash mid-dispatch leaves a status='running' agent_runs row (store.py:1111-1125) and a case in TRIAGED/REPAIRING/VERIFYING that nothing resumes after restart. The "survives orchestrator restart" test (test_daemon.py:1103-1203) proves only that persisted handoffs survive, not that in-flight states are recovered.
- Fix: (a) startup sweep of active states with stale/failed runs -> re-dispatch or ESCALATED; (b) consecutive-failure counter -> escalate after N; (c) repair returns status: "failed" (not completed) when no patch was produced, and orchestrator escalates a completed-but-empty repair.

### 1.4 [MEDIUM] orchestrator.py:141-144 + store.py:713-726 — concurrent intake of a second observation for the same incident double-dispatches the same active state
create_or_find_case links a second source with the same incident_signature (different client_nonce, so delivery-id dedup at store.py:674-684 does not trigger) to the same case and returns it without a "duplicate" flag; on_source_received (orchestrator.py:218-228) then calls advance(case_id, "TRIAGED"), and line 141's resume path accepts the self-transition and dispatches triage a second time concurrently: two agent_runs rows, and the loser of the DIAGNOSED race gets a confusing {"error": ...} from the nested advance (lines 183-184).
- Fix: serialize dispatch per case (per-case lock or a claim on agent_runs with status='running'), and make the resume path idempotent.

### 1.5 [MEDIUM] State machine completeness — CLOSED / ROLLED_BACK / reopen transitions are unreachable in production
transition_case (store.py:930) is called only from orchestrator.py:149,194,198,203,206,209. perform_case_action (store.py:825-928) supports only approve/reject/cancel. There is no production action reaching CLOSED, ROLLED_BACK, ESCALATED->REPAIRING, or PATCH_REJECTED->REPAIRING (state_machine.py:44,46-48). Consequences: (a) RETROSPECTIVE_TRIGGER_STATES = {CLOSED, ROLLED_BACK} (store.py:57) never fires through the lifecycle, so the retrospective feature never triggers in production; (b) a rejected patch can never be re-repaired — the repair loop is a dead end rather than a loop (good for non-convergence, but also means no remediation path).
- Fix: add explicit actions (close_case with service token, retry_repair from PATCH_REJECTED) routed through the same grant/validation model, or document those transitions as operator-DB-only.

### 1.6 [LOW] Grant crash-atomicity is good (positive)
perform_case_action (store.py:869-899) consumes the grant, inserts the approval, and updates the case in one implicit transaction committed at line 925 — a mid-function crash rolls all three back. Grant consumption is serialized by self.lock; a double-submitted token gets 401 on second use.

---

## 2. LLM integration

### 2.1 [MEDIUM] teams_adapter.py:446-472 — no timeout, no retry/backoff, asyncio.run in a threaded server
agent.reply_stream(...) is awaited with no asyncio.wait_for: a hung upstream blocks the request thread indefinitely. No retry/backoff exists in the AgentScope dispatch path (llm_summary.py:105-148 has a 30s urllib timeout but also no retry). asyncio.run at line 472 raises RuntimeError if ever invoked from a thread with a running loop (e.g. dispatch called from an async/SSE context) — a latent landmine.
- Fix: wrap the stream iteration in asyncio.wait_for(..., timeout=60); retry transport errors with exponential backoff (2/4/8s) before marking failed; route dispatch through a dedicated executor.

### 2.2 [MEDIUM] teams_adapter.py:96-110, 489-499 — heuristic failure detection can misclassify; _EMPTY_OUTPUT_PATTERNS is dead code
_detect_failure flags any output containing "i cannot"/"i am unable"/"i'm sorry, but" as model_refusal (line 108) — a legitimate diagnostic saying "I cannot reproduce this without more evidence" is marked failed. Event-level checks (EXCEED_MAX_ITERS, REPLY_END error, lines 493-500) are the reliable signals; the text heuristics are noise. _EMPTY_OUTPUT_PATTERNS (lines 90-93) is never used.
- Fix: make event-level detection authoritative, restrict text heuristics to exact refusal markers, delete _EMPTY_OUTPUT_PATTERNS.

### 2.3 [MEDIUM] teams_adapter.py:595-650 — prompt-injection surface: user-controlled source signals embedded verbatim
_build_task_prompt serializes the whole context into ctx_json[:2000] (line 600). context.source_events[].signals carries user-supplied exception_type/message_pattern/keywords from the intake payload (store.py:696-704, orchestrator.py:58-79) — a crafted message_pattern can inject "ignore previous instructions..." directly into the task prompt. Schema validation (teams_adapter.py:166-186) and deterministic overrides for repair/verification (551-565) mitigate, but triage/diagnosis outputs (priority, hypotheses) are only schema-validated.
- Fix: pass source signals through a sanitized, quoted-as-data projection and add a system instruction that context is data, never instructions.

### 2.4 [LOW] Token budget — truncation is character-based; usage recorded but never enforced
ctx_json[:2000], raw_text[:2000]/[:4000], result_summary[:500] are character slices, not token-aware; input/output tokens are captured per MODEL_CALL_END (457-460) but never summed or budgeted; ReActConfig(max_iters=5) (line 259) is the only cap.
- Fix: add a max_tokens/event budget in the stream loop and fail (not retry) when exceeded.

### 2.5 [LOW] JSON robustness is decent (positive), with a nit
_extract_json_block (113-127) handles fenced-json and a bare-object fallback; invalid JSON fails closed to "failed" (537-544). Nit: the greedy bare-object fallback (line 121) can grab prose braces; _validate_structured mutates its input in place for 0/1->bool coercion (178-183) — surprising but harmless.

---

## 3. Boundary enforcement

### 3.1 [HIGH] verification.py:102-104 + quality_gate.py:20-32 — the quality gate executes code at an agent/user-chosen path
repo_ref = context.get("sandbox_ref") or context.get("repository_ref", "") — when sandbox_ref is absent (any non-demo flow, or an AgentScope LLM that returns a sandbox_repository_ref), the fallback is the user-supplied repository_ref from the intake payload (store.py:647, 699), canonicalized but otherwise unvalidated. If that directory contains cli.py, verification executes quality_gate.py <dir>, which runs <dir>/cli.py as a subprocess with cwd inside the directory (quality_gate.py:29-32). _record_gate_evidence additionally reads <dir>/cli.py bytes (verification.py:63-64). In AgentScope mode an LLM-chosen sandbox_repository_ref is persisted verbatim by set_patch_context (orchestrator.py:170-174). Net effect: arbitrary code execution at a path chosen by the model or an authenticated intake caller.
- Why it matters: the design intent (identities.yaml, harness.py:74-77) is that verification only inspects a Store-controlled isolated sandbox; the repository_ref fallback silently widens that to any path containing a cli.py.
- Fix: validate repo_ref is under the Store sandbox root (<state_dir>/sandboxes, cf. controlled_repair.py:117-120) or exactly the demo target before executing; otherwise return quality_gate_passed: None + escalate. Reject repository_ref as a gate target entirely.

### 3.2 [MEDIUM] orchestrator.py:167-177 — LLM-claimed sandbox_repository_ref is persisted without validation
set_patch_context(case_id, patch_ref, agent_result.get("sandbox_repository_ref", "")) trusts whatever string the repair result carries (in AgentScope non-demo mode, the LLM's). This enables 3.1 and lets a case's sandbox_ref be any path string.
- Fix: only persist sandbox_repository_ref when it came from the controlled repair tool (repair_mode == "demo_sandbox"), or verify it inside the Store sandbox root before persisting.

### 3.3 [LOW] "Agent cannot approve itself" holds (positive), with one indirect caveat
Grant tokens are issued only via issue_approval_grant behind require_human_approval (server.py:638-644); the adapter/harness/orchestrator never hold them; grant use is state- and token-checked under the store lock (store.py:837-882). The only indirect leak is finding 1.1: the anchor the human approves (target_ref = LLM-claimed patch_ref) can be model-fabricated.

### 3.4 [LOW] review_context / code_interpreter are properly bounded (positive)
Project review is deterministic and read-only (project_review.py:13-38). code_semantics.py projects the dossier to a strict field allowlist (57-102), validates evidence refs and neighbor node_ids against the dossier (126-164), and includes source text only on explicit opt-in — a good model for other prompts.

---

## 4. teams_adapter.py — honesty, errors, credentials, dead code

### 4.1 [LOW] Honest (positive)
Module docstring (1-31), AgentTeamsAdapter alias comment (680-683), set_mode("production")->agentscope (214-224), describe() runtime_claim (harness.py:165-168), and agentteams_preflight.py (fail-closed require_ready, no network/docker invocation, TCP DOCKER_HOST explicitly rejected at 169-181) are all candid about being local AgentScope/mock, not AgentTeams. Preflight correctly refuses to silently fall back.

### 4.2 [LOW] teams_adapter.py:297 vs 316-379 — inconsistent error envelope
dispatch_task returns {"error": ...} for an unmapped state while review/code-interpreter paths return {"status": "failed"/"error", ...}; the orchestrator only checks status == "completed", so the {"error"} shape works by accident.
- Fix: unify on {"status": "failed", "failure_reason": ...}.

### 4.3 [LOW] Dead code
_EMPTY_OUTPUT_PATTERNS (teams_adapter.py:90-93); AgentEntrypoint Protocol (189-190); export_trace (654-677, never called); next_states (state_machine.py:63-64); orchestrator imports is_terminal/requires_approval (orchestrator.py:19-20) never used.

### 4.4 [LOW] teams_adapter.py:582-592 — blanket except Exception erases diagnostics
The catch-all converts every failure (including programming bugs and DB errors) into failure_reason: "exception: ..." with no traceback retained; output is at least durable via finish_agent_run. Acceptable fail-closed, but log the traceback.

### 4.5 [LOW] envfile.py:29-40 — docstring/behavior mismatch
load_dotenv returns "changed" (set >=1 key) while the docstring promises "True if the file existed and was read". Also no inline-comment or "export " prefix handling.

---

## 5. Concurrency & robustness

- store.py:114,118 — SQLite check_same_thread=False + one shared connection guarded by an RLock; WAL + synchronous=NORMAL. Agent dispatch holds no lock while the LLM runs (begin/finish_agent_run are separate lock-held steps, store.py:1117-1138), so long runs do not block other writes. The orchestrator itself is not locked, which is the source of 1.2/1.4.
- harness.py:200-213 — one bad executor call is caught and converted to status: "failed" (a bad tool call does not crash the case; per 1.3 it fails silently for non-verifying states).
- Non-convergence: the repair loop cannot spin (see 1.5) — PATCH_REJECTED never auto-re-enters REPAIRING; the practical risk is the opposite (dead-end states).
- agents/triage.py:22 and agents/diagnosis.py:17 hardcode confidence: 0.85 — mock output presented as if evidence. Documented as stubs, but any console showing confidence should mark it as stub-derived.

---

## 6. Top 5 most impactful improvements

1. Make the release decision deterministic-only (1.1, 3.2): gate RELEASE_APPROVAL on a real quality-gate run against a Store-validated sandbox; LLM-asserted quality_gate_passed and patch_ref must never drive a transition. Closes the self-approval hole.
2. Validate the quality-gate target path (3.1): run quality_gate.py only against paths under the Store sandbox root or the exact demo target; drop the repository_ref fallback.
3. Add crash recovery + failure escalation for active states (1.3): startup sweep of stale running agent_runs / active cases; escalate after N consecutive failed agent runs instead of leaving cases stuck at TRIAGED/DIAGNOSED/REPAIRING.
4. Harden the orchestrator transition path (1.2, 1.4): check {"error"} results from transition_case before dispatch/context build; serialize dispatch per case so the resume path is idempotent under concurrent intake/approval.
5. LLM-call robustness in the AgentScope adapter (2.1, 2.4): hard timeout + retry/backoff around reply_stream, a token/iteration budget, and token-aware truncation.

Positive notes worth preserving: grant consumption is transactional and lock-serialized; artifact storage is path-escaped and hash-verified (store.py:1207-1284); the tool_runs hash chain (store.py:1140-1205) is a solid audit primitive; the honesty posture of teams_adapter/preflight (fail-closed, no AgentTeams claims) is exactly right.
