# Agent Identity 清单

> Code CCTV DevLoop · GOAI Agent Infra 方向三 · 竞赛材料
> 依据框架 §5 扩展，来源：`agent_runtime/identities.yaml`（代码权威定义）+ 当前本地执行契约。

> **实施状态：** 当前公开运行时由本地 `DevLoopHarness` 统筹任务图，再经 `AgentScopeExecutionAdapter` 执行 Mock/AgentScope 实验；真实 AgentTeams Worker、Team/Task/Handoff 与官方 Trace 尚未配置或验证。本文的 AgentTeams 协同要求是赛事目标，不是已部署事实。

## 1. 角色边界总览

| Agent | 职责 | 可读取 | 可执行 | 明确禁止 | 审批边界 |
|-------|------|--------|--------|---------|---------|
| **分诊证据** Triage | 聚合多源输入、去重、分类、提取复现条件、构建证据索引 | Issue、日志、反馈、CI 失败摘要 | 写入标准化 Case、标注优先级和置信度 | 不下根因结论、不改代码 | 不持有审批凭证 |
| **诊断影响** Diagnosis | 检索代码、历史提交和测试，建立根因假设与影响范围 | Case 证据、只读代码仓、只读 Git 历史 | 生成诊断报告、提出修复策略与风险级别 | 不写工作树、不合并代码、不发布 | 不持有审批凭证 |
| **修复执行** Repair | 在隔离沙箱生成最小补丁和测试补充 | 获批准的修复计划、受限仓库写权限 | 创建分支/补丁、运行受限格式化与单测 | 直接写主分支、读取无关密钥、执行部署 | **不持有审批令牌** |
| **验证发布** Verification | 验证补丁、检查质量门禁、执行模拟灰度并建议放行或回滚 | 补丁、测试结果、模拟指标、发布策略 | 执行测试与部署模拟、生成验证报告 | 未批准的真实生产发布；忽略失败门禁 | **不持有审批令牌** |

## 2. 编排基础设施（不属于 Agent）

| 职责 | 实现位置 | 说明 |
|------|---------|------|
| Case 创建与任务拆解 | `agent_runtime/orchestrator.py` | 接收规范化事件，创建 Case，按状态机推进 |
| 状态机维护与失败转移 | `agent_runtime/state_machine.py` | 12 状态转移表、超时和升级 |
| Harness 任务图与派发 | `agent_runtime/harness.py` 中的 `DevLoopHarness` | 统一维护 4 个业务 Agent 的显式状态映射、交接顺序与派发标记；不推进状态、不持有审批凭证 |
| 本地执行适配与目标 AgentTeams Bridge | `agent_runtime/teams_adapter.py` 中的 `AgentScopeExecutionAdapter` | 仅执行 Harness 已派发的 Mock/AgentScope 任务；真实 AgentTeams 任务、Handoff 和 Trace 待单独接入 |
| 审批门禁与回滚 | `policy/` + orchestrator | 高风险动作请求审批，**Agent 不持有审批凭证** |
| 复盘知识沉淀 | `retrospective/`（异步批处理） | Case 终态后生成复盘报告 + 知识条目 |

## 3. 本地 AgentScope 运行时行为

`--runtime-mode agentscope` 使用 AgentScope + 可配置 DeepSeek 凭证的本地实验路径。4 个 Agent 当前以空工具集输出结构化 JSON，避免带工具 ReAct 循环不收敛（`EXCEED_MAX_ITERS`）。这不是 AgentTeams 生产运行或官方 Trace 证据。

| Agent | toolkit | 状态映射 | 结构化输出字段 |
|-------|---------|---------|---------------|
| Triage | 空 | `TRIAGED` | `action, priority, classification, symptoms[], evidence_sources[], confidence` |
| Diagnosis | 空 | `DIAGNOSED` | `action, hypotheses[{description, confidence, code_locations[]}], impact_scope, risk_level, remediation_strategy` |
| Repair | 空 | `REPAIRING` | `action, patch_ref, branch, files_changed[], test_results[]` |
| Verification | 空 | `VERIFYING` | `action, quality_gate_passed, checks[], recommendation` |

**受控工具链**（Repair/Verification 的实际权威动作，不依赖 LLM 文本）：
- Repair + `repair_mode=demo_sandbox` → `tools/controlled_repair.py`：`sandbox_copy` → `apply_case_a_patch`，只在隔离沙箱操作，源仓库只读。
- Verification + `sandbox_ref` → `agents/verification.py`：`quality_gate.py` 确定性门禁，`quality_gate_passed` 覆盖 LLM 输出。

## 4. 失败与结果契约

- 迭代耗尽、空输出、模型拒绝 → `failed`（绝不冒充 `completed`）。
- `action`/`hypotheses` 等核心 LLM 输出嵌套在 `structured_output`，复盘层 `_parse_output_ref` 会展开。
- 分诊或诊断失败不得驱动后续状态转移；Case 停在当前阶段，等待重试、取消或人工升级。验证阶段的执行错误或失败会进入 `ESCALATED`，避免未验证补丁继续流转。

## 5. 审批安全边界（不可简化为 UI 约定）

1. **双因子签发**：`service_token`（Agent/脚本持有）不能单独签发审批 Grant；签发还要求 `X-Code-CCTV-Approval-Key`，缺失时返回 `403`。
2. **一次性 Grant**：`approval_token` 绑定 case/action/target_ref/时效，以 `X-Code-CCTV-Token-Type: approval` 消费即失效；仅存 `SHA256(approval_token)`。
3. **知识复核隔离**：`POST /api/knowledge/{record_id}/review` 要求服务令牌和独立人工审批密钥；reviewer 身份取服务端系统用户，客户端不可伪造。
4. **证据哈希链**：`tool_runs` 的 `chain_hash` 保证工具调用序列不可篡改。
