# RhineLabUI 案例 001：栏目数据未校验导致导出失败

- 仓库：https://github.com/sakura4388/RhineLabUI
- 审查提交：`6185da2..6394b3c6a203ad687d535071cecd6bcce3447ff2`
- 缺陷提交：`a76eed6311c4d344894f23acbc2647b7186855dc`
- 状态：已复现；本地检出中已修复并验证，尚未提交或推送
- 范围：`scripts/archive-content.mjs` 的 `validateContent()`、`archiveText()`；`src/main.ts` 的栏目渲染

## 预期与实际

编辑 `content/archives.json` 时，新增的 `sections` 栏目如果缺少 `items`，内容校验应给出具体的输入错误，并阻止导出。实际情况是校验通过；随后 `archiveText()` 调用 `section.items.map()` 时抛出 `TypeError`。把 `sections` 设置为空数组也会通过校验，并生成没有栏目正文的 TXT。

## 复现

需要 Node.js 22.12 或更新版本。保持目标仓库原样，在 Code Defog 仓库执行：

```powershell
node cases/rhinelabui-001/reproduce.mjs ../RhineLabUI
```

脚本读取并复制目标仓库的现有数据，仅在内存中删除一条栏目的 `items`，不改动目标文件。缺陷版本输出 `validation: accepted` 和 `TypeError`，以非零状态退出。修复版本返回明确的 `records[0].sections[0].items` 校验错误，不再进入导出，退出码为 0。

## 证据与边界

2026-09-25 在 Windows、Node.js v24.19.0、提交 `6394b3c` 上运行了最小场景：缺少 `items` 时校验通过，导出抛出 `Cannot read properties of undefined (reading 'map')`；空 `sections` 时校验通过，导出正文为空。当前仓库自带的八份数据均有三个栏目，因此正常构建不一定触发此缺陷。

本地修复后，`validateContent()` 对可选的 `sections` 检查恰好三个栏目，以及每个栏目非空的 `title`、`en`、`items`。同一复现脚本输出具体字段错误并以 0 退出；`npm run check:content` 的 26 项通过，其中新增了缺失、空栏目、错误类型等边界。隔离工作树的 `npm run build` 通过。首次沙箱构建受目录读取权限影响失败，重跑成功；该失败不属于代码缺陷。

本例已有修复前失败和修复后通过的本地证据。修复仍为未提交的本地改动，案例暂不绑定修复提交哈希。
