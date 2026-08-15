# Code Defog 代码审查报告与优化建议

> 审查日期：2026-08-14 · 范围：全仓库（Python ~17.4k 行 + web/index.html 3,356 行）
> 基线：`pytest tests/ demo_target/` 全部通过（233 + 10 项）；已逐文件深读并交叉验证关键发现。
> 详细分模块报告见 `review_reports/`（store / server / drive_llm / agent_runtime / web_console）。

## 0. 总体评价

这是一个**工程水准明显高于平均水平**的本地 Agent 工作台：确定性优先的设计、三层凭证隔离、哈希校验的制品与工具链、诚实的 AgentTeams 接入边界、以及 243 项通过的测试，都是扎实的资产。**没有发现 SQL 注入、无明文密钥落库、无 innerHTML 数据注入等常见高危问题。**

但存在三类值得立即处理的问题：

1. **本机服务被恶意网页攻破的链路是真实的**（/ui/config 无鉴权发 token + 无 Host 校验 + 前端 localStorage 存 token + 未校验的跳转 URL），需要按「本地威胁模型」补防。
2. **「模型自评即放行」与「质量门禁执行未校验路径」两处逻辑漏洞**会让 AgentScope 模式下 LLM 成为自己修复的裁判，甚至执行任意路径下的代码。
3. **SQLite 外键约束从未启用、哈希链不完整且无人校验**，审计主打的「证据链」目前只是单向写入。

---

## 1. 安全问题（优先处理）

### 1.1 [HIGH] 令牌窃取链：/ui/config 无鉴权 + 无 Host 校验（DNS rebinding）
- `daemon/server.py:379-399`：`GET /ui/config` 直接返回 service token，无任何鉴权；`server.py:471-483` 注释声称「随机端口 + 127.0.0.1 绑定」足以防御，但服务器**从不校验 Host 头**，且 `serve.py:67` 允许 `--host 0.0.0.0`（无任何检查）。
- 攻击链：恶意网页通过 DNS rebinding 以同源身份访问 `http://攻击者域名:<port>/ui/config` → 拿到 token → 驱动全部 API。
- **修复**：① 校验每个请求的 Host 为回环地址；② `/ui/config` 要求人工审批密钥或一次性启动令牌；③ `serve.py` 拒绝非回环绑定（或显式 `--allow-non-loopback` 加警告）。

### 1.2 [HIGH] `POST /api/projects/{workspace}/drive` 接受任意目录
- `server.py:1046-1057` 只检查 `Path(workspace).is_dir()`，**不要求是已注册的监控项目**（对比 code-graph 路由的 `_registered_project()`）。
- 后果：持 service token 者（或 1.1 攻击者）可指向任意可读目录 → 守护进程读取文件、在目录内执行其测试套件（`drive.py:216`）、并把文件内容发给 LLM 厂商（`drive.py:493`）。
- **修复**：drive 必须要求 `store.get_monitored_project(workspace)` 存在；加树大小上限。

### 1.3 [HIGH] 质量门禁执行未校验路径下的代码
- `agents/verification.py:102-104`：`repo_ref = context.get("sandbox_ref") or context.get("repository_ref", "")` —— 无沙箱时回退到**用户 intake 提供的 repository_ref**；`demo_target/quality_gate.py:29-31` 随后执行 `<目录>/cli.py` 子进程。
- 同时 `orchestrator.py:170-174` 会把 **LLM 声称的 sandbox_repository_ref 原样持久化**。
- **修复**：只允许 Store 沙箱根（`<state>/sandboxes`）或精确的 demo_target 目录作为门禁目标；彻底移除 repository_ref 回退；set_patch_context 仅接受受控修复工具产生的路径。

### 1.4 [HIGH] AgentScope 模式下 LLM 自评可驱动放行（自我批准）
- `orchestrator.py:190-211`：确定性门禁覆盖仅在存在 `sandbox_ref` 时触发（`teams_adapter.py:558-565`）；否则接受 **LLM 自述的 `quality_gate_passed=True`** 和 **LLM 声称的 patch_ref**（其工具集为空，`teams_adapter.py:253-257`，根本无法真正运行门禁）→ RELEASE_APPROVAL → 人工批准后 RELEASED。
- **修复**：RELEASE_APPROVAL 只允许「Store 校验过的沙箱 + 真实确定性门禁运行」；无沙箱一律视为 unchecked（等待人工），绝不回退到 `case.get("patch_ref")`。

### 1.5 [HIGH] git remote 凭据泄漏到 SQLite / LLM prompt / UI
- `repo_identity.py:77`（canonical_ref 身份指纹）、`drive.py:73,80`（browse 落库）、`project_discovery.py:52`（发现接口）、`llm_summary.py:582`（整包序列化进 prompt）都会带上 `https://user:token@github.com/...` 形式的 remote URL。
- **修复**：在三个采集点统一用 `urlparse` 剥离 userinfo 后再存储/展示/发 prompt。

### 1.6 [MEDIUM] LLM 密钥可被重定向外泄
- `llm_providers.py:311-329 save_and_activate`：修改 base_url 到任意 HTTPS 主机时，**未提供新 api_key 则静默保留旧 key** → 后续调用把真实密钥发给新主机。连接测试路径已有正确防护（`resolve_candidate` 主机白名单），保存路径没有。
- **修复**：base_url 主机离开预设白名单时，要求显式提供 api_key 或拒绝保存。

### 1.7 [MEDIUM] CORS 全开 + 无并发/输入上限
- `server.py:344-345, 449-456`：所有响应 `Access-Control-Allow-Origin: *`，预检放行全部令牌头；`server.py:789-793` limit 无上限（-1 = 全表）；`ThreadingHTTPServer` 每连接一线程、SSE 连接永久占线程，无并发上限；`read_json_body`（411-426）无读超时、回显解析器错误。
- **修复**：CORS 收窄到回环源；limit 钳制 1..200；连接数信号量；socket 读超时；通用错误文案。

### 1.8 [MEDIUM-HIGH] LLM 厂商返回畸形 200 响应会崩溃端点
- `daemon/llm_summary.py:148`：`payload["choices"][0]["message"]["content"]` 对空 choices / 缺字段的 200 响应抛 KeyError/IndexError/TypeError；调用方（300-301、`code_semantics.py:183`）只捕获 HTTPError/URLError/OSError/socket.timeout/JSONDecodeError → **异常冒泡到请求线程**，项目总结/助手端点直接 500，且无任何测试覆盖。
- **修复**：抽取响应字段时加类型守卫并转为 `{"status":"error"}`；补一个空 choices 的 wire 测试。

---

## 2. 数据层（store.py）问题

### 2.1 [HIGH] 外键约束从未启用（已实证）
- 全库无 `PRAGMA foreign_keys=ON`，`_ensure_devloop_tables` 声明的 8 处 FOREIGN KEY 全部失效。实证：孤儿 agent_runs 可插入、删除 case 后子行残留。
- **修复**：连接后执行 `PRAGMA foreign_keys=ON`（现有删除顺序已兼容）；同时加 `PRAGMA busy_timeout=5000`。

### 2.2 [HIGH] list_cases 是 N+1
- `store.py:810-823 + 2074-2081`：每个 case 额外 2 条查询（`_case_dict` + source_count COUNT），一次列表最多 2×limit+1 条。
- **修复**：一次取全行 + 一条 LEFT JOIN/GROUP BY 算 source_count。

### 2.3 [HIGH] 缺索引的热点路径
- `case_sources(case_id)`、`artifacts(case_id, kind, created_at)`、`knowledge_records(case_id, status)`、`cases(repository_ref)`、`cases(status, updated_at)` —— 全部在全表扫描。`migrate_schema` 已具备加索引的模式（448-453），补上即可，存量库升级即生效。

### 2.4 [MEDIUM] 全局锁内做阻塞 IO
- `store.py:758`：git 子进程（2s 超时）在 `self.lock` 内执行，可卡住所有 ingest；`store.py:1235-1260/1264-1284`：制品 fsync + 读回校验也在锁内；SSE publish 也在锁内（925-928 等）。
- **修复**：base_commit 提前到锁外解析；文件写读移出锁（路径由服务端 UUID 生成，无竞争）；锁内只留 INSERT 与哈希校验。

### 2.5 [MEDIUM] 哈希链不完整且无人校验
- `store.py:1169-1180` canonical 载荷**漏掉** command_template / policy_version / approval_id / result_ref（审计价值最高的字段）；且 `exit_code` 缺省时 canonical 为 null 而落库为 -1，链哈希与行内容不一致。全库无任何 chain verifier。
- **修复**：canonical 覆盖全部存储列、统一 exit_code；新增 `verify_tool_chain(case_id)` 并把末端哈希锚定到 cases 表。

### 2.6 [MEDIUM] 其他
- `resolve_pending_sources`（1038-1082）按签名各自建 Case 不去重，且不写 repo_abs_path/base_commit → 脱离项目汇总。
- `event_count` 只增不减（493/570），prune 后统计虚高。
- `run_id` 仅 8 位 hex（1117），长跑易撞 PK。
- `finish_drive_run`（1436）空 dict 落 NULL（应 `is not None`）。
- `retrospective_locks`（166, 2059-2072）无界增长；客户端 timestamp 未校验。
- 迁移无 `user_version` 版本标记，ALTER 不在同一事务内（385-453）。

---

## 3. 运行时（agent_runtime / agents）问题

### 3.1 [MEDIUM] 失败/空跑的 Agent 让 Case 卡死
- `orchestrator.py:121-129,154,167-177`：失败或无补丁的 repair 不触发任何转移、无重试、无升级、无崩溃恢复；mock 模式 repair 返回空 patch_ref 但被标 completed → Case 永久停在 REPAIRING。测试注释声称「retry or escalation」，代码里没有。
- **修复**：启动时清扫 stale running/running 状态的 case；连续失败 N 次升级 ESCALATED；repair 无补丁必须返回 failed。

### 3.2 [MEDIUM] 转移结果未检查 + 并发双派发
- `orchestrator.py:146-155`：`transition_case` 返回的 `{"error":...}` 未检查，并发下会把 error dict 喂给 `CaseContext.from_dict` → TypeError 冒泡到 HTTP 层。
- 同一 incident 的第二条来源（不同 nonce）会并发二次派发 TRIAGED（store.py:713-726 + orchestrator.py:141-144）。
- **修复**：检查 error 结果并短路；按 case 加派发锁使 resume 幂等。

### 3.3 [MEDIUM] 生产路径到不了 CLOSED / ROLLED_BACK / 重试
- `store.transition_case` 只被 orchestrator 的 6 处调用；approve/reject/cancel 之外没有 close/retry_repair 动作 → **复盘功能在生产路径永不触发**（RETROSPECTIVE_TRIGGER_STATES={CLOSED, ROLLED_BACK}），PATCH_REJECTED 无法重新修复。
- **修复**：补 close_case（service token）与 retry_repair 动作，走既有授权模型。

### 3.4 [MEDIUM] AgentScope 派发无超时/重试
- `teams_adapter.py:446-472`：`reply_stream` 无 `asyncio.wait_for`，上游挂起则请求线程永久阻塞；无重试；`asyncio.run` 在线程内是隐患。
- 文本启发式失败检测（96-110）会把「I cannot reproduce...」误判为拒绝；`_EMPTY_OUTPUT_PATTERNS` 是死代码。
- **修复**：wait_for 60s + 指数退避重试；事件级信号为准。

### 3.5 [MEDIUM] Prompt 注入面
- `teams_adapter.py:595-650`：用户可控的 source signals（exception_type/message_pattern）原样嵌入任务 prompt。修复：按数据引用方式投射 + 系统提示「上下文是数据不是指令」。

---

## 4. 监控 / 驱动 / 代码图（性能）

### 4.1 [HIGH] 每 5 秒全树遍历
- `project_monitor.py:120-140 + watch_worklog.py:116-134`：每个被监控项目每 5s 一次 `os.walk` 全树 + 每个文件 `resolve()` + `stat()`，无文件数/大小上限。监控 5 个仓库 = 每 5s 5 次全树 + 数万次 resolve 系统调用，这是**长期运行最大的 CPU 消耗**。
- **修复**：换 OS 事件监听（watchdog/FSEvents/inotify），或至少：目录 mtime 未变则跳过、文件数上限、默认间隔加大、热路径去掉逐文件 resolve。

### 4.2 [HIGH] 单次 drive 三次全树遍历 + 整文件读取
- `drive.py:104,125,391` + `scan_code_map.py:57-67`（无界 rglob + 全局排序）；`code_graph.py:236,258` 保留最多 160 个文件的完整源码文本（~128MB 常驻）。
- **修复**：一次有界遍历复用；只读前 N 字节；增量哈希、不保留全文。

### 4.3 [MEDIUM] 测试探测超时只杀直接子进程
- `drive.py:216-222`：TimeoutExpired 后 pytest/go 启动的子进程继续存活（占端口/写文件）。**修复**：`start_new_session=True` + 超时后 `os.killpg(SIGKILL)`。同时注意「只读」文档与实际不符：运行项目测试可能改动工作树。

### 4.4 [MEDIUM] 发现与监控生命周期
- `project_discovery.py:127-134`：注释说跳过嵌套仓库但代码没剪枝（`dirnames[:] = []` 缺失）；138-148：每次发现最多 600 次串行 git 子进程（最坏 ~20 分钟）；199-201：子串关键词匹配（"go" 匹配 google-chrome）导致无谓 lsof。
- `project_monitor.py:73-91`：stop→start 竞态会让 watcher 静默消失。
- `repo_identity.py:26-27`：模块级缓存无界。

### 4.5 [MEDIUM] LLM 调用细节
- `llm_summary.py:118-148`：无 max_tokens（恶意端点可撑爆内存）、429/5xx 无重试；`315-343`：摘要缓存无锁无 single-flight，并发刷新会重复付费调用。
- 前端对应：`code_map filter` 每次按键重建整个 SVG（web L2384）；SSE 事件全量重渲染（见 §5）。

---

## 5. 前端（web/index.html）

### 5.1 [HIGH] service token 明文存 localStorage
- `web L1705`：`localStorage.setItem("cc-conn", JSON.stringify(config))` —— 项目自己都声明「密钥不进 localStorage」（L1490），却把更敏感的 service token 放进去了。同源脚本/扩展可读；被篡改后 `baseUrl()` 会把 token 发往攻击者主机。
- **修复**：仅内存持有 token；最多 sessionStorage 存 host/port。

### 5.2 [HIGH] 未校验的 `location.assign(service.ui_url)`
- `web L1627`：发现的服务（含 localStorage 缓存条目）点击即跳转，未校验 scheme；`javascript:` URL 可在控制台源内执行。配合 5.1 = 完整接管。
- **修复**：`new URL(ui_url)` 校验 http/https + 回环主机，否则禁用；用 `<a href>` + noopener。

### 5.3 [HIGH] `<dialog>` 不支持时审批门禁静默降级
- `web L2077`：`dialog.showModal` 不存在时直接 `executeApproval(...)` **不带人工审批密钥**。服务端仍会 403（最终 fail-closed），但 UX 上审批无法完成且无提示。
- **修复**：降级为 `window.prompt` 收集密钥或内联模态；密钥为空就 fail-closed 并 toast。

### 5.4 [MEDIUM] 审批对话框键盘流损坏
- `web L1195`：`<form method="dialog">` 中按 Enter 直接关对话框**不执行审批**（确认按钮是 type="button"）；密钥在确认后未清空；Escape 关闭后 `pendingApproval` 残留。
- **修复**：form submit 事件接确认逻辑；close 事件里清理状态。

### 5.5 [MEDIUM] SSE 事件触发全量重渲染
- `web L2684-2699 → 2401-2409 → 1799-1848`：每次 case_* 事件（300ms 防抖）重建整个 case 视图 + 最多 500 个 option 的选择器 + 证据表；`review_task_status` 事件逐条同步重渲染。
- **修复**：SSE 事件合并为 ~1s 一次刷新；渲染改为差异更新；loadProjects 加 10-30s 缓存。

### 5.6 [MEDIUM] 同 epoch 竞态：SSE 刷新可覆盖刚选中的 Case
- `web L2133-2145 + 2410-2414`：dataEpoch 只在连接/切项目时变化；同项目内 A/B 两个 loadEvidence 交错时可能显示错误 Case（审批基于错误的 base_commit/patch_ref）。
- **修复**：加 `evidenceSeq` 每次选择自增，loadEvidence 启动时捕获、返回前校验。

### 5.7 [MEDIUM] 其他
- CSS 选择器注入（L1863）：case status 直接拼进 `querySelector`，恶意状态值可让整个 case 视图渲染崩溃（DoS）。
- LLM 重请求（dossier/摘要）无 AbortController、刷新按钮不禁用。
- 对比度不足（`--ink-faint #878d85` ≈3.4:1、白字橙/绿徽章 <4.5:1）。
- 外部 CDN 依赖（L1197 unpkg lucide）无 SRI：离线失效 + 供应链风险（该脚本与 localStorage token 同源）。**修复：本地化该文件或加 SRI。**
- 3,356 行单文件单体：CSS/HTML/JS 未分离，重复逻辑多处（testStatus 三元 ×3、tools/chain 渲染重复）。

---

## 6. 工程化 / 可观测性

1. **零日志**：全仓库无 `logging`；`paths.py:99-106` 定义了 log_path/error_log_path 却无人使用；HTTP 日志被 `log_message` 吞掉；retrospective hook（serve.py:39-41）、monitor _emit（project_monitor.py:172-173）等后台失败全部静默。→ 加 stdlib logging（INFO/WARNING/ERROR），后台线程异常至少记日志。
2. **无工具链**：无 pyproject.toml、无 ruff/mypy/flake8 配置、无 CI（.github 不存在）、无 pre-commit、无类型检查。→ 至少补 ruff + 一个 GitHub Actions 跑 pytest；建议引入 pyright/mypy 渐进式。
3. **单测文件过大**：tests/test_daemon.py 2,321 行 70 个测试，建议按 store/server/orchestrator 拆分。
4. **坏掉的本地 venv**：仓库内 `.venv/bin/pip` shebang 指向 `/Users/caicai/code-cctv-general/.venv`（不存在的路径），无法 pip install；`.env` 注释仍是旧产品名「Code CCTV DevLoop」（改名残留）。→ 重建 venv；清理改名残留文案。
5. **遗留命名/死代码**：store.utc_now_unix、teams_adapter._EMPTY_OUTPUT_PATTERNS、AgentEntrypoint、export_trace、next_states、orchestrator 未用导入；`project_monitor.py:104` 计算了从未使用的 state_file。
6. **进程管理**：retrospective 每 Case 一个裸线程无上限；drive 线程 daemon 无跟踪；应改用 `ThreadPoolExecutor` 并保留句柄以便取消/健康检查。
7. **文档**：README 与 API 路由基本一致（已核对）；docs/ 目录诚实标注实施状态。建议补一份「安全边界」文档记录威胁模型（本机单用户 + 恶意网页防护）。

---

## 7. 建议实施路线图

### 第一批（1-2 天，安全底线 + 崩溃修复）
1. `PRAGMA foreign_keys=ON` + 缺的索引（§2.1/2.3）
2. Host 校验 + 回环绑定强制 + `/ui/config` 鉴权（§1.1）
3. drive 要求已注册项目（§1.2）
4. quality gate 路径白名单 + 移除 repository_ref 回退（§1.3）
5. RELEASE_APPROVAL 只信确定性门禁（§1.4）
6. git remote 凭据统一脱敏（§1.5）
7. save_and_activate 主机白名单（§1.6）
8. 前端：ui_url 校验 + token 移出 localStorage + 审批对话框降级修复（§5.1-5.4）
9. llm_summary.py:148 畸形 200 响应守卫 + wire 测试（§1.8）
10. watch_worklog：更新成功后才落状态 + 原子写 + 内容哈希检测（§6b）

### 第二批（1 周，正确性/健壮性）
- orchestrator 转移错误检查 + 派发幂等 + 启动清扫/失败升级（§3.1-3.2）
- 补 close_case / retry_repair 动作，让复盘在生产路径触发（§3.3）
- 哈希链补全 + verify_tool_chain + cases 锚定（§2.5）
- 锁外移 git/制品 IO；list_cases 去 N+1（§2.2/2.4）
- AgentScope 派发超时/重试/token 预算（§3.4）
- 测试探测进程组击杀（§4.3）
- watch_worklog 补 snapshot/diff/run_once 单测（§6b）
- scan_code_map SKIP_DIRS 改相对路径匹配（§6b）；skills.py 元组 bug + 表格注入修复（§6b）
- 测试确定性改造：SSE/监控 sleep → Event 等待；restart 测试真重开 DB；抽 tests/_helpers.py（§6b）

### 第三批（1-2 周，性能/工程化）
- watcher 换 watchdog/FSEvents 或目录 mtime 快路径（§4.1）
- drive/code_graph 有界遍历 + 增量哈希（§4.2）
- 前端 SSE 合并刷新 + 差异渲染 + 拆分单文件（§5.5）
- logging 全接入 + ruff + CI + pyproject（§6）
- 迁移 user_version 版本化（§2.6）

---

## 6b. 测试 / 脚本 / 周边代码（重点新发现）

### [HIGH] watch_worklog.py —— 生产变更检测引擎零测试 + 3 个丢变更 bug
`daemon/project_monitor.py:31` 直接复用其 snapshot/diff 引擎，但全仓库**没有任何测试引用它**。已实证的 bug：
- `watch_worklog.py:246, 289-290`：**先保存快照、后跑更新**——updater 失败（CalledProcessError 被 217-219 吞掉）时状态已前移，变更**永远丢失且不重试**。
- `watch_worklog.py:91`：save_state 是裸 write_text（非原子），崩溃写坏后 load_state 静默返回空 → 当作新基线，吞掉真实变更。
- `watch_worklog.py:130-133,149`：只比 mtime_ns+size——**保尺寸改内容的重写检测不到**；同一 tick 两次写入被合并。
- `watch_worklog.py:66-68`：watcher 状态（全量路径+mtime）写在**世界可读的 /tmp/code-defog/**，与 daemon 0700 姿势矛盾。
- `watch_worklog.py:266`：run_forever 启动不加载持久化状态 → 停机期间的变更丢失。
- **修复**：更新成功后才落状态；原子写；检测加内容哈希；状态目录走 `daemon.paths.monitor_state_dir()` 0700；补 snapshot/diff/run_once 单测。

### [HIGH] llm_summary.py:148 畸形 200 响应崩溃端点（见 §1.8）

### [HIGH] 测试自身问题
- `test_daemon.py:2014,2030`：SSE 测试用 `time.sleep(0.3)×2` 等连接 + 监听线程吞异常 → CI 负载下偶发失败。**修复：threading.Event 等待**。
- `test_daemon.py:1103-1203`：名为「survive restart」的测试**从未真正重启**——两个 Orchestrator 共用同一个 SQLite 连接。**修复：关闭并重开 DB 文件**。
- `test_project_monitor_suite.py:176-178,196-198`：2s+3s 真实 sleep 卡时序窗口，慢 CI 会漏事件；改为直接驱动 watcher 循环。
- `test_daemon.py:1828-1983` 五处 `server.shutdown()` 缺 `server_close()`，监听 socket 泄漏到 GC。
- 消费侧授权绑定三分支（store.py:852-867：target_ref/state/pending_action → 409）**零覆盖**；`clear_all` 只清库不清 artifact/sandbox 文件（测试只断言 DB）。
- fixture 重复：~28 份 create_or_find_case 拷贝、8 个文件的 server-start helper 形状不一致 → 抽 `tests/_helpers.py`。

### [MEDIUM] update_worklog.py
- `727 vs 759`：文件读两次，第一次读的内容拼进第二次读的内容 → 并发更新丢行。
- `434,759`：损坏/非 UTF-8 文件直接 Uncaught UnicodeDecodeError。
- `501-510,688-696`：只有 START 无 END 时下次运行整段重复；END 在 START 前抛 ValueError。
- `715`：os.replace 后无父目录 fsync；--note/--phase 重复运行会重复追加时间戳笔记。

### [MEDIUM] scan_code_map.py:58
- SKIP_DIRS 用**完整绝对路径**的 part 匹配——工作区在 `build/`、`dist/`、`env/` 等目录名下的项目会被**整个扫成空**且 exit 0。**修复：用 relative_to(path).parts 匹配**。
- :60 跟随符号链接出扫描根（对不可信目录运行时是任意读面）；escape_cell 不转义反斜杠破坏 round-trip。

### [MEDIUM] skills.py（复盘技能）
- `448-465`：evidence_indexer 的 case 节点输出**字符串化元组**（`(case_id, C-1)`），与 docstring 矛盾，测试只查顶层键故不可见。
- `167-170`：chain_sequence/exit_code 未清洗直接拼进 Markdown 表格行 → **表格注入**绕过（exit_code 未清洗落库，store.py:1200）。
- `220-225 vs 268-362`：docstring 声称 confidence 从不猜测，实际 0.9/1.0/0.5/0.7 是硬编码策略权重。
- `311-313`：`if gate is not None` 把字符串 "false"/"0" 当通过；`391-396`：propose 了不存在的 compliance_checker 技能。

### [MEDIUM] retrospective.py
- `54-72,80-91`：报告制品已落、knowledge 写入失败时，已生成守卫短路 → **部分失败不可恢复**（需 force）。
- `74-75`：case 在读取间隙消失时 `get_case_evidence` 返回 None → skills.py:100 AttributeError。

### [MEDIUM] controlled_repair.py
- `121-122`：patch_ref 确定性 → 失败后重跑同一 Case 撞 exists-guard 且无法恢复（无重跑测试）。
- `121-125`：并发 TOCTOU，输者收裸 FileExistsError；`125` copytree 默认跟随符号链接。

### 依赖与工具链
- requirements.txt：certifi==2026.7.22 为**真实版本**（已核实）；`pytest-asyncio==1.3.0` **未被使用**（零 async 测试）；dashscope/pydantic/pydantic-core 均未被项目代码 import（传递依赖，建议注明原因）。
- **无 pyproject.toml/pytest.ini**：裸 `pytest` 报 ModuleNotFoundError（只有 `python -m pytest` 可用，README 写的是后者所以文档路径没问题，但建议补 pyproject 的 pythonpath）。
- README 全部关键声明（鉴权头、审批哈希、密钥权限、127.0.0.1 绑定、202/409、20s 快速超时、仅超时/非零退出才升级 Case）**逐条核实准确**。
- 测试总体评价：**断言扎实、隔离良好、安全重点突出**（每测试独立 TemporaryDirectory + 全新 StateStore、无真实网络/密钥），无 critical 级问题。

### [补充] 第二轮审出的测试覆盖缺口（全部为「未测」而非产品 bug）
- **[HIGH] 路径包含/穿越从未测试**：`_artifact_target`/`read_artifact` 的 URI 包含检查（store.py:1207-1216, 1272-1277）没有恶意 URI/DB 行用例（如 `../../../etc/passwd`）；`delete_session`（527-534）接受任意已解析绝对路径，仅靠 service token 防护。→ 补恶意 URI 与越权 workspace 测试。
- **[HIGH] 崩溃恢复/并发从未测试**：无并发 ingest 用例（单连接 + RLock）；无 grant 标记已用与状态更新之间的中途失败一致性用例（870-899）；restart 测试未真正重开 DB。
- **[MEDIUM] 鉴权分支缺口**：错误审批密钥 → 403（server.py:333）未测；跨动作 grant 消费（reject_plan 的 token 当 approve_plan 用，store.py:845）未测；401 只测了 GET /api/state 一个端点；未鉴权的 /api/stream、畸形 JSON → 400 未测。
- **[MEDIUM] grant 端到端测试跑真实子进程链**（verification → quality_gate.py，~12 处 urlopen(timeout=10)），耗时且依赖环境 → 建议标记为集成测试。
- **[LOW]** db_bytes>=0 恒真（521 出错返回 0）；chain_hash 只断言非空不复算；retention 断言在剪枝严重错误时也能过；wrong-patch 构造用 str.replace 静默 no-op；WAL synchronous=NORMAL 崩溃窗口未注；PRUNE_EVERY_INGESTS/retention 边界无测试。
- **[LOW]** controlled_repair 分支无测试（沙箱路径逃逸拒绝、源文件前置条件被篡改、符号链接、重跑幂等）；test_retrospective 只覆盖 happy path（escalation/拒绝审批/失败 Agent 分支与 _parse_output_ref 解包无直接测试）。

---

## 8. 明确的优点（建议保留）

- 三层凭证隔离 + hmac.compare_digest + 一次性 approval token 的消费是事务性的、锁串行化的。
- 制品路径逃逸防护 + 哈希校验、原子写入（mkstemp+fsync+os.replace）、0600/0700 权限处理。
- 前端整体无 innerHTML 数据注入、epoch 竞态防护、SSE 心跳/清理正确。
- LLM 密钥处理（certifi TLS、host 白名单、public_config 脱敏）与 AgentTeams 诚实姿态（fail-closed preflight）。
- 确定性优先：统计/图表全部来自 SQLite 聚合，LLM 只做叙述。

*（详细行号与修复建议见 review_reports/ 下各模块报告。）*

---

## 9. 实施状态（2026-08-14 更新）

> 全部改动已落地并通过 `pytest tests/ demo_target/`（**269 passed + 4 subtests**，较审查基线新增 26 项回归测试）。

### ✅ 第一批（安全底线 + 崩溃修复）— 全部完成
1. `PRAGMA foreign_keys=ON` + `busy_timeout=5000` + 5 个缺失索引（store.py）；5 个测试改为先建 case 再插子行
2. Host 头校验（server.py/dashboard.py，拒绝 DNS rebinding）+ serve.py 非回环绑定拒绝（`--allow-non-loopback` 显式覆盖）+ `/ui/config` Sec-Fetch-Site 加固 + CORS 全端收窄为回环/null 源回显（新增 tests/test_loopback_guard.py，5 项）
3. drive POST 内置路径要求已注册监控项目（403），注入的 drive_runner 测试契约豁免（新增 2 项测试）
4. quality gate 只允许 Store 沙箱根 / demo_target 目录；彻底移除 repository_ref 执行回退（verification.py + `_gate_target_allowed`）
5. REPAIRING 只接受 `repair_mode=demo_sandbox` 的受控修复（patch_ref/sandbox_ref 需在沙箱根内）；VERIFYING 门禁通过但无 sandbox_ref → 停留 VERIFYING 等待人工，不再进入 RELEASE_APPROVAL；LLM 自述 patch_ref 永不落库（新增 2 项测试）
6. git remote 凭据统一脱敏 `redact_remote_url`（repo_identity/drive/project_discovery，http(s) userinfo 剥离）（新增 5 项测试）
7. `save_and_activate` 主机白名单：改到白名单外主机且未显式给 key → 拒绝（新增 1 项测试）
8. 前端：`serviceUrlSafe`（http/https + 回环校验）、token 移出 localStorage（sessionStorage）、审批对话框 Enter 提交/密钥清空/close 清理/无 dialog 时 fail-closed prompt、case status selector 白名单
9. `_post_chat` 畸形 200 响应守卫（ValueError，调用方 except 补 ValueError）；新增 wire 级测试（3 个畸形形状）
10. watch_worklog：状态后置保存（失败不推进快照）、原子写 + 0600、ctime/inode 扩展检测、状态目录移出 /tmp 改 per-user 0700（新增 4 项引擎测试）

### ✅ 第二批（正确性/健壮性）— 核心项完成
- orchestrator：`transition_case` error 结果检查（防并发 TypeError）、按 case 的派发锁（防双派发）
- 崩溃恢复 `recover_interrupted()`：重启时把 running 的 agent_runs 标 failed、active 状态 case 升级 ESCALATED（serve.py 启动时调用；新增测试）
- 新增 `close_case` / `retry_repair` 动作（走状态机校验；retry 自动 resume 修复 Agent；新增 HTTP 级测试）
- 哈希链补全：canonical 覆盖全部存储列（含 command_template/policy_version/approval_id/result_ref）、exit_code 归一化（None→-1）、新增 `verify_tool_chain()` 校验器 + cases.chain_anchor 篡改锚点（新增 2 项测试）
- `scan_code_map` SKIP_DIRS 改相对路径匹配（工作区在 build/dist/env 下不再误扫空；新增测试）
- skills.py：Markdown 表格注入修复（chain_sequence/exit_code 清洗）、gate 仅认 bool、evidence_indexer 输出 key/value 对象而非字符串化元组
- controlled_repair：重跑幂等（已存在且哈希一致 → 返回既有沙箱，不再报错）
- retrospective：evidence None 防护；知识写入失败时回滚报告/清单制品，重试可再生（新增测试）；HTTP 处理器捕获内部异常返回 500 JSON
- 测试探测 `start_new_session` + 超时击杀整个进程组；AgentScope 派发硬超时 `asyncio.wait_for`（120s）

### 🟡 第三批（性能/工程化）— 部分完成
- ✅ pyproject.toml（裸 `pytest` 可用）+ 移除未用的 pytest-asyncio + GitHub Actions CI（.github/workflows/ci.yml）
- ✅ stdlib logging 接入 serve.py（日志落 `paths.log_path`）+ 线程异常 excepthook + retrospective/monitor 失败落日志
- ✅ watcher 热路径优化：逐文件 `resolve()` 移除（快路径名字比较）、snapshot 保持全量正确性（含 ctime/inode 重写检测）、>20k 文件告警
- ✅ 前端 SSE 合并：case 事件防抖 500ms、review_task 事件 150ms 合并渲染、evidence 字节级去重（未变不重建视图）、projects 8s TTL 缓存（手动刷新强制）
- ✅ code_graph 不再常驻源码全文（只留符号/导入/行数/哈希，内存从 ~128MB 降到符号级）
- ✅ 迁移 `PRAGMA user_version = 2` + DevLoop ALTER 事务化
- ✅ 测试工程：新增 `tests/_helpers.py`（start_server/seed_case 共享，5 个测试文件迁移）、SSE 测试 sleep(0.3)×2 → threading.Event（修掉 flaky 竞态）、补全所有 server_close()
- ⏳ 下迭代（可选）：watcher 换 OS 事件监听（需新增依赖）、前端 CSS/JS 拆分、monitor 测试 2s+3s 时序改造

### 新增/修改文件清单
- 核心：`daemon/store.py`、`daemon/server.py`、`daemon/serve.py`、`daemon/dashboard.py`、`daemon/drive.py`、`daemon/llm_providers.py`、`daemon/llm_summary.py`、`daemon/code_semantics.py`、`daemon/repo_identity.py`、`daemon/project_discovery.py`、`daemon/project_monitor.py`、`agent_runtime/orchestrator.py`、`agent_runtime/teams_adapter.py`、`agents/verification.py`、`tools/controlled_repair.py`、`retrospective/retrospective.py`、`retrospective/skills.py`、`scripts/watch_worklog.py`、`scripts/scan_code_map.py`、`web/index.html`
- 测试：`tests/test_loopback_guard.py`（新）、`tests/test_daemon.py`、`tests/test_drive.py`、`tests/test_core_boundaries.py`、`tests/test_retrospective.py`、`tests/test_llm_providers.py`、`tests/test_project_summary.py`、`tests/test_scripts.py`、`tests/test_project_monitor_suite.py`
- 工程：`pyproject.toml`（新）、`.github/workflows/ci.yml`（新）、`requirements.txt`、`tests/_helpers.py`（新）
