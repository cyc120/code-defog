# Skill 清单

> Code CCTV DevLoop · GOAI Agent Infra 方向三 · 竞赛材料
> 依据框架 §11.1 六类 Skill 候选扩展；标注每项的**实现状态**（✅ 已在代码实现 / 🟡 接口预留 / 📋 待独立化）。

> **说明：** 本清单描述本地代码与赛事候选 Skill 的实现状态，不构成真实 AgentTeams Worker、Team、Handoff 或 Trace 已部署的声明。

## 1. 分诊类（Triage Skills）

| Skill | 描述 | 输入 | 输出 | 复用潜力 | 状态 |
|-------|------|------|------|---------|------|
| `issue_normalizer` | 将不同格式 Issue 统一为结构化字段 | 原始 Issue 文本 | `{title, description, severity, reporter, url}` | 通用 Issue 聚合 | 🟡 `case_sources` 已存标准化字段 |
| `symptom_extractor` | 从错误日志提取关键错误签名 | 错误日志文本 | `[{exception_type, message_pattern, stack_frames[]}]` | 通用日志分析 | ✅ `extracted_signals_json` 落库 |
| `incident_matcher` | 判断不同来源是否同一事故 | 两个规范化事件源 | `{is_same_incident, confidence, reason}` | 通用跨源关联 | ✅ 两级指纹 `incident_signature` |

## 2. 诊断类（Diagnosis Skills）

| Skill | 描述 | 输入 | 输出 | 复用潜力 | 状态 |
|-------|------|------|------|---------|------|
| `code_searcher` | 按错误签名检索相关代码位置 | 异常类型 + 消息模式 + 仓库路径 | `[{file, line, snippet, relevance}]` | 通用代码检索 | 🟡 Diagnosis 输出 `code_locations[]` |
| `git_blamer` | 追溯相关代码行最近修改 | 文件路径 + 行号 | `[{commit, author, date, message}]` | 标准 git blame 封装 | 📋 未独立实现 |
| `impact_analyzer` | 分析变更影响范围 | 文件路径 + 行号 | `{callers[], callees[], tests[], risk_score}` | 静态分析工具 | 🟡 Diagnosis 输出 `impact_scope` |

## 3. 修复类（Repair Skills）

| Skill | 描述 | 输入 | 输出 | 复用潜力 | 状态 |
|-------|------|------|------|---------|------|
| `patch_generator` | 隔离工作树生成最小修复补丁 | 诊断报告 + 仓库引用 + 修复策略 | `{patch_ref, branch, files_changed[]}` | 核心修复能力 | ✅ `tools/controlled_repair.py` |
| `test_augmenter` | 为修复生成补充测试用例 | 补丁 + 现有测试 | `[{test_file, test_function, covers}]` | 可与任何补丁工具组合 | 📋 未独立实现 |

## 4. 验证类（Verification Skills）

| Skill | 描述 | 输入 | 输出 | 复用潜力 | 状态 |
|-------|------|------|------|---------|------|
| `quality_gate` | 执行测试套件、静态检查和覆盖率门禁 | 补丁引用 + 测试套件路径 | `{passed, failures[], coverage_delta, lint_issues[]}` | 通用 CI 质量门禁 | ✅ `demo_target/quality_gate.py` 确定性门禁 |
| `canary_simulator` | 模拟灰度发布并对比指标 | 补丁 + 模拟环境配置 | `{metrics_before, metrics_after, anomaly_score, recommendation}` | 部署验证通用工具 | 📋 接口预留（`canary_simulator` 已入 Skill 候选映射） |

## 5. 复盘类（Retrospective Skills）

| Skill | 描述 | 输入 | 输出 | 复用潜力 | 状态 |
|-------|------|------|------|---------|------|
| `case_summarizer` | 从完整证据包生成面向人的复盘报告 | Case 完整证据包 | 复盘报告 Markdown | 通用复盘模板 | ✅ `retrospective/skills.py::case_summarizer` |
| `knowledge_extractor` | 从已关闭 Case 提取可复用知识条目 | Case 证据 + 复盘报告 | `[{title, category, content, confidence}]` | 知识库构建通用工具 | ✅ `retrospective/skills.py::knowledge_extractor`（确定性，不调 LLM） |

## 6. 审计类（Audit Skills）

| Skill | 描述 | 输入 | 输出 | 复用潜力 | 状态 |
|-------|------|------|------|---------|------|
| `evidence_indexer` | 生成可下载/可验证的证据索引（含工具哈希链） | Case ID | `{case_id, evidence_tree[], hashes[], trace}` | 通用审计导出 | ✅ `retrospective/skills.py::evidence_indexer` |
| `compliance_checker` | 检查审批 Grant、门禁和策略合规性 | Case ID | `{compliant, violations[], recommendations[]}` | 合规审计通用工具 | 🟡 由 `handle_knowledge_review` 的人工审批密钥校验与 `handle_case_action` 覆盖 |

## 7. 实现映射说明

- **确定性优先**：复盘类 Skill 为纯函数、离线可复现（同一证据 → 同一输出），不依赖 LLM 结果稳定性。
- **受控工具链**：`patch_generator` 与 `quality_gate` 是 Repair/Verification 的权威动作源，LLM 结构化输出仅作补充说明，避免"以模型文本作为唯一验证依据"。
- **知识安全边界**：`knowledge_extractor` 产出默认 `pending_review`，经人工复核（服务令牌 + 独立人工审批密钥）→ `verified` 才能被后续 Agent 引用，防止模型幻觉污染知识库。
