# ReAct Agent Loop：真实踩坑与红线

## 1. 记录口径

- “真实踩坑”只记录来源项目中已有事故文档、测试或本次可复现实证。
- 事故中的客户名、原始用户内容、会话 ID、Job ID 和 Trace ID不复制到 CBB。
- 当前结论基于 `Corevo Platform upstream/dev@c7e869b54139e89743f8ae45c09e6fc2aef68320`。
- 每条经验只提炼可迁移结论；具体实现仍要回到来源索引核验。

## 2. 真实踩坑

### PIT-001：模型陷入同一工具和参数的重复循环

- 发生时间：2026-06
- 症状：多个生产 trace 中，同一工具和完全相同参数连续执行，但任务没有获得新信息。
- 根因：
  - 循环原先缺少跨 iteration 的 action 保护；
  - 失败结果、工具发现和模型策略共同放大重复；
  - 不同副作用工具没有差异化阈值。
- 修复：
  - 用真实工具名和规范化参数生成 action key；
  - 根据只读/写入、成功/失败、可重试性设置阈值；
  - 软拦截后把可理解结果交回模型，不直接杀死整个任务。
- 预防测试：
  - 分页参数不被误认为同一 action；
  - 只读成功、写成功、可重试失败和不可重试失败分别覆盖；
  - 写操作屏障前后不错误去重。
- 证据：
  - `docs/bug/20260617-agent-tool-loop-guard.md`
  - `kernel/tests/agent/test_react_tools_cache.py`

### PIT-002：重复调用不仅是循环问题，也可能是工具发现问题

- 发生时间：2026-06-10
- 严重程度：P1
- 症状：政策类查询反复选择同一工具，或选择语义相近但错误的工具。
- 根因：
  - 循环缺少同参只读缓存；
  - Tool Search 再次返回已经加载的工具；
  - 正确工具描述和标签不足；
  - 分页/结果不完整诱导模型重复。
- 修复：
  - 单 run 只读同参缓存；
  - Tool Search 默认过滤已加载工具，必要时回退；
  - 改善工具描述、别名和能力标签；
  - 明确 ID 等结果字段该传给哪个下游工具。
- 结论：不能只在 ReAct 层加“禁止重复”就结束；需要同时检查工具 schema、召回和结果语义。
- 证据：`docs/bug/20260610-policy-tool-duplicate-calls.md`

### PIT-003：会话状态放在进程内存，跨 Pod 后工作流失忆

- 发生时间：2026-05-12
- 症状：Plan 模式中等待用户后，新请求落到另一个 Kernel Pod；新 Pod 认为当前不在 Plan 模式。
- 根因：会话级状态存进程内全局字典，没有共享权威状态源。
- 修复：Redis 成为多 Pod 权威状态源，本地内存仅作热缓存/降级，并覆盖跨 Pod 恢复测试。
- 预防测试：
  - 在不同 manager/Pod 模拟间恢复；
  - Redis 无 key 时覆盖本地旧缓存；
  - 等待用户、计划审批和退出流程端到端验证。
- 结论：任何跨请求、跨 Pod、可恢复的 Interrupt 状态都不能只存在循环对象内。
- 证据：`docs/bug/20260512-plan模式状态跨Pod丢失.md`

### PIT-004：安全门禁拒绝正确，但错误语义丢失

- 发生时间：2026-06-23
- 症状：输入被任务意图安全门禁拦截后，前端只显示“Kernel 未知失败”。
- 根因：`ReactAgent.run()` 在调用模型前提前返回，但没有写结构化 runtime error source。
- 修复：把拒绝映射为稳定 policy 错误，保留 source component、error type 和用户可见信息。
- 预防测试：
  - Agent 层验证 metadata；
  - Job 层验证映射到稳定错误码；
  - 所有 early return 都通过统一终态构造器。
- 结论：拒绝执行本身和“如何收敛成可解释终态”必须同时设计。
- 证据：`docs/bug/20260623-task-intent-guard-policy-error.md`

### PIT-005：外层终止时，开放工具没有 result

- 发生时间：2026-06-29
- 症状：Langfuse 能看到工具异常或长耗时，但 SSE 与 `job.metrics.tool_calls` 没有对应 timeout/失败。
- 根因：外层 Job timeout/cancel/fail 截断正在运行的工具，只发送 execution 终态，没有补发开放工具的 `tool.call.result`。
- 修复：
  - 记录 started 和 finished 工具；
  - 外层终态前对开放工具合成 timeout/cancelled/failed 结果；
  - 补偿保持幂等。
- 预防测试：重复执行补偿回调只产生一个工具结果。
- 结论：循环内的 try/except 无法覆盖所有终止；外层 Job Runner 必须拥有开放步骤补偿。
- 证据：
  - `docs/bug/20260629-tool-timeout-sse-metrics.md`
  - `test/unit-test/services/test_task_runner_terminal_tools.py`

### PIT-006：可丢 delta 阻塞权威终态

- 发生时间：2026-04-25
- 严重程度：P1
- 症状：用户已经看到完整 Agent 输出，但后台 Job 最终显示失败，历史 assistant 消息也没有完成落库。
- 根因：发送终态前同步等待无上界的 delta `flush_all()`；下游阻塞后，权威终态永远没有发出。
- 修复方向：
  - delta flush 只能 bounded best-effort；
  - 权威终态有独立有限 timeout/retry；
  - Platform repair 写入明确错误并尽力完成消息收敛。
- 预防测试：成功、失败、timeout 和取消四条出口在 delta flush 挂起时仍能发送终态。
- 结论：实时体验数据可以降级，权威终态不能被它反向依赖。
- 证据：`docs/bug/20260425-任务成功但会话管理显示失败-终态事件未发出.md`

### PIT-007：半初始化测试对象随构造边界变化而漂移

- 发现时间：2026-07-29 本次采集
- 症状：`kernel/tests/agent/test_user_waiting_tools.py` 的 3 个测试失败。
- 触发条件：测试使用 `ReactAgent.__new__()` 并手工填充大量字段，没有设置后来新增的 `_delegation_runtime_policy`。
- 影响：等待用户和空响应的目标断言尚未执行，就在 `_run_loop` 初始化阶段报 `AttributeError`。
- 根因：
  - 生产类构造状态过多；
  - 测试绕过公开构造器；
  - 运行依赖没有封装成显式 runtime context 或 factory。
- 建议修复：
  - 测试优先使用正式构造器或集中测试工厂；
  - 把循环必需依赖收敛为显式不可变上下文；
  - 增加“最小可构造”契约测试。
- 证据：本次测试记录见 `source-map.md`。

### PIT-008：Skill 方法论可见，但配套 Tool 没有授权

- 发生时间：2026-07-16
- 严重程度：P1
- 症状：管理员请求创建 Skill 时，模型能看到 Skill Creator 方法论，却找不到需要的八个
  生命周期 Tool。
- 根因：
  - Skill 和 Tool 实现已经发布，但既有数据库 Agent 策略没有同步加入配套 Tool；
  - 历史发布脚本仍持有静态工具清单，形成第二个可写事实源；
  - 原有链路没有校验“已启用 Skill 的 required companion Tool 必须全部授权”；
  - 只补数据库授权后，direct Tool Search 首轮仍不暴露配套 Tool，真实回归依旧失败。
- 修复：
  - 数据库策略继续作为 Tool 授权唯一事实源；
  - Platform 校验 Skill/companion 完整性，缺项直接失败而非运行时扩权；
  - Kernel 直接暴露“已授权且所属 Skill 已启用”的配套 Tool；
  - 补充 forward backfill、空库初始化和回归验证。
- 预防测试：
  - 每个 enabled Skill 的 required companion Tool 完整性；
  - `skill_ref` 不能绕过数据库授权；
  - companion Tool 只在 Skill 已启用且用户角色允许时可见；
  - 真实 Job 能执行第一项必要 Tool。
- 结论：Prompt、Skill 和 Tool 实现三者都正确，整体功能仍可能处于不可用的半启用状态。
- 证据：
  - `docs/bug/20260716-skill-creator-tools-not-authorized.md`
  - `docs/agent/tool-search-runtime-policy.md`
  - `kernel/tests/agent/test_react_tools_cache.py`
  - `kernel/tests/subagents/test_subagent_policy.py`

### PIT-009：Skill description 被错误解析并反向损坏源文件

- 发生时间：2026-07-28
- 症状：上传的 Skill 在 Agent Prompt 中只有路径，没有正常 description；数据库值变成单个
  YAML 块标量标记 `>`。
- 根因：
  - 前端逐行截取 frontmatter，不支持 YAML 多行块；
  - Platform 信任前端派生值，没有从原始 ZIP 重新安全解析；
  - 多套独立解析器行为不一致；
  - runtime-assets 契约一度漏传 description；
  - 错误数据库值又被用于重写对象存储中的 `SKILL.md`。
- 修复：
  - 服务端统一安全 YAML codec；
  - 上传包内 `SKILL.md` 成为 description 权威事实；
  - runtime-assets 到 Kernel 完整传递 description；
  - Platform description 优先于损坏的存储回退值。
- 预防测试：
  - `>`、`|`、引号、BOM、LF/CRLF；
  - 上传预览、数据库、对象存储、runtime-assets 和 Prompt 五处一致；
  - 错误前端派生值不能覆盖源包。
- 结论：Skill 路由质量依赖元数据端到端完整性；Prompt Builder 单测通过不代表链路正确。
- 证据：
  - `docs/bug/20260728-skill-description-frontmatter-runtime-assets.md`
  - `kernel/tests/services/test_runtime_assets_source.py`

### PIT-010：Prompt 给出了 Skill 路径，但沙箱实际读取 404

- 发生时间：2026-04-18；2026-05-22 跨仓库迁移后回归
- 症状：模型按 Prompt 调用 `read("skills/<name>/SKILL.md")`，但返回文件不存在或路径越权。
- 根因：
  - Prompt 使用统一 `skills/` 路径，PathMapping 却指向空的个人技能目录；
  - 合并层使用 symlink，读路径校验跟随 symlink 后认为越界；
  - 仓库迁移只搬了主线代码，历史修复 patch 没有同步。
- 修复：映射统一指向会话技能合并层；读写使用不同的 symlink 安全策略；迁移后重新应用修复。
- 预防测试：
  - 在真实沙箱文件视图读取 builtin/tenant Skill；
  - Prompt 给出的每个 Skill 路径做端到端 read；
  - 跨仓库/路径重构使用历史事故清单回归。
- 结论：Prompt 路径、资产物化、PathGuard 和 Tool 执行必须作为一个契约验证。
- 证据：`docs/bug/20260418-skill-read-404-pathmapping-symlink.md`

### PIT-011：运行模式变化后，Skill 仍教模型调用不存在的动态分发器

- 发生条件：关闭 `invoke_dynamic_tool`，但 Skill Markdown 仍包含强制
  `tool_search → invoke_dynamic_tool` 的旧说明。
- 症状：模型按 Skill 调用未暴露 Tool，或重复 Tool Search 但无法进入下一步。
- 来源修复：读取/预展开 Skill 时，根据 direct/dynamic/no-search 能力投影正文，并有定向测试。
- 剩余风险：当前实现依赖已知短语替换，不能证明任意自定义 Skill 的自由文本都会正确改写。
- 迁移建议：用结构化 capability 声明生成运行时说明，并建立 Prompt/Tool Schema 一致性测试。
- 证据：
  - `kernel/core/prompts/system/runtime_projection.py`
  - `kernel/core/prompts/system/tool_policy.py`
  - `kernel/tests/prompts/test_tool_policy_runtime.py`

## 3. 当前证据矛盾与剩余风险

### GAP-001：Skill description “净化”强度与安全文档不完全一致

- 安全文档把自定义 Tool/Skill description 注入列为攻击面，并描述“长度限制 + 注入模式检测”。
- 当前 Tool description 实现包含可疑模式检测；当前 Skill description loader 只做 1,000 字符截断。
- `docs/security/README.md` 将对应安全项总体标记为已修复，但现有代码不足以证明 Skill
  description 已完成同等级检测。
- CBB 结论：只能把“Skill description 有长度上限”写成当前事实；不能把“已充分防 Prompt
  Injection”写成已验证事实。需要恶意 Skill 元数据/正文的对抗测试和维护者复核。

## 4. 红线

### RED-001：工具必须“双门禁”

- 禁止行为：只要模型输出了工具名就执行。
- 适用范围：所有静态、动态和别名工具。
- 后果：越权执行、未授权数据访问或高风险副作用。
- 强制措施：
  - 模型可见性快照；
  - 执行时再次校验真实目标工具和本次授权；
  - 未声明权限时默认拒绝。
- 验证：未暴露、禁用、未知、动态未加载和未授权工具测试。

### RED-002：模型和工具内容不可信

- 禁止行为：让模型参数覆盖 actor/tenant/role/allowed tools，或把工具输出当系统指令。
- 后果：跨租户、提示注入和策略绕过。
- 强制措施：可信运行时注入身份；不可信内容来源标记；服务端权限检查。
- 验证：伪造身份、跨租户和恶意 tool output 测试。

### RED-003：tool call/result 不能失配

- 禁止行为：进入下一轮模型前留下孤儿 call，或用错误 call ID 回填结果。
- 后果：Provider 协议错误、上下文污染、错误归因和不可重放。
- 强制措施：唯一 call ID、每个 accepted call 一个结构化结果、checkpoint 完整性检查。
- 验证：多工具、拒绝、未知、timeout、取消和恢复测试。

### RED-004：副作用未知时必须保守

- 禁止行为：自动重试、缓存、去重合并或并行执行副作用未知/非幂等工具。
- 后果：重复写入、重复扣费、数据损坏和乱序。
- 强制措施：`side_effect`、`idempotency`、`parallel_safe` 显式元数据；未知默认最严格。
- 验证：写操作屏障、重复提交和故障重试测试。

### RED-005：所有等待必须有上界

- 禁止行为：无上限循环、LLM 等待、工具执行、flush、遥测或清理。
- 后果：资源泄漏、Job 永久 RUNNING、无法取消和雪崩。
- 强制措施：统一 deadline；分层 timeout 不得超过剩余预算。
- 验证：每个外部依赖挂起时均能在上界内收敛。

### RED-006：取消不能被吞

- 禁止行为：`except Exception`/降级逻辑把外部取消转成普通工具失败后继续。
- 后果：用户取消无效、worker 无法关闭、旧 lease 继续产生副作用。
- 强制措施：单独传播取消；外层识别取消来源；finally 只做有界清理。
- 验证：模型阶段、工具阶段、持久化阶段和终态阶段分别取消。

### RED-007：失败不能伪装成成功

- 禁止行为：最大迭代、空响应、安全拒绝、不可恢复错误或缺失终态映射为成功。
- 后果：用户被误导、计费和成功率失真、事故不可定位。
- 强制措施：显式状态机、统一终态构造、结构化错误来源。
- 验证：每条 early return 和异常出口的状态/错误码测试。

### RED-008：权威终态优先于 best-effort 工作

- 禁止行为：让可丢 delta、遥测、索引、文件同步或清理无限阻塞终态。
- 后果：执行实际完成但系统永远无法收敛。
- 强制措施：bounded best-effort、独立终态投递、持久终态事实、repair。
- 验证：所有 best-effort 依赖挂起时的终态可靠性测试。

### RED-009：开放步骤必须在终态前闭合

- 禁止行为：发送 execution 终态时仍存在没有 result 的 started tool/step。
- 后果：SSE、metrics、trace 和审计事实不一致。
- 强制措施：外层维护开放集合并执行幂等补偿。
- 验证：timeout/cancel/fail 重复补偿测试。

### RED-010：跨 run 与跨租户状态隔离

- 禁止行为：复用可变上下文、工具缓存、重复保护状态、checkpoint 或权限快照。
- 后果：数据泄漏、错误缓存命中和越权。
- 强制措施：run-scoped state；tenant/run 进入缓存键；恢复时验证 owner。
- 验证：并发多租户和旧 checkpoint 注入测试。

### RED-011：Skill 与 Tool Search 不能授予权限

- 禁止行为：根据 Skill 已启用、`skill_ref`、Prompt 文本或搜索命中，自动把 Tool 加入授权集合。
- 后果：越权数据访问、高风险副作用和租户隔离失效。
- 强制措施：数据库/可信策略形成不可变授权上限；Skill 和搜索结果只能在集合内求交。
- 验证：全局 Registry 有 Tool 但 Agent 策略无 Tool、Skill 启用但 Tool 未授权、搜索显式写出
  未授权 Tool 名三类拒绝测试。

### RED-012：禁止 Skill 半启用

- 禁止行为：required companion Tool 缺失时仍把 Skill 方法论注入 Prompt。
- 后果：模型无进展循环、伪造完成、生产功能随机失效。
- 强制措施：发布与 run 准备阶段完整性校验；缺项 fail closed 并给出结构化配置错误。
- 验证：每个 required companion 缺一项、禁用一项、角色过滤一项的测试。

### RED-013：Prompt 必须与真实 Tool 模式一致

- 禁止行为：Prompt/Skill 要求调用本轮不存在的 Tool，或把 direct/dynamic 两种协议混用。
- 后果：未暴露调用、重复 Tool Search、额外轮次和不可恢复失败。
- 强制措施：Prompt 从同一份 RunSnapshot 投影；自由文本能力引用建立静态/运行时一致性检查。
- 验证：direct、dynamic、no-search、text-only、子 Agent 五类 Prompt/Schema 快照测试。

### RED-014：后续状态不能追溯性授权旧响应

- 禁止行为：Tool Search 加载 Tool 后，用新工具集合执行加载前模型响应中的未暴露 Tool Call。
- 后果：绕过本轮模型可见性审计，使事件与执行权限无法重放。
- 强制措施：每个模型响应绑定不可变 `ModelTurnView`；执行只按该视图校验。
- 验证：direct 模式搜索前旧 turn 拒绝、搜索后新 turn 允许。

### RED-015：旧内容不能恢复已撤销能力

- 禁止行为：因为历史对话含 Skill 正文、旧 Tool Schema 或 checkpoint loaded state，就恢复已撤销
  Skill/Tool 权限。
- 后果：撤权不生效、跨版本策略漂移和安全事故。
- 强制措施：恢复时重新与当前可信授权求交，并验证资产版本、owner 和 Hash。
- 验证：撤销后恢复、任务切换、跨租户旧 checkpoint 注入测试。

### RED-016：自定义 Skill 内容不能成为安全事实

- 禁止行为：从 Skill description/body 读取 actor、tenant、role、allowed tools 或系统安全例外。
- 后果：持久 Prompt Injection、权限放大和策略覆盖。
- 强制措施：Skill 来源审查与版本化；内容边界标记；身份/授权仅由服务端上下文注入和执行。
- 验证：恶意 description/body、Tool 输出和 references 的对抗测试。

## 5. AI 冲突处理

如果目标项目要求触碰红线：

1. 停止相关设计或实现；
2. 指出红线编号和具体冲突；
3. 说明安全、数据或终态后果；
4. 给出不触碰红线的替代方案；
5. 等待有权负责人明确决策；
6. 不得自行把红线降级为“建议”。
