# Demo Case 说明书

> Code CCTV DevLoop · GOAI Agent Infra 方向三 · 竞赛材料
> 依据框架 §3.3 扩展。两个演练案例共享 `demo_target/` 仓库（P1 阶段创建，作为 P2–P4 测试目标）。

> **实施状态：** 本文保留两条演练案例与验收目标。真实 AgentTeams 控制面、Team/Task/Handoff 工作流和可导出 Trace 尚未配置或验证；当前本地 Mock/AgentScope 运行不能替代该验收，也不存在可引用的已验证 `evidence/*.json` 运行证据。

## 0. 演示总览

| 案例 | 演示目标 | 状态机终点 | 核心看点 |
|------|---------|-----------|---------|
| **Case A：成功修复** | 端到端闭环到 `CLOSED` | `CLOSED` | 受控工具链 + 审批门禁 + 确定性验证 + 复盘知识沉淀 |
| **Case B：预发布拦截** | 错误补丁被质量门禁拦下 | `PATCH_REJECTED` | 系统**不盲目信任模型输出**，门禁拦截有问题补丁 |

> 两案例的目标是在真实 AgentTeams Runtime 完成可复核的 Team/Task/Trace 验收；该目标目前尚未完成。DeepSeek 仅可用于本地 AgentScope 实验路径，不能作为真实 AgentTeams 运行的证明。

## 1. 共享目标仓库：`demo_target/`

| 文件 | 作用 |
|------|------|
| `cli.py` | 故意有 bug 的 Python CLI（Case A 的 `KeyError` 崩溃 + Case B 的校验可被静默） |
| `quality_gate.py` | 确定性质量门禁脚本（驱动 `PATCH_REJECTED`，含 fsync 保证） |
| `test_config.py` | 隔离场景测试（Case A 基线 + 正确/错误修复判定） |

### `cli.py` 缺陷位置

```python
# Case A: 直接下标访问，缺少 'projects' 键时抛 KeyError
return {
    "projects": config["projects"],          # ← bug
    "required_field": config["required_field"],
}
```

---

## 2. 案例 A：成功修复（Python 边界条件 bug）

### 场景
CLI 处理缺少 `projects` 字段的配置文件时抛未捕获 `KeyError` 崩溃。配置含必填 `required_field`，仅缺 `projects`。

### 三类输入（多源归并演示）

| 来源 | 内容 |
|------|------|
| **Issue** | 配置为 `{"required_field": "enabled"}` 时运行 `--list` 报 `KeyError: 'projects'` |
| **日志** | `ERROR root: config['projects'] → KeyError: 'projects'`，堆栈指向 `cli.py:25` |
| **测试失败** | `test_config.py::test_no_projects_crashes_with_keyerror` 期望返回空列表，实际抛异常 |

### 已知根因
`cli.py:25` 用 `config["projects"]` 直接下标访问，未用 `.get("projects", [])`。

### 预期修复
```python
return {
    "projects": config.get("projects", []),      # ← 修复
    "required_field": config.get("required_field"),
}
```
修复后 `--list` 返回空输出，退出码 0；原有 `validate_config` 不受影响。

### 验证标准
- `test_fix_returns_empty_list_on_no_projects` 通过（exit 0）
- 原有 `validate_config` 不受影响
- 确定性质量门禁 `exit 0`

### 目标闭环路径
```
TRIAGED → DIAGNOSED → PLAN_APPROVAL →[审批]→ REPAIRING
  →(sandbox_copy → apply_case_a_patch，隔离沙箱)
  → VERIFYING →(quality_gate exit 0)→ RELEASE_APPROVAL →[审批]→ RELEASED → CLOSED
  →(异步复盘：复盘报告 + 知识条目 pending_review)
```

---

## 3. 案例 B：预发布测试拦截（修复引入新问题）

### 场景
对 Case A 的"错误修复方案"——修复者用过度宽松的默认值，导致本应报错的非法配置被静默忽略。

### 三类输入（与 Case A 共享前两类）

| 来源 | 内容 |
|------|------|
| **Issue** | 与 Case A 相同 |
| **日志** | 与 Case A 相同 |
| **回归测试失败** | Case A 测试通过，但 `test_config.py::test_wrong_fix_silences_required_field_validation` 失败（期望抛 `ConfigError`，实际被静默忽略） |

### 已知根因（错误修复）
修复过度激进：将 `config["required_field"]` 也改为可选，破坏了必填校验逻辑（`validate_config` 被绕过/静默）。

### 预期行为
验证阶段检测到回归测试失败 → 系统自动置 `PATCH_REJECTED`（预发布拦截，区别于发布后的 `ROLLED_BACK`）→ 回退修复或转人工 → 证据包记录失败原因与决策。

### 验证标准
- 回归测试失败被正确捕获
- 补丁**未被合并**，源仓库 `demo_target/` 零改动
- 证据包记录 `PATCH_REJECTED` 原因 + 后续处置建议

### 目标闭环路径
```
… → REPAIRING → VERIFYING →(quality_gate exit 1，确定性拦截)→ PATCH_REJECTED
```

> **语义区分**：`PATCH_REJECTED` = 预发布质量门禁拦截（补丁有问题，不应合并）；`ROLLED_BACK` = 发布后指标异常触发回滚（已合并但线上不佳）。

---

## 4. 演示剧本（评审演示建议）

1. **输入聚合**：通过 `POST /api/cases` 提交 Issue/日志/测试三种输入 → 展示两级指纹归并到同一 Case。
2. **Agent 编排**：观察 4 个 Agent 的结构化输出与本地 Mock/AgentScope 事件；真实 AgentTeams Trace 在接入后单独验收。
3. **审批门禁**：展示 `PLAN_APPROVAL` / `RELEASE_APPROVAL` 的人工审批：服务令牌加独立人工审批密钥签发一次性 `approval_token`，Agent 不能仅凭服务令牌自审批。
4. **受控修复**：Case A 在隔离沙箱生成补丁，源仓库只读。
5. **门禁拦截**：Case B 错误补丁被 `quality_gate exit 1` 拦下 → `PATCH_REJECTED`。
6. **复盘沉淀**：Case 关闭后自动生成复盘报告 + 知识条目（pending_review），展示人工复核 → verified。
7. **证据审计**：`GET /api/cases/{id}/evidence` 导出完整证据索引（tool_runs 哈希链 + 审批 + 复盘知识状态）。

## 5. 回放可重复性

- 全部依赖本地（SQLite + demo_target + 可配置 DeepSeek key），无生产环境前置。
- 知识提取为确定性纯函数，同一证据输出稳定，演示可重复。
- Case 证据通过 SQLite 和 `GET /api/cases/{id}/evidence` 查询；`evidence/` 下的本地 JSON 生成物不入库，也不能当作当前真实 AgentTeams 运行证据。
- 真实接入完成后，应以官方控制面导出的 Team/Task/Handoff/Trace 和可重放案例作为验收材料，而不是复用本地生成的 JSON 文件。
