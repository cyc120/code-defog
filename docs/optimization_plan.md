# GOAI 赛题核对与优化方案

> **日期：** 2026-08-07（初赛截止 08-16，复赛 08-25~09-03，决赛 09-22）
> **依据：** 11 路多 Agent 交叉核对（6 子系统盘点 + 5 赛题维度对抗性验证）+ 139 项回归 + 实机复现
> **结论：** 选题正确、底座扎实（139 passed、审批/证据/状态机/复盘全部真实落地），但存在 **4 个 P0 Demo 级断链** 与 **1 个赛题核心硬阻塞（AgentTeams 零代码调用）**。优化核心 = 先修 P0 让复赛 Demo 在纯 HTTP/UI 下能跑到 CLOSED/PATCH_REJECTED，再用诚实披露守住初赛材料底线。

---

## 0. 一页结论

| 维度 | 现状 | 判定 |
| --- | --- | --- |
| 底座（SQLite/审批/证据链/状态机/复盘/UI） | 全量落地，139 tests + 4 subtests 全绿 | ✅ 扎实 |
| 多 Agent 闭环（八环） | 8 项中 4 项真实现、4 项部分实现 | 🟡 达标可演 |
| **AgentTeams 设计基点** | **仅文档映射，代码零 SDK 调用**；`--runtime-mode agentteams` fail-closed | 🔴 复赛硬阻塞 |
| **P0 Demo 断链** | base_commit 无写入 / 无 HTTP→CLOSED / 无 key 时伪造 completed / Case B 无法受控回放 | 🔴 4 处 |
| 初赛材料 | Skill 清单 + Identity 清单已有；PPT/视频/工具清单需新写 | 🟡 可行 |
| 复赛验证材料 | 无官方 AgentTeams Trace；`evidence/*.json` 为伪造历史产物（勿打包） | 🔴 硬阻塞 |

**第一优先级决策（08-07~08 今天做）：** 复赛验证材料二选一——
- **A 路线（XL 工作量）：** 真实接入 AgentTeams，双 Case 跑通并导出官方 Team/Task/Trace。满足框架 §6.2 L264 硬验收，但依赖 `agt`/Docker 控制面与凭据，风险高。
- **B 路线（S 文档改动）：** 明确降级为"本地受控回放证据包"（Case A→CLOSED、Case B→PATCH_REJECTED 的 evidence_indexer 哈希链导出 + 日志捕获），技术文档明文标注非官方 Trace。诚实、稳，但 AgentTeams 合规会被扣分。

该决策决定 PPT/视频的披露口径，**今天必须定**。

---

## 1. 赛题 vs 项目差距矩阵

> 每条含 `file:line` 证据与严重度。来源：11 路核对结果 + 实机抽查复核。

### 1.1 P0 —— 复赛 Demo 直接卡死 / 赛题核心

| # | 差距 | 证据 | 影响 |
| --- | --- | --- | --- |
| P0-1 | **base_commit 无任何写入方** → 纯 HTTP/UI 下 PLAN_APPROVAL 审批门禁必 409 | `store.py:178` schema、`:898-900` grant 校验引用；`store.py:663-670` Case 插入不设；全库 grep 无 writer | 评审用 Web UI 或 curl 走新鲜 demo 无法过计划审批，必须手改 DB |
| P0-2 | **无 HTTP 路径到达 CLOSED** → 演示剧本宣称终点 CLOSED 无法从 API 达成 | `server.py:546-603` 仅 approve/reject/cancel；CLOSED 只能 `store.transition_case`；approve_release 不 resume Agent（`server.py:571-583`） | Case A 停在 RELEASED，复盘钩子不触发 |
| P0-3 | **agentscope 模式无 API key 时伪造 `completed` 证据** | `teams_adapter.py:218-224` model=None 仍返回 completed；evidence 的 runtime_event_types 无 TOOL_CALL | 忘设 DEEPSEEK_API_KEY 会得到"绿色完成"的幻觉 JSON，录成运行时证据 |
| P0-4 | **Case B PATCH_REJECTED 无法经受控工具链回放** | `tools/controlled_repair.py:24-33` 只有 GOOD patch；坏补丁仅存在于 `demo_target/test_config.py:136-141` 常量与手工沙箱（`tests/test_daemon.py:1423-1440`） | "不盲目信任模型输出"的核心叙事缺受控载体 |
| P0-5 | **AgentTeams 代码零调用**：Bridge 接口（create_team/get_trace/await_result/cancel_task）全部缺失 | `teams_adapter.py:1-8` 明确否认集成；`serve.py:97-110` fail-closed；grep 无 AgentTeams SDK import；requirements 无 AgentTeams 依赖 | 框架 §6.2 L264 硬验收未达成；复赛"AgentTeams 编排"评估点无载体 |

### 1.2 P1 —— 强化竞争力（赛题要求的一半以上）

| # | 差距 | 证据 | 影响 |
| --- | --- | --- | --- |
| P1-1 | **工具调用断裂**：identities.yaml 声明 10 工具，tools/ 仅 controlled_repair.py；四 Agent 全空 Toolkit | `teams_adapter.py:229-234` toolkit_map=no_tools；`agents/triage.py:22-23`、`diagnosis.py:17-18` 为 stub | 八环之"工具调用"只剩两个硬编码确定性调用 |
| P1-2 | **回滚半途**：ROLLED_BACK 仅状态表定义，零可达路径 | `state_machine.py:46-47`；grep 除 state_machine/store 零命中 | PATCH_REJECTED vs ROLLED_BACK 语义只有一半能演 |
| P1-3 | **证据回放验证缺失**：chain_hash 写入正确但无 verify/replay 函数 | `store.py:1013-1033` 计算链；无 verify_chain；`/evidence` 只镜像存储哈希 | §7.4 可审计性声明无代码背书 |
| P1-4 | **verified 知识单向**：沉淀→复核完成，但无 agent 消费 | `store.py:1272-1306` review；grep agent_runtime/agents 零消费 | §5.3 知识复用环断裂 |
| P1-5 | **connectors/ 与 policy/ 是空壳** | `connectors/__init__.py:1`、`policy/__init__.py:1` 仅 docstring | 框架 §4.1 目录职责与实现不符；无专属策略拒绝测试 |
| P1-6 | **≥3 源归并未测试/未实现** | `tests/test_daemon.py:575-607` 仅合并 2 源 | §9.2"每案例≥3 来源"未证明 |
| P1-7 | **修复空 patch_ref 不升级** → 演示 hang 在 REPAIRING | `orchestrator.py:155-165` completed=True 但无 patch_ref 不 advance | 现场演示冻结 |
| P1-8 | **skills 无统一调用面**：4 个复盘 skill 纯函数但仅 Python import；无 HTTP/CLI dispatcher | `retrospective/skills.py` 无 `__main__`/argparse/HTTP | "Skill 清单→可调用 skill"故事弱 |

### 1.3 P2 —— 打磨与诚实性

| # | 差距 | 证据 | 影响 |
| --- | --- | --- | --- |
| P2-1 | `delivery_id` retry→409 已实现但无测试 | `server.py:334-336` 返回 409；测试只断言 store 级 duplicate | 幂等叙事缺证据 |
| P2-2 | 候选关联评分 flat=0.5，无 Jaccard/时间评分、无 >0.8 自动关联 | `store.py:637-653` | 与 §7.1 L434-448 不符 |
| P2-3 | pending→独立 Case 升级仅在候选存在时触发 | `store.py:647-653` deadline 仅 `if candidate_rows` | 无候选的 pending 永挂 |
| P2-4 | 补全触发（partial→complete 合并）未实现；message_pattern 参数化归一缺失 | §7.1 L450 / `store.py:91` | 关联完整性缺口 |
| P2-5 | consume 端 target_ref 绑定可选 | `store.py:754-756` `if target_ref and ...` | 弱于 issue 端绑定 |
| P2-6 | tool_runs.approval_id 恒 NULL；output_sha256 为派生摘要非原始 stdout | `agents/verification.py:87`；grep 无 caller 传 approval_id | 工具↔审批关联隐式 |
| P2-7 | trace_id 未端到端贯穿 advance 链 | `teams_adapter.py:351` 每次新 mint | 审计链路不严格 |
| P2-8 | skill_catalog 有误导标记：symptom_extractor=✅ 实为透传；compliance_checker=🟡 但零实现 | `store.py:569-577` 透传；skills.py:391-396 恒提 compliance_checker | 评审核对即穿帮 |

### 1.4 材料差距

| 阶段 | 已有可拷贝 | 需新写 | 硬阻塞 |
| --- | --- | --- | --- |
| 初赛 08-16 | Skill 清单、Agent Identity 清单、README/框架/案例说明 | 方案 PPT、讲解视频、工具/云产品清单（S） | — |
| 复赛 08-25 | 可运行 Demo（web+daemon）、可执行代码、技术文档、requirements 本体 | 依赖披露三态文档、验证材料（被 AgentTeams 阻塞） | 官方 Trace 或明确降级 |
| 决赛 09-22 | — | 路演 PPT、开源计划、项目一页纸、LICENSE | 无 |

**证据红线：** `evidence/production_dispatch_*.json` 是伪造历史产物（repairing 声称改过不存在的 `demo_target/config_loader.py`，verifying 声称"128/128 tests / billing / 覆盖率 89%"），已被 `.gitignore:13` 排除，**打包提交时必须排除**，严禁作为运行证据引用。`.env.example:5` 的 DEEPSEEK_API_KEY 标签（"Required for production AgentTeams mode"）与 `README:13`（production 非 AgentTeams）矛盾，需改。

---

## 2. 优化方案（按优先级）

### 2.1 P0 —— 修复清单（复赛 Demo 能否跑通，建议 08-07~08-12 内完成）

**P0-1 补 base_commit 写入。** 改动 `daemon/store.py` 的 Case 创建路径（`create_or_find_case` 或 `server.py` 的 `/api/cases` handler）：接收 payload 的 `base_commit`，或对 `repository_ref` 解析（`git -C <repo> rev-parse HEAD`，失败则留空并记录 warning）。Web UI（`web/index.html:841`）把 `caseData.base_commit` 传给审批目标。验收：新增 HTTP 测试——不发 raw SQL 也能 `approve_plan` 签发成功。工作量 S。

**P0-2 补 HTTP→CLOSED 路径。** `daemon/server.py:546-603` 增加 `close` action（service_token 即可，或 RELEASED 后由服务端在放行宽限期自动关）。approve_release 后若无回滚信号，自动 `RELEASED→CLOSED` 并触发复盘钩子。验收：一条 HTTP 测试从 Case 创建一路到 CLOSED + 复盘钩子触发。工作量 S。

**P0-3 agentscope 无 key 时 fail-closed。** `teams_adapter.py:218-224` 在 `set_mode('agentscope')` 时校验凭据，缺失则 raise（或标记 run 为 `status='unverified'` 并在 evidence 加醒目 warning），禁止返回"绿色 completed"。验收：无 key 的 dispatch 不产生可当证据的 completed 记录；加测试。工作量 S。

**P0-4 受控 Case B 工具路径。** `tools/controlled_repair.py` 增加 `repair_mode='case_b'` 分支应用 `WRONG_FIX_REPLACEMENT`（当前仅测试常量），带同样的沙箱隔离与 tool_run 证据；修复 Agent 在 repair_mode=case_b 时走该分支，quality_gate 退出 1 → PATCH_REJECTED 可从工具链回放。验收：`tests/test_daemon.py:1412-1450` 的坏补丁路径改走受控工具。工作量 M。

**P0-5 AgentTeams 二选一（今天决策）。**
- **A 路线**：`agent_runtime/agentteams_bridge.py` 实现 `AgentTeamsWorkflowBridge`（create_team/dispatch_task/await_result/get_trace/cancel_task），`serve.py` agentteams 模式接真实控制面；双 Case 跑通导出官方 Trace。工作量 XL（3-5 天攻坚，依赖 agt/Docker/凭据，须先 Hello World 验证能力边界）。
- **B 路线**：不改编排层；补 `docs/competition_disclosure.md` 明确降级为受控回放证据包（Case A→CLOSED、Case B→PATCH_REJECTED 的 evidence_indexer 导出 + SSE 日志），技术文档标注"本地确定性回放，非官方 AgentTeams Trace"。工作量 S。

### 2.2 P1 —— 强化清单（初赛材料落地 + 复赛竞争力，08-12~08-20）

| 项 | 改动 | 工作量 |
| --- | --- | --- |
| P1-1 最小真实工具 | 实现 `tools/symptom_extractor.py`（~30 行正则从原始日志提 exception_type/message_pattern/frames）、`tools/incident_matcher.py`（封装 `compute_incident_signature` 返回 `{is_same_incident,confidence,reason}`）、`tools/code_searcher.py`（~20 行 grep 包装）；绑定到 `identities.yaml` 的 Triage/Diagnosis toolkit | M（2-3 个 S） |
| P1-2 回滚路径 | 加 `canary_sim` 确定性工具（对旧 vs 补丁后 cli.py 跑 quality_gate 出指标 delta）+ `POST /actions` 的 `mark_rolled_back`（approval-gated）→ `RELEASED→ROLLED_BACK` 可演 | M |
| P1-3 证据回放 | `store.verify_chain(case_id)` 重算哈希链，`/api/cases/{id}/evidence` 返回 `chain_valid`；测试断言篡改后 flip False | S |
| P1-4 知识复用 | `orchestrator._build_agent_context`（:34-107）注入 `status='verified'` 知识；测试证明 verified 记录影响后续 Case 诊断 | M |
| P1-5 空壳补实 | `connectors/` 实现 issue/log/feedback/CI 四薄适配器（各 ~15 行 normalize→POST /api/cases）或文档说明通用 intake 替代；`policy/` 实现 `risk_classify()` 与 `allowlist_check()` 供 orchestrator 引用 | M |
| P1-6 ≥3 源归并测试 | 新增测试：issue+log+ci 三类来源经 incident_signature 并入同一 Case 且 `len(sources)>=3` | S |
| P1-7 空 patch_ref 升级 | `orchestrator.py:155-165` 对 completed 但 patch_ref='' 走 `ESCALATED`（镜像 VERIFYING 失败分支 :180-186）+ 测试 | S |
| P1-8 skills 调用面 | `python -m skills run <name> ...` CLI 或 `POST /api/skills/{name}` 调度器，把 4 个复盘 skill + quality_gate + 新工具注册为可调用 skill | M |

### 2.3 P2 —— 打磨清单（按余量排）

P2-1 补 409 测试；P2-2 候选评分实现 Jaccard+阈值；P2-3 pending 无条件 deadline；P2-4 补全触发 + 参数归一；P2-5 consume 强制 target_ref；P2-6 传 approval_id + 改 output_sha256 为原始 stdout 摘要；P2-7 trace_id 端到端贯穿；P2-8 修正 skill_catalog 标记（symptom_extractor→🟡、compliance_checker→实现 40 行或从 proposer 移除、evidence_indexer→加 verify_chain 或删"replay"措辞）。

---

## 3. 排期（09 天初赛 + 复赛 8 天）

### 3.1 初赛冲刺（08-07 → 08-16，今天起）

| 日 | 事项 | 产出 | 工作量 |
| --- | --- | --- | --- |
| **D1-2 (08-07/08)** | **AgentTeams 决策攻坚**：验证 agt/Docker/控制面 Hello World → 定 A/B 路线 → 定 PPT/视频披露口径。同步 P0-1/P0-2/P0-3 修起来 | 决策记录 + 3 个 P0 修复 | XL |
| **D2-3** | 拷贝轻改三清单：Skill 清单补附录字段+标记核验；Identity 清单补"协同/交接契约"对表；新写工具/云产品三态清单（`docs/tool_inventory.md`） | 3 份材料 | S-M |
| **D3-5** | 写方案 PPT（6-8 页：问题→架构→八环闭环→AgentTeams 映射→三层审批→差异化→路线图→诚实披露） | PPT 大纲 + 成稿 | M |
| **D5-7** | 录讲解视频：按 `demo_case_spec.md:110-118` 七步剧本做 Web UI + CLI 确定性回放，5-8 分钟；AgentTeams 若未接入标注"本地受控回放" | 视频 | M |
| **D7-8** | 提交包整合：5 项齐备 + 全量回归复跑 + AgentTeams 状态披露定稿 + 排除伪造 evidence JSON | 提交包 | S |
| **D9 (08-16)** | 提交 | — | — |

### 3.2 复赛冲刺（08-17 → 08-25）

| 日 | 事项 | 产出 |
| --- | --- | --- |
| 08-17~18 | A 路线：AgentTeams 真实接入 + Hello World + 双 Case 官方 Trace；或 B 路线：受控回放证据包 + 披露文档 | 验证材料（硬阻塞项） |
| 08-19~20 | P1-1 最小真实工具 + P1-6 三源归并测试 + P1-7 空补丁升级 | 工具链真实化 |
| 08-21~22 | 依赖披露三态文档 + 修正 `.env.example` + 删/排除伪造 production_dispatch JSON + 架构/时序图 | 技术文档升级 |
| 08-23~24 | P1-2 回滚路径 + P1-3 证据回放 + P1-8 skills 调度器；补端到端 HTTP 回放单测（创建→CLOSED、创建→PATCH_REJECTED 各一条） | Demo 可回放闭环 |
| 08-25 | 复赛提交 | — |

### 3.3 决赛项（09-22 前）

选开源许可证（建议 MIT/Apache-2.0）补 LICENSE 并删 `README:183-185` 的 "All rights reserved"；开源计划；路演 PPT（由初赛 PPT 迭代）；项目一页纸。

---

## 4. 诚实披露红线（评审信任的生命线）

1. **AgentTeams**：未接入时不得在任何材料中把本地 UUID、AgentScope 事件、`smoke_test` 输出当作官方 Trace（README:141-143 已禁止，保持）。
2. **evidence/*.json**：伪造历史产物，**打包排除**，严禁引用。
3. **工具清单**：逐项标注 本地实现 / 接口预留 / 缺失，不得用 ✅ 掩盖 stub（P2-8 修正）。
4. **无 key 行为**：P0-3 修复后，agentscope 无 DEEPSEEK_API_KEY 时不得产出可当证据的 completed 记录。
5. **PPT/视频披露口径**：与 AgentTeams 决策（D1-2）保持一致；B 路线下明确"受控回放，非官方 Trace"。

---

## 5. 建议的第一周执行动作（08-07 今天）

1. 开会定 **AgentTeams A/B 路线**（P0-5）。
2. 修 **P0-1/P0-2/P0-3**（合计约半天，都是 S 级改动，直接决定复赛 Demo 可运行性）。
3. 删/隔离 `evidence/production_dispatch_*.json` 于提交包之外，改 `.env.example` 误导标签（各 5 分钟）。
4. 启动 `docs/tool_inventory.md` 三态清单（S）。
