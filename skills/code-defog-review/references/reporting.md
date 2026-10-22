# Review result contract

The user should understand the outcome without reading raw scanner JSON or a terminal log. Write in the user's language. Keep the first screen short; attach detailed evidence below it. Do not confuse source files located by the collector with files whose behavior was actually reviewed.

## Shareable report format

The shareable report artifact is a Word `.docx` by default. Generate it from the structured review JSON:

```powershell
python <skill>/scripts/render_report.py <review.json> --output <report.docx>
```

If `--output` is omitted, the script writes a `.docx` beside the JSON. Do not create a parallel Markdown report file. Keep the chat response concise; keep the full evidence in the Word document.

The DOCX includes the repository and revision, conclusion, scope and coverage, commands actually run, and every finding. Present findings in a readable table with severity/category, exact repository-relative locations, expected behavior or user-facing claim, actual implementation/evidence, impact, and suggested action. Include reproduction details when available. Keep unverified leads separate from confirmed issues. Do not truncate the Word report to the five findings summarized in chat.

Render the DOCX and inspect every page before delivery. Check table readability, intact file/line references, missing glyphs, clipping, and page breaks. If rendering is unavailable, say the document was not visually verified.

## Required order

1. **Conclusion:** one sentence with confirmed defect count, unverified lead count, and the practical meaning of the checks. If there are no confirmed defects, say “未确认缺陷” and state the main coverage limit. Never say “无问题” based only on a passing build or suite.
2. **Scope and coverage:** repository, exact base/head or worktree scope, changed language and file counts, which code paths were examined, skipped or truncated files, and material areas not checked. Explain any generated or non-source files excluded from manual review. Avoid a percentage that suggests complete semantic coverage.
3. **Checks actually run:** a compact table with command, status (`通过`, `失败`, `环境受限`, `未运行`), and one-line observation. A collector `check_candidates[].status: not_run` remains a suggestion even if the same command was later run manually; report the later run separately. If an environment failure was retried successfully, show both attempts and the reason for classifying the first failure as environmental. Include exit codes when available.
4. **Findings:** confirmed defects before unverified leads. For each confirmed defect give severity, user impact and trigger, expected versus actual behavior, exact reproducible command/input and observation, file/line, and the smallest practical fix. Separate a broken regression check from a demonstrated product behavior bug. For each unverified lead say what evidence is missing and the next check that would resolve it.
5. **Next actions:** ordered, concrete changes and focused checks. Label proposals as proposals; only call a fix validated when the relevant check passed after the edit. State whether target files were modified and whether anything was pushed.

## Compact template

```markdown
# <Repository> 审查结果

**结论：** 已确认 <N> 个问题，待验证 <N> 个线索。<一句用户可理解的影响>。

**范围：** `<base>..<head>` / 工作区；<语言与文件数>。实际检查了 <路径或行为>；未覆盖 <重要边界>。

| 检查 | 状态 | 结果 |
| --- | --- | --- |
| `<command>` | 通过/失败/环境受限/未运行 | <退出码及关键观察> |

## 已确认问题
### P1/P2/P3 · <行为标题>
影响与触发：...
证据：`<command>`，输入 ...；预期 ...；实际 ...（退出码 ...）。
位置：`<file>:<line>`。
建议：...；修复后用 `<focused check>` 验证。

## 待验证线索
- <位置与担忧>；尚缺 <证据>；下一步 <检查>。

## 下一步
1. <最高优先级修改及验证方式>。

本次 <未修改/已修改...>，<未推送/已推送...>。
```

Severity measures user impact, not confidence. Do not invent a severity or exact line number when the evidence cannot support it. Link to local files in the final answer when possible; the report file itself should keep paths usable from its location. If the full report is long, put the conclusion, check table, and next actions before extended reproduction notes.
