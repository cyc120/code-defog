---
name: code-defog-review
description: Review local Git code changes across languages for reproducible bugs with evidence, then generate a shareable Word report. Use when the user asks to audit AI-written code, review a local diff, find missed edge cases, or verify a suspected regression. Do not use for routine implementation without a review request.
---

# Code Defog Review

Review the user's current worktree, starting with the changed code. The outcome is a small set of reproducible findings and clearly labeled unverified risks. This skill works without the Code Defog daemon, an API key, or a cloud model.

## Workflow

1. Confirm the target repository and review scope from the request. Default to staged, unstaged, and untracked changes relative to `HEAD`; use a user-specified base commit when provided. Check `git status` and the actual diff. If the worktree is clean, report that scope and review a committed change only when the user identifies one. Never assume that a passing suite proves the change is correct.
2. Run the bundled `scripts/collect_changes.py` with Python and `--workspace <repo>`; add `--base <ref>` when reviewing against another commit. Resolve the script relative to this `SKILL.md`; the current directory can be any repository or subdirectory. Treat its JSON as a locator, not a bug verdict. Check `status`, `truncated`, `languages`, `review_targets`, `skipped`, and `non_source_files` before claiming coverage. Each target may have `preview_truncated`; the preview contains only a few changed lines per file. Read the actual diff, surrounding code, callers and tests. Inspect deleted files, large or unreadable files, and unrecognized languages directly when relevant. If there is no Git worktree, review only the files the user identified.
3. Select the most consequential plausible behavior boundaries across the changed languages. The collector gives function-level hints only for Python; for other languages, use its changed-line locations and inspect the code manually. Favor inputs or lifecycle sequences that existing tests do not exercise; explain why each proposed check matters.
4. Inspect `check_candidates` and the underlying project manifest before running any command: package scripts can perform arbitrary actions. A candidate has `status: not_run` until it actually runs. When the user requests verification or testing, run relevant build or type checks and reproduce selected risks with focused checks. Put generated fixtures and throwaway code in a temporary directory; preserve the user's worktree. Run existing project tests only when the request authorizes testing. Record the exact command, input, exit status, observed result, and relevant environment. If a check cannot be run, report the reason.
5. Classify each result. **Confirmed** requires an observed failure with a repeatable command and a clear expected behavior. **Unverified** means a plausible concern without reproduction. **Environment issue** means the check could not run reliably. Never turn a model judgment, static hint, or test count alone into a confirmed bug.
6. Give the user a concise decision-ready summary in chat, then create the complete shareable report as a Word `.docx` using `scripts/render_report.py`. Include every confirmed issue, unverified lead, documentation gap, exact `file:line`, expected-versus-actual evidence, checks, and coverage limits. Do not create a parallel Markdown report artifact. Follow `references/reporting.md`; render and visually inspect every DOCX page before delivery. If the user asked for fixes, make the local change and rerun the focused reproducer when testing is authorized. Otherwise keep the review read-only.

## Result format

Follow `references/reporting.md` for the user-facing report. State the reviewed range, what was actually examined, and what remains unreviewed. Keep collector candidates separate from commands actually run. If nothing is confirmed, say so explicitly. Report fixes and checks only when they actually ran.

## Boundaries

- No push, PR, remote upload, or external model call is implied by invoking this skill. Use local tools first; send source code to a cloud model only when the user has authorized that destination and scope.
- Keep a failed or unavailable check visible in the result. Do not silently treat it as a pass.
- Preserve a distinction between a review hint, a reproduced defect, and a validated fix. A fix is validated only by the relevant passing check after the change.
- The bundled collector locates changed source lines for common languages, suggests manifest-backed checks, and locates changed Python functions. It does not parse other languages into functions, prove a review is complete, or execute proposed checks.
