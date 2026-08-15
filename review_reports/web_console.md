# Code Defog web/index.html — Deep Review Report

**File reviewed:** `/Users/caicai/code-defog/web/index.html` — 3,356-line single-file management console (CSS L7–771, HTML L772–1197, JS L1198–3354).

**Method:** Full read of all 3,356 lines plus grep verification of every sink (`innerHTML`, `location.*`, `localStorage`, timers, `AbortController`, `fetch`, prompts/dialogs, selector interpolation).

**Overall impression:** The code is notably more disciplined than most single-file consoles — *all* 26 `innerHTML` assignments are container clears only (`target.innerHTML = ""`); dynamic content consistently goes through a `create(tag, class, text)` helper that sets `textContent`, so the classic "innerHTML + API data" XSS class is **absent**. There is no `eval`/`new Function`/`document.write`. Epoch guards (`dataEpoch`, `connectionEpoch`, `streamEpoch`) and an AbortController for the assistant/SSE are in place. The issues below are the real remaining gaps.

---

## 1. Security

### [HIGH] Service token persisted in plaintext `localStorage`
**Location:** L1687 (`cc-conn` read), L1705 (`localStorage.setItem("cc-conn", JSON.stringify(config))`); token attached to every request at L1326.
**Issue:** The connection config — including the `X-Code-Defog-Token` service token — is written verbatim into `localStorage`. The same file explicitly avoids this for LLM keys ("密钥不进入 localStorage 或界面状态", L1490), so this is an acknowledged anti-pattern applied to the *more* sensitive credential. Any same-origin script, browser extension, or the XSS chain described below can read it.
**Why it matters:** `localStorage` is origin-scoped, unencrypted, and readable by any JS in the console origin. Combined with the discovery-URL finding below, a single script execution event = full token theft. A tampered `cc-conn` entry also lets an attacker redirect `baseUrl()` (L1324) so that every subsequent `api()` call ships the token to an attacker-controlled `host:port`.
**Fix:** Keep the token in memory only (the `config` object), or `sessionStorage` at most; persist only `host`/`port`/`user`. Consider offering a "remember token" opt-in, and clear on page unload. Validate `host` against loopback when `served=false`.

### [HIGH] Unvalidated `location.assign(service.ui_url)` — `javascript:` URL execution
**Location:** L1627 (`location.assign(service.ui_url)`), selection logic L1603–1605, discovery/cache fallback L1590–1597 and L1632–1650.
**Issue:** Discovered services come from `/ui/services` (or the `localStorage` cache when offline/`file:`), and any entry with `status: ready|legacy` becomes a clickable button whose click does `location.assign(ui_url)`. Only `isCurrentService` parses the URL (to compare origins); nothing validates the *scheme*. `location.assign("javascript:…")` executes the payload in the console origin.
**Why it matters:** Service-discovery registries can include other local processes (the feature is explicitly "本机服务发现 Agent"). A poisoned or stale cache entry (`javascript:` or `data:` URL) → script execution in the console origin → reads the `cc-conn` token (see previous finding) → full compromise. This is the highest-impact chain in the file.
**Fix:** Before making the button selectable, parse with `new URL(ui_url)` and require `protocol === "http:" || "https:"` **and** a loopback hostname when not same-origin; otherwise render disabled. Prefer a real `<a href>` with `target="_blank" rel="noopener"` over `location.assign`.

### [HIGH] Approval gate silently bypassed when `<dialog>` unsupported
**Location:** L2077 (`else executeApproval(approve, pendingApproval.approver)`).
**Issue:** `openApproval` falls back to `executeApproval(...)` **without the human approval key** if `dialog.showModal` is not a function.
**Why it matters:** The whole control-loop design ("一次性审批授权…无法重复使用", L2070) depends on the human key being presented. On any browser without `<dialog>` (or where the API is shadowed), approvals/rejections would be issued unauthenticated. A security control that silently degrades is worse than one that fails closed.
**Fix:** Fall back to the existing `window.prompt` flow (or render an inline modal) that still collects and sends `humanApprovalKey`; never call `executeApproval` with an empty key. Fail closed with a toast if no dialog can be shown.

### [MEDIUM] CSS-selector injection via server-controlled `status` in the state machine
**Location:** L1863 `document.querySelector(`[data-node="${status}"]`)` (also L1873); `status` originates from case data (keys at L1209–1222, but arbitrary strings pass through `statusMeta`).
**Issue:** Case `status` is interpolated directly into a selector. A status containing `"` or `]` (e.g. `RECEIVED"] , [data-node="x`) produces a selector that `querySelector` rejects with `SyntaxError`, which propagates out of `renderStateMachine` → aborts `renderCase` mid-render, leaving a half-updated DOM and breaking the case view until reload. Not script execution, but a cheap DoS of the primary view.
**Why it matters:** Status values are ultimately persisted by the backend/store; a malformed or hostile status bricks the audit console's rendering. Also any future refactor of this pattern toward `innerHTML` would turn it into real XSS.
**Fix:** Whitelist: `const states = Object.keys(STATUS_META); if (!states.includes(status)) { … }`, or compare via `node.dataset.node === status` instead of a string-built selector.

### [LOW] Approval key lingers in the DOM; `pendingApproval` survives Escape/backdrop close
**Location:** L2074 (cleared on open), L2080–2087 (confirm), L2079 (cancel); dialog markup L1195.
**Issue:** After `dialog-confirm` closes the dialog, `#dialog-approval-key` is **not** cleared (it is only cleared the *next* time the dialog opens). Pressing Escape or clicking the backdrop closes the native `<dialog>` without running the cancel handler, leaving `pendingApproval` set until the next `openApproval` overwrites it.
**Why it matters:** The one-time approval key remains readable in the DOM between dialogs; a stale `pendingApproval` could theoretically be consumed by a later stray confirm click (low probability, but the state should be explicitly torn down).
**Fix:** Clear `$("dialog-approval-key").value` in the confirm path and register a `close` event listener on the dialog that nulls `pendingApproval`.

---

## 2. API Integration

### [MEDIUM] Same-epoch race: SSE-triggered refresh can overwrite a just-selected case's evidence
**Location:** `loadEvidence` L2133–2145, `case-select` change handler L2410–2414, SSE handler L2697–2698.
**Issue:** `dataEpoch` only changes on connect/project switch. Within the same project, two `loadEvidence` calls with different `selectedId` but the same epoch can interleave: the user picks case B while a refresh (from a `case_*` SSE event) is loading case A; if A's response lands last, `evidence` is A while `selectedId` is B — the whole case panel shows the wrong case with no error.
**Why it matters:** In a busy workbench (SSE events every few seconds), this is a realistic stale-state display, and approval decisions render against the wrong case's `base_commit`/`patch_ref` — a correctness issue in a control-flow UI.
**Fix:** Add a per-selection request token: `let evidenceSeq = 0;` increment in the select handler and in `renderCase` entry; `loadEvidence` captures its seq at start and bails unless `seq === evidenceSeq`. Same guard for `loadCases`/`loadProjects` fetches triggered by different user actions.

### [MEDIUM] No AbortController / in-flight guard for LLM-heavy requests; refresh buttons not disabled
**Location:** `loadCodeMapDossier` L2335–2356, `loadProjectSummary` L3062–3077 (+ button L3078), `refresh-btn` L2415, `overview-refresh-btn` L3078.
**Issue:** The assistant (L1429) and SSE stream (L2439) use `AbortController`, but the code-interpret LLM call, the LLM summary refresh, and the LLM test/save calls do not. Refresh buttons stay enabled during a fetch, so rapid clicks fire duplicate concurrent requests (the epoch guard prevents stale *writes*, not duplicate *work*).
**Why it matters:** Interpret/summary are the slowest endpoints; user switches node/project mid-interpret and the request keeps running server-side (response is dropped client-side by the L2350 guard — good — but cannot be cancelled). Duplicate summary fetches re-trigger LLM generation server-side on every click.
**Fix:** Give `loadCodeMapDossier` and `loadProjectSummary` their own AbortControllers (abort on project switch / node change), add an `inFlight` flag that disables the triggering button, and abort in `selectProject`/`resetCodeMap`.

### [LOW] `loadProjectSummary` swallows errors silently and can throw → spurious "offline"
**Location:** L3062–3077; `renderOverviewStats` L3080–3096 (specifically L3095 `summaryLlm.summary.progress_by_phase`).
**Issue:** The catch only calls `setOnline(false)` — no toast, no error message in the overview, so a failed summary looks like a dead service. Worse, if the server returns `llm` with `status:"ok"` but no `summary` object, L3095 throws `TypeError` inside the try block → caught → `setOnline(false)` even though the service is fine (and every render in that call is discarded).
**Why it matters:** Misleading connectivity status is the primary "is my workbench alive" signal; silent failures hide the actual error.
**Fix:** Guard with `summaryLlm?.summary?.progress_by_phase`, surface `error.message` into the LLM card's empty state, and only flip `setOnline(false)` for actual network errors (the `api()` wrapper already throws a distinct "无法连接本地服务" for those).

---

## 3. State Management

### [MEDIUM] Full re-render cascade on every SSE event
**Location:** `handleEvent` L2684–2699 → `refreshAll` L2401–2409 → `refreshActiveView` L2387–2400 → `loadCases`/`loadEvidence` → `renderCase` L1799–1848 (which re-renders state machine, 4 agent cards, tool pipeline, hash chain, tabs **and** the evidence table); `renderSelector` L1738–1753 rebuilds up to 500 `<option>` elements; `updateMetrics` L1732–1737 re-filters 500 cases ×3.
**Issue:** Every `case_*`/`knowledge_reviewed` SSE event (debounced 300 ms) triggers a full project list fetch **plus** a full teardown/rebuild of the entire case view. `review_task_status` events (L2686–2694) additionally re-render the whole review run synchronously *per event*.
**Why it matters:** DOM churn → visible jank/flicker during active operation, `select` scroll position lost, table re-built while the user is reading it, and N fetches per burst. For a monitoring dashboard this is the dominant performance cost.
**Fix:** (a) Debounce/batch SSE events into one refresh per ~1 s; (b) make `renderCase`/`renderEvidence` do targeted updates (set textContent on existing nodes, diff the select by id, only re-render the tab panel actually visible); (c) stop re-fetching `loadProjects` on every event — cache for 10–30 s.

### [LOW] Global mutable state + duplicated DOM-ownership paths
**Location:** L1237–1272 (≈37 module-level `let`s: `config`, `cases`, `evidence`, `reviewRun`, `summaryLlm`, `codeGraph`, …) and mixed update patterns (`innerHTML=""`+append vs `replaceChildren()`, direct textContent updates vs full rebuilds).
**Issue:** Several globals are written from multiple flows (`summaryLlm` from `loadProjectSummary` and `renderDriveRun`; `reviewRun` from `renderReviewRun` and `onDriveStatus`), making the "current truth" ambiguous and the same DOM node updated from different paths.
**Why it matters:** Cross-view coupling (`renderOverviewStats` reads `summaryLlm` set by the drive flow) produces order-dependent renders; hard to test, hard to reason about.
**Fix:** Consolidate into a small typed state object with one render dispatcher per view; or at minimum document each global's writers/readers and move view-specific state (`codeMap*`, `review*`) into per-view closure objects.

### [LOW] `runtime-phase` / `runtime-phase-label` markup is never updated by JS
**Location:** L877 (static "Mock / 45%"), referenced nowhere in JS.
**Issue:** Dead/stale markup — the "运行模式" capability bar shows Mock even when `config.runtime_mode === "production"` (which `runtimeLabel()` already distinguishes elsewhere).
**Fix:** Update `#runtime-phase`/`#runtime-phase-label` inside `setOnline()` or remove the phase row if it's aspirational.

---

## 4. UX / Accessibility

### [MEDIUM] Approval dialog closes without acting when Enter is pressed
**Location:** L1195 `<form method="dialog">`, confirm button L2080 (`type="button"`).
**Issue:** Pressing Enter inside `#dialog-approval-key` submits the form with `method="dialog"` → the dialog closes **without** running the confirm handler, silently discarding the typed key. The primary path for keyboard users is broken (mouse click works).
**Why it matters:** Keyboard accessibility bug on the highest-stakes control (approvals). Users may believe the approval was submitted.
**Fix:** Make the confirm button `type="submit"` and handle the form `submit` event with the same logic, or add a `keydown`/form-submit handler that calls the confirm path.

### [MEDIUM] Native `confirm`/`prompt` for destructive and secret-bearing actions
**Location:** L2103 (`window.confirm` for escalate), L2111 (`window.prompt` for the knowledge-review approval key).
**Issue:** Blocking native dialogs: unstylable, break keyboard/focus flow, don't integrate with the app's theme, and `prompt` returns untrimmed input with no validation or live feedback.
**Why it matters:** Inconsistent with the (otherwise well-built) `<dialog>` approval flow; the prompt-based key entry is a worse UX for the same security-sensitive operation.
**Fix:** Reuse the approval `<dialog>` (generalize it to take an action descriptor) for both escalate confirmation and knowledge review.

### [MEDIUM] `--ink-faint` used for 9–10 px labels fails AA contrast
**Location:** CSS L121–126 (`--ink-faint: #878d85` light / `#8b938a` dark) applied to `.eyebrow`, `.field-label`, `.section-note`, `.agent-label`, `.phase-meta`, `.metric-label`, etc.
**Issue:** `#878d85` on `#fff` ≈ **3.4:1** — below WCAG AA 4.5:1 for the small (9–11 px) text it's used on, pervasively.
**Fix:** Darken to ≈ `#6b7268` (light theme) / lighten `#aab2a8` (dark theme), or bump these labels to 12 px + 4.5:1.

### [LOW] `.state-badge` white text on orange/green/rose fails AA
**Location:** CSS L219–220 (`color:#fff`), `STATUS_META` colors L1209–1222; `is-dark-text` only for 3 statuses (L1824).
**Issue:** White on `--orange #d96833` ≈ 3.5:1, `--rose #c66a42` ≈ 3.8:1, `--green #16885c` ≈ 4.4:1 (DIAGNOSED, REPAIRING, RELEASED, CLOSED, ROLLED_BACK, ESCALATED) — all below 4.5:1 at 10 px.
**Fix:** Extend the `is-dark-text` list to those statuses, or darken the badge fills.

### [LOW] Full-screen overlays lack dialog semantics and focus trapping
**Location:** `#connect-screen` L775–794, `#projects-screen` L1115–1143 (plain `<div>` overlays); assistant/LLM drawers L1145–1192 (have `role="dialog"`/`aria-modal` but no focus trap).
**Issue:** When the connect/project screens are shown, background content stays in the tab order and the user's focus is not moved into the overlay; drawers likewise let Tab escape to the page behind.
**Fix:** Add `role="dialog"` + `aria-modal="true"` to the two screens, move focus to the first control on show, return focus on close, and add a minimal focus trap (keydown Tab handler) for all four overlays.

### [LOW] Evidence tabs lack `tablist`/`tab` semantics and arrow-key navigation
**Location:** `renderEvidenceTabs` L1956–1967 (buttons with `aria-selected` only).
**Fix:** Add `role="tablist"`/`role="tab"` + `aria-controls`, and Left/Right arrow handling.

### [LOW] Hardcoded Chinese UI strings (i18n)
**Location:** Throughout — `STATUS_META` labels, `TABS`, `AGENTS`, `TOOL_LABELS`, every `toast(...)`, `empty-data` message, `aria-label`, static markup; `lang="zh-CN"` hardcoded at L2. ~150+ user-facing strings.
**Fix:** If multilingual support is a goal, extract to a strings map keyed by a single locale constant (low cost, keeps single-file constraint); otherwise document that zh-CN is intentional.

---

## 5. Performance

### [MEDIUM] Code-map filter re-renders the entire SVG on every keystroke
**Location:** L2384 `addEventListener("input", renderCodeMap)`; `renderCodeMap` L2193–2260 rebuilds all nodes/edges/labels from scratch.
**Issue:** No debounce. For a truncated-but-large graph (hundreds–thousands of nodes), every keystroke re-runs layout math (`Math.ceil(Math.sqrt(n*1.5))`), rebuilds the SVG DOM, and re-attaches per-node listeners.
**Why it matters:** Typing "srv_" into the filter is visibly janky on real repos; wasted layout/GC on every character.
**Fix:** Debounce (~150 ms) the input handler and/or toggle `hidden`/`display` on filtered node groups instead of rebuilding the SVG.

### [MEDIUM] Unbounded re-render of large lists on each refresh
**Location:** `renderSelector` L1738–1753 (rebuilds up to 500 `<option>`s every refresh), `renderEvidence`/`table` L1968–1995 (full table rebuild per case load), `renderReviewTasks`/`renderReviewHistory` full rebuilds.
**Why it matters:** Combined with SSE-driven refresh cadence (see §3), every event re-allocates hundreds of DOM nodes; on low-end machines this is the main jank source.
**Fix:** Reuse option elements (only add/remove diffs), cap `limit=500` → paginate or "load more", and keep the currently-visible evidence tab mounted with incremental updates.

### [LOW] `updateMetrics` triple-filter over the case array on every refresh
**Location:** L1732–1737. Fine at 500, but it runs per refresh — compute counts once alongside `loadCases` and store them.

---

## 6. Code Quality

### [LOW] 3,356-line monolith with duplicated logic
**Location:** Whole file; concrete duplicates:
- Test-result ternary duplicated 3× — L2710–2712 (`driveTestItem`), L2752 (KPI row), L2574 (`renderReviewFindings`) — with *slightly different* branch order (a real bug source: L2752 checks `runner_unavailable` *after* `execution_error`, L2710 checks it inside; inconsistent labels).
- `renderTools` L1923–1940 vs `renderChain` L1941–1954 near-identical sorted iteration.
- `AGENTS` fallback (L1225–1230) vs `harnessInfo.tasks` mapping (L1880–1889).
- Status→CSS-var mapping duplicated (`statusCssVar` L3056–3060 vs direct `style.background` in `renderCase` L1823).
**Fix:** Extract `testStatusLabel(test)` helper; merge tools/chain renderers; single `statusColor(status)` helper.

### [LOW] No error boundary / partial-render fragility
**Location:** `renderCase` L1799–1848 (7 sub-renders, any throw leaves partial DOM), `renderStateMachine` selector injection (see §1).
**Fix:** Wrap each sub-render in try/catch with a visible per-section error placeholder, so one bad datum degrades one section instead of the page.

### [LOW] External CDN dependency without SRI
**Location:** L1197 `https://unpkg.com/lucide@0.468.0/dist/umd/lucide.min.js`.
**Why it matters:** Supply-chain risk (a compromised pin executes in the console origin alongside the stored token) + offline breakage (icons silently missing, fallback text is present so layout survives).
**Fix:** Vendor the file locally or add `integrity="sha384-…"` + `crossorigin="anonymous"`.

---

## Positives worth preserving
- No `innerHTML` with untrusted data; all rendering via `textContent` helper.
- Epoch-based stale-response guards on all data loads; AbortController on assistant + SSE.
- SSE with retry/backoff instead of polling; debounced refresh timer; `stopSubscription` on reconnect.
- `aria-live` on toast/discovery/assistant thread; `aria-label` on all icon-only buttons; `role="img"`+labels on SVG charts; focus-visible outlines; `type="password"` + `autocomplete` hygiene on secret inputs; LLM key never persisted.
- Good empty/loading/error states (`empty-data`, offline banner, disable-while-running on drive button).

---

## Top 5 Most Impactful Improvements

1. **Kill the token-theft chain (Security):** validate discovery `ui_url` scheme (http/https + loopback) before `location.assign`, and stop persisting the service token in `localStorage` (`cc-conn`). Together these close the highest-severity exploit path in the file (L1627 + L1705).

2. **Fix approval correctness & keyboard flow:** make the dialog's Enter key submit-and-approve (form `submit` handler), clear the key after submit, tear down `pendingApproval` on close, and never fall back to an unauthenticated `executeApproval` when `<dialog>` is unavailable (L2077–2087).

3. **Eliminate stale-state races:** add per-selection request tokens for `loadEvidence`/`loadCases` (and abort LLM interpret/summary on navigation) so SSE-triggered refreshes can never overwrite a newly selected case (L2133–2145, L2410–2414).

4. **Stop the render cascade:** batch/debounce SSE events, diff the case `<select>` and the active evidence table instead of rebuilding all ~500 options and 7 sub-views per event, and debounce the code-map filter (L2384, L2684–2699).

5. **Accessibility pass:** fix Enter-in-dialog, replace `confirm`/`prompt` with in-app dialogs, fix `--ink-faint`/state-badge contrast, and add focus management to the four overlays — the concrete, user-visible gaps that remain despite an otherwise careful a11y baseline.
