# RhineLabUI 案例 002：循环检查仍使用旧五列布局

- 仓库：<https://github.com/sakura4388/RhineLabUI>
- 审查基线：`6394b3c6a203ad687d535071cecd6bcce3447ff2`
- 状态：本地检出中已修复并验证，尚未提交或推送
- 分类：回归检查失效；不代表页面循环交互已证实失效

## 复现和修复

在目标仓库执行：

```powershell
node --experimental-strip-types scripts/check-loop.mjs
```

基线版本在第 18 行失败，`AssertionError: 1 !== undefined`，退出码 1。原因是脚本仍以五列计算 `columnFiles()`，而当前内容只有一列。

本地修复让检查从 `archiveColumns.length` 读取实际列数，单列时检查 32 个池实例，并继续检验正反方向共 20,000 次相邻循环导航。修复后同一命令退出码 0，输出 `directionalMoves: 20000`、`poolSize: 32`、`checkedPoolInstances: 32`、`checks: passed`。隔离工作树的 `npm run build` 也通过。

此案例说明过时检查会失去回归保护，但不能凭旧检查失败断定页面交互存在缺陷。浏览器拖动线索仍需单独验证。
