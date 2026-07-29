# ReAct Agent Loop 子功能域

## 元数据

- 状态：待验证
- 功能负责人：暂无
- 首次验证日期：暂无
- 最近复核日期：2026-07-29
- 已验证版本/环境：
  - 来源实现：Corevo Platform `upstream/dev@c7e869b54139e89743f8ae45c09e6fc2aef68320`
  - 来源仓库在采集时工作区干净，`HEAD`、本地 `upstream/dev` 和远端 `refs/heads/dev` 一致
  - 来源要求 Python 3.11+；本次选定测试使用来源仓库 `.venv`
  - CBB 示例使用 Python 标准库、不依赖 Moss；循环与 Prompt/Skill/Tool 关系共 23 个测试
- 来源索引：[evidence/source-map.md](evidence/source-map.md)
- 待验证原因：
  - CBB 提炼结果尚未经过独立复核；
  - 来源仓库选定测试中有 3 个测试因绕过构造函数创建 `ReactAgent`、未补新增运行时字段而失败，详见来源索引。
  - 来源 `test_registry_subagent_assets.py` 还因导入已不存在的资产中间件符号而在测试收集阶段
    失败，导致该模块的 Skill 资产回归当前无法执行。

> 本目录中的 `react` 指 **ReAct（Reason + Act）Agent 循环**，不是 React 前端框架。

## 1. 功能目标

ReAct Agent Loop 把一次用户任务组织成有界的“模型判断 → 工具行动 → 结果观察 → 再判断”循环，直到得到可接受的最终回答、等待外部输入、被取消、失败或达到资源上限。

本功能域要让 AI 在新项目中设计一个边界清晰的循环内核，而不是复制 Moss 当前六千余行、包含大量产品逻辑的 `ReactAgent` 类。

一句话定义：

> ReAct 循环负责可靠地推进模型轮次和工具观察；身份、权限、持久化、沙箱、事件、子 Agent 和外层 Job 通过明确边界接入。

## 2. 适用场景与非目标

### 适用场景

- 模型需要多轮调用工具才能完成任务；
- 需要记录每轮模型输出、工具调用、工具结果和终止原因；
- 需要对迭代次数、总时限、单次模型调用和单次工具调用设置边界；
- 需要支持取消、用户中断、上下文压缩、错误降级和运行观测；
- 需要把同一个循环内核用于主 Agent、受限子 Agent 或后台 Agent Run。

### 非目标

本目录不直接定义：

- 某个 LLM Provider 的流式解析协议；
- 完整 Prompt 模板平台、Skill 发布系统、Memory、RAG 或 Tool Search 排序实现；
- 具体工具业务逻辑和工具目录；
- Job 队列、数据库 Schema、消息队列或前端 SSE 协议；
- 沙箱、文件存储和租户权限的具体实现；
- 子 Agent 的调度、递归和报告协议；
- React 前端页面或组件开发规范。

这些能力会影响循环，因此本目录必须说明接口、轮次关系和红线，但不应把它们全部实现进循环类。
Prompt、Skill、Tool 与循环的详细关系见
[PROMPT_SKILL_TOOL_FLOW.md](PROMPT_SKILL_TOOL_FLOW.md)。

## 3. 边界与依赖

详细说明见 [BOUNDARIES.md](BOUNDARIES.md)。

### 循环拥有的职责

- 建立单次运行状态和轮次序号；
- 在每轮调用模型，并解释“文本 / 工具调用 / 空响应”三类输出；
- 把模型产生的工具调用交给工具执行边界；
- 为每个 `tool_call_id` 写回对应 observation；
- 判断循环内终态并生成结构化结果；
- 保证迭代、模型调用和工具调用有明确上限；
- 将取消信号及时向外传播，不吞掉异步任务取消。

### 循环依赖但不拥有的职责

- **外层 Job Runner**：全局 timeout、权威终态、持久化、计费、事件投递和补偿；
- **Model Adapter**：Provider 格式、流式增量、tool call 拼装和 token usage；
- **Context Manager**：消息历史、上下文预算、压缩、checkpoint 和恢复；
- **Prompt Builder**：按运行时能力组装系统规则、Agent 指令、Skill 索引和环境上下文；
- **Skill Runtime**：发布版本、元数据、正文、渐进式加载、资源投影和内容 Hash；
- **Tool Registry / Executor**：schema、参数校验、执行、side effect、timeout 和并行安全；
- **Tool Search**：在本 run 已授权候选内排序和加载工具，不负责授予权限；
- **Policy / Security**：本次可见工具、用户与租户授权、计划模式和高风险操作审批；
- **Event Sink**：事件 ID、顺序、重放、started/result 配对和外部投递；
- **Sandbox / Artifact**：资源隔离、文件可见性、同步、引用和释放；
- **Subagent Runtime**：递归限制、独立状态、父子取消、结果和 usage 汇总。

### 信任边界

- 模型输出始终是不可信输入，包括工具名、参数、文本和终止声明。
- 工具结果也可能包含不可信外部内容，不能自动提升为系统指令。
- Skill description 和自定义 Skill 正文是模型指导内容，不是授权事实，且仍是 Prompt Injection
  攻击面。
- `allowed_tools`、身份、租户和权限上下文只能来自可信运行时，不能由模型填写。
- Job、execution、step 和 tool call 标识由运行时生成或校验，不能依赖模型保证唯一。

## 4. 核心模型与不变量

### 核心模型

- `RunContext`：本次运行冻结的身份、租户、授权、模型和运行参数快照；
- `RunState`：当前状态、轮次、资源用量、错误来源和终止原因；
- `Iteration`：一轮模型输出及其引发的全部工具结果；
- `ModelTurn`：正文、工具调用、usage 和 provider 终止原因；
- `ToolRequest`：`call_id + tool_name + arguments`；
- `ToolOutcome`：成功值或结构化错误；
- `RunAssetSnapshot`：本 run 冻结的 Prompt/Skill/Tool 版本与授权集合；
- `SkillProjection`：按本轮真实工具和委派能力投影后的 Skill 正文；
- `ModelTurnView`：某次模型调用真正收到的消息与 Tool Schema 快照；
- `RunResult`：循环输出和完整状态快照；
- `Interrupt`：等待用户选择、计划审批或外部恢复的非完成终态。

### 必须保持的不变量

1. 一个 `RunState`、缓存和重复调用保护状态只属于一个 run，不能跨 Job 或租户共享。
2. 一轮模型输出只要包含工具调用，就不能因为同时有正文而直接判定完成。
3. 下一次模型调用前，每个已接受的 `tool_call_id` 必须有且只有一个对应工具结果。
4. 工具结果必须使用原始 `tool_call_id` 配对；未知工具和拒绝调用也要返回结构化 observation。
5. 模型只能看到本次运行允许暴露的工具；模型“知道工具名”不等于拥有执行权限。
6. 工具执行前必须完成工具存在性、暴露范围、参数和权限检查。
7. 没有显式声明副作用与并行安全的工具，按“副作用未知、不可并行、不可缓存”处理。
8. 总迭代数、总运行时间、模型调用时间和工具调用时间都有上界。
9. 空响应、达到迭代上限、策略拒绝和非重试错误不能伪装成成功。
10. `asyncio.CancelledError` 等外部取消信号不能被普通异常处理吞掉。
11. started 类运行事实必须最终闭合为 result/cancelled/timeout/failed；外层终态前要补齐仍开放的步骤。
12. 终态一旦提交不可被后到的 delta、重试或清理回调反向改写。
13. 上下文压缩和恢复不能破坏 assistant tool call 与 tool result 的配对关系。
14. 同一 run 使用启动时冻结的模型与工具/权限快照；运行途中默认不热切换。
15. Prompt、Skill、Tool Search 和模型输出都不能扩大服务端 Tool 授权集合。
16. Prompt 只能列出本 run 已启用的 Skill，Skill 元数据和正文必须可追溯到精确版本或 Hash。
17. Skill 正文要求的 Tool 调用方式必须按真实运行能力投影，不能教模型调用不存在的路径。
18. 已启用 Skill 的 required companion Tool 缺失时必须 fail closed，不能形成“方法论可见、
    工具不可用”的半启用状态。
19. Tool Search 只能从本 run 已授权候选中加载；direct 模式新工具从下一次模型调用开始可见。
20. 执行模型 Tool Call 时按产生响应的 `ModelTurnView` 校验，不能使用后来扩大的工具集合。
21. 旧对话、旧 checkpoint 或旧 Skill 正文不能恢复已撤销的 Skill/Tool 权限。

## 5. 已验证设计

以下结论有当前源码、测试或事故修复证据支持；“推荐”内容是从证据提炼的可迁移设计，不代表 Moss 当前已经完全按该形态重构。

### 5.1 小内核 + 生命周期边界

Moss 当前主循环已经把取消、上下文压缩、模型错误重试、过渡性回复拦截、内容锚定、工具错误、安全、沙箱和持久化拆到 middleware 生命周期中。可迁移的关键不是 middleware 类名，而是八个稳定时点：

- `before_run / after_run`
- `before_model / wrap_model / after_model`
- `before_tool / wrap_tool / after_tool`

推荐让循环只控制状态迁移和调用顺序，把安全、上下文、资源、重试和观测作为边界策略接入。

### 5.2 模型响应的三分支

- 有工具调用：记录 assistant tool calls，执行工具，写回 observation，继续循环；
- 无工具调用且有正文：进入终止校验，通过后完成；
- 既无工具调用也无正文：有界纠正，耗尽后失败。

Moss 已有“空响应不能生成假成功”的回归测试，也对“有正文且同时有工具调用”保留工具执行语义。

### 5.3 工具调用采用防御性执行链

推荐顺序：

1. 校验工具是否曾暴露给本轮模型；
2. 解析真实工具名，避免动态分发外壳绕过策略；
3. 检查运行时禁用和业务模式限制；
4. 校验与归一化参数；
5. 权限、安全和配额检查；
6. 重复调用保护；
7. 在单工具 timeout 内执行；
8. 把异常转换成结构化结果；
9. 记录 tool result、事件和回调。

任何短路都仍要产生与 `call_id` 配对的 observation。

### 5.4 默认串行，显式安全才并行

Moss 只把连续且显式 `parallel_safe` 的工具组成并行组，并让写操作形成顺序屏障。迁移时不能根据“都是同一轮 tool calls”就默认并发。

### 5.5 错误先分类，再决定重试

- 网络、限流或部分 timeout 可以在工具/模型适配层透明重试；
- 鉴权、配额、内容安全、参数错误等决定性失败不应盲目重复；
- 工具失败作为结构化 observation 返回模型，允许换方案；
- 运行时失败必须保存结构化来源，供外层映射成稳定错误码。

### 5.6 终态判断必须是协议，不是关键词

Moss 当前仍保留终止关键词，同时对无工具正文、过渡性声明、未完成 TODO、子 Agent 结构化输出、待回收异步子任务和等待用户输入增加额外门禁。

推荐的新项目不要只依赖“任务完成”等文本关键词，应使用：

- 是否存在未闭合工具或异步任务；
- 是否存在未完成计划/TODO；
- 输出是否满足结构契约；
- 是否触发等待用户/审批的 Interrupt；
- 任务验收器是否通过。

### 5.7 外层运行时拥有权威终态

Agent Loop 返回结果不等于 Job 已可靠完成。外层必须：

- 补齐仍开放的工具结果；
- 收敛持久化状态；
- 发送唯一权威终态；
- 保证终态不被 best-effort delta flush、遥测或文件同步无限阻塞；
- 在投递失败时留下可修复事实。

### 5.8 Prompt、Skill 与 Tool 是三条边界，不是一段大提示词

来源实现的主 Agent 先把已启用 Skill 的名称、description 和读取路径放入 system prompt；模型
明确匹配时通过 `read` 加载正文，正文作为 observation 进入后续轮次。即时子 Agent 为节省
1–2 轮调用，会在预算内把正文预展开进 system prompt。

Skill 关联 Tool 的 `skill_ref` 只表达实现归属。Tool 仍必须存在于数据库授权快照；已启用 Skill
的 required companion Tool 缺失时应拒绝半启用状态。Tool Search 也只能在已授权候选内排序。

Tool Search direct 模式把召回 Tool 放入下一次模型调用的顶层 Schema；dynamic 模式通过稳定
分发器调用，但真实目标仍要验证已加载和已授权。完整设计、事故和验证矩阵见
[PROMPT_SKILL_TOOL_FLOW.md](PROMPT_SKILL_TOOL_FLOW.md)。

## 6. 示例代码地图

### [examples/minimal_react_loop.py](examples/minimal_react_loop.py)

一个只依赖 Python 标准库的边界骨架，展示：

- 模型、策略、工具和事件的显式端口；
- 每次运行独立的 `RunState`；
- 工具调用与结果严格按 `call_id` 配对；
- 工具暴露和执行授权双重检查；
- 工具参数校验、timeout 和结构化错误；
- 有界空响应纠正、最大迭代和协作式取消；
- 外部 `CancelledError` 向 Job Runner 传播；
- 默认串行执行工具。

它证明循环可以从具体 LLM、工具框架和 Job 系统中分离。

它没有证明：

- 这段代码可直接替换 Moss 的生产实现；
- 当前消息结构能直接发送给任意 Provider；
- 示例已经覆盖上下文压缩、重试、重复调用保护、并行工具、持久化或外部事件协议；
- `AllowListPolicy` 足以承担生产权限控制。

迁移时必须重新实现 Provider adapter、权限、终态事件、资源生命周期和真实验收。

### [tests/test_minimal_react_loop.py](tests/test_minimal_react_loop.py)

标准库 `unittest` 回归用例，覆盖正文完成、工具结果配对、正文与工具并存、未知工具、策略拒绝、最大迭代、空响应、取消、工具 timeout、开放工具取消闭合和重复 call ID。

运行：

```bash
python3 -m unittest discover -s react/tests -v
```

### [examples/prompt_skill_tool_flow.py](examples/prompt_skill_tool_flow.py)

标准库边界示例，展示：

- `RunSnapshot` 冻结已启用 Skill 和已授权 Tool；
- Prompt 中 Skill 元数据索引与正文按需加载；
- Skill capability 按 direct/dynamic/no-search 运行能力投影；
- required companion Tool 完整性；
- Tool Search 不能扩大授权；
- 新加载 Tool 只影响后续 `ModelTurnView`；
- 子 Agent 预算内预展开。

它不实现真实模型、数据库、Skill 发布、搜索排序、沙箱或完整 Prompt Injection 防御。

### [tests/test_prompt_skill_tool_flow.py](tests/test_prompt_skill_tool_flow.py)

12 个标准库回归用例，覆盖 Skill 索引、半启用 fail closed、Skill/Tool 权限隔离、
direct/dynamic 轮次、搜索候选收窄、旧轮暴露快照和子 Agent 预算。

## 7. 适配新项目的方法

### 先调查目标项目

至少回答：

- 谁创建和拥有一个 run，权威 Job 状态存在哪里？
- 支持哪些 LLM Provider，流式 tool call 如何拼装和校验？
- Prompt 的模块、优先级、版本、动态变量和输出模式如何定义？
- Skill 由谁发布和审核，description/body/Hash 的事实源在哪里？
- 主 Agent 懒加载 Skill，还是子 Agent 预算内预展开？超预算如何处理？
- 工具 schema、权限、副作用、幂等、timeout 和并行安全由谁声明？
- Skill required/optional capability 如何映射到已授权 Tool？缺项时是否 fail closed？
- Tool Search 使用 direct 还是 dynamic 模式，加载结果从哪一轮生效？
- 身份、租户和数据权限上下文来自哪里，是否能在 run 内冻结？
- 取消来自用户、超时、worker shutdown 还是 lease/fencing 失效？
- 消息历史和 checkpoint 是否跨进程、跨 Pod 恢复？
- 事件是否需要重放、幂等、计费或审计？
- 是否支持用户问卷、计划审批、子 Agent 和长时异步任务？

### 推荐实施顺序

1. 先定义状态、终态和 Interrupt，不从 `while` 循环开始。
2. 冻结 `ModelPort`、`ToolPort`、`PolicyPort`、`PromptPort`、`SkillPort` 和外层
   `JobRunner` 契约。
3. 建立不可变 Run 资产快照，先证明 Skill 与 Tool 授权不会互相隐式扩大。
4. 实现无工具的一轮问答和最大迭代边界。
5. 实现单工具调用与严格的 call/result 配对。
6. 加入 Skill 元数据索引、按需正文加载和 Prompt/运行能力一致性测试。
7. 加入参数、权限、安全、副作用、timeout 和 Tool Search 轮次。
8. 加入结构化错误、有限重试和重复调用保护。
9. 加入上下文预算、压缩和 checkpoint。
10. 加入权威事件、开放步骤补偿和终态可靠性。
11. 最后增加安全并行、子 Agent、Skill 预展开和产品特有终止门禁。

### 可调整项

- 框架、语言、消息 DTO 和 middleware 形态；
- 迭代上限和各级 timeout；
- 错误分类、重试次数和退避；
- 工具是否支持同轮并行；
- checkpoint 频率和上下文压缩算法；
- 终止验收器和 Interrupt 类型。

这些调整不能破坏第 4 节的不变量和第 8 节红线。

## 8. 坑点、真实踩坑与红线

完整记录见 [evidence/incidents-and-red-lines.md](evidence/incidents-and-red-lines.md)。

### 常见坑点

- 把 ReAct 实现成一个巨型类，导致构造依赖、状态、测试和产品规则互相缠绕；
- 只靠模型文本判断是否完成；
- 有正文时忽略同轮 tool calls；
- 只在工具函数里鉴权，模型可见性和执行权限不一致；
- 把所有异常都当作可重试；
- 把工具内部重试暴露成多次模型 tool call；
- 同参只读失败或写操作被模型无限重复；
- 同轮工具一律并行，跨过写操作和依赖屏障；
- 把会话状态存在 Pod 内存，跨 Pod 后丢失；
- 只记录最终回答，不记录轮次、工具结果和错误来源；
- 流式输出可见就误认为 Job 已提交成功；
- 测试通过 `__new__` 或大量手工字段拼装半初始化对象，新增运行时字段后测试先于业务暴露漂移。
- 把 Skill description、正文、Prompt、Tool Schema 和授权集合混成一个“能力开关”；
- Skill 方法论要求调用 Tool，但发布/数据库策略没有同步授权配套 Tool；
- Tool Search 找到全局 Registry 中的 Tool 后跳过 Agent 数据库授权；
- direct 模式刚搜索完就按旧模型响应执行尚未暴露的真实 Tool；
- 用自由文本替换修正任意 Skill 中的旧 Tool 名，却没有 Prompt/Schema 一致性测试；
- Prompt 给出 `skills/x/SKILL.md`，但沙箱映射、symlink 或 PathGuard 实际无法读取；
- Skill 多行 description 在上传、数据库、对象存储或 runtime-assets 某一层被截断；
- 把自定义 Skill description/body 当作可信系统策略，忽略 Prompt Injection 风险。

### 已确认的真实踩坑

1. 生产 trace 出现相同工具和参数连续重复、没有推进任务；后续增加通用软保护。
2. 政策类工具发生 P1 重复调用，原因同时涉及循环无同参缓存、Tool Search 重复召回和工具描述不足。
3. Plan 状态曾保存在进程内存，多 Pod 调度后丢失，导致等待用户/退出计划流程误判。
4. 任务意图安全门禁早退时没有结构化错误来源，前端只能显示“Kernel 未知失败”。
5. 外层 timeout/cancel 截断工具时，SSE 与 metrics 曾缺少对应 `tool.call.result`。
6. best-effort delta flush 曾阻塞权威终态，造成用户已经看到完整结果但后台把 Job 记为失败。
7. Skill Creator 曾出现“方法论已可见、八个配套 Tool 未授权”的 P1 半启用事故；只补授权后
   direct 模式仍未首轮暴露，真实回归依旧失败。
8. Skill 多行 description 曾被误解析为单个 `>`，错误数据库值又覆盖对象存储中的原始
   `SKILL.md`，最终破坏 Prompt 路由元数据。
9. Skill 读取路径曾因 PathMapping 与 symlink/PathGuard 不一致返回 404，并在跨仓库迁移后
   发生历史修复丢失回归。
10. 生产 trace 中出现过重复读取同一 `SKILL.md`、没有推动任务进展。

### 红线

1. **禁止执行未暴露、未注册或未授权工具。**
2. **禁止把模型提供的身份、租户、权限或工具可见性当作可信事实。**
3. **禁止在下一次模型调用前留下没有对应结果的已接受 tool call。**
4. **禁止自动重试、缓存或并行执行副作用未知或非幂等工具。**
5. **禁止无上限循环、无上限模型等待或无上限工具执行。**
6. **禁止吞掉 `CancelledError`、worker shutdown、lease/fencing 失效等外部控制信号。**
7. **禁止把最大迭代、空响应、策略拒绝、内容安全拦截或未知错误汇聚成成功。**
8. **禁止让可丢的流式 delta、遥测、文件同步或清理无限阻塞权威终态。**
9. **禁止终态事件之前留下开放的 tool/step；补偿事件必须幂等。**
10. **禁止上下文压缩拆散或伪造 tool call/result 配对。**
11. **禁止把 run 内缓存、checkpoint 或会话状态跨租户串用。**
12. **禁止仅凭 Prompt 承担工具权限和安全控制。**
13. **禁止让 Skill、`skill_ref` 或 Tool Search 自动扩大数据库 Tool 授权集合。**
14. **禁止发布“Skill 已启用但 required companion Tool 未授权/不可达”的半启用状态。**
15. **禁止让 Prompt/Skill 宣称与本 run Tool Search 模式不一致的调用路径。**
16. **禁止用后续轮次扩大的工具集合追溯性地授权旧模型响应。**
17. **禁止从旧历史、旧 checkpoint 或已撤销 Skill 内容恢复权限。**
18. **禁止把自定义 Skill description/body 当作可信授权、身份或系统安全事实。**

发现目标需求与这些红线冲突时，AI 必须停止相关设计或实现，指出冲突、后果和需要决策的负责人。

## 9. 验证方法

### 最小单元测试矩阵

- 无工具正文正常完成；
- 正文与 tool calls 同时存在时仍执行工具；
- 每个 call ID 只有一个结果；
- 未知、未暴露、禁用和未授权工具均不执行；
- 参数缺失、类型错误和额外参数行为明确；
- 工具成功、决定性失败、可重试失败和 timeout；
- 同参重复调用按副作用策略处理；
- 串行屏障和仅显式安全工具并行；
- 空响应有限纠正后失败；
- 最大迭代不是成功；
- 协作式取消与 `CancelledError` 传播；
- 上下文压缩后消息协议仍合法；
- 用户 Interrupt 不被当作普通成功或失败；
- started/result 和唯一终态事件配对；
- 清理、事件或持久化失败不会永久阻塞终态。
- Prompt 只列已启用 Skill，禁用/越权 Skill 不出现；
- Skill description/body 的来源、版本、Hash 和完整性可追溯；
- Skill required companion Tool 完整性以及 Skill/Tool 各自禁用；
- direct Tool Search 的下一轮生效和旧轮拒绝；
- dynamic 分发器外壳与真实目标双校验；
- Skill 正文按实际工具模式投影，不要求不存在的调用路径；
- 恶意 Skill description/body 不能改变身份、租户和授权；
- `runtime-assets → Prompt → read Skill → search → Tool Call` 端到端链路；
- 沙箱中真实读取 Prompt 给出的 `skills/*/SKILL.md` 路径。

### 集成验证

- 使用真实模型完成“必须调用工具才能回答”的不可猜测任务；
- 注入工具 timeout、网络抖动、鉴权失败和配额失败；
- 多副本环境跨 Pod 恢复会话状态；
- 模拟外层 timeout 时工具仍在运行；
- 模拟事件 sink、对象存储或遥测 flush 阻塞；
- 重启 worker 后从 checkpoint 恢复且不重复副作用；
- 并发运行不同租户，确认状态、工具和文件隔离。

### 当前证据

- 来源重复调用保护/策略门禁子集：`8 passed, 30 deselected`；
- 来源其他边界测试：`10 passed, 3 failed`；
- 来源 Prompt/Skill/Tool 定向测试：结果见 [evidence/source-map.md](evidence/source-map.md)；
- 3 个失败均为测试夹具通过 `ReactAgent.__new__()` 半初始化对象、未补 `_delegation_runtime_policy`，不是本次修改造成；这同时暴露了构造边界和测试方式的脆弱性；
- 另有 1 个 registry subagent 资产测试模块因导入漂移无法收集；
- CBB 循环与关系示例：`23 tests, OK`；
- 真实 LLM API 测试文件存在，但本次采集未调用外部模型；
- CBB 示例测试结果记录在 [evidence/source-map.md](evidence/source-map.md)。

由于存在待修复的来源测试和独立复核缺失，本目录保持 `待验证`。

## 10. 与其他功能域的组合

- **tool-search**：决定模型本轮看到哪些工具；不能越过 ReAct 的暴露快照和执行授权。
- **skill-runtime（候选）**：负责发布、版本、审核、description/body、资源与 Hash；ReAct 只消费
  冻结投影。
- **prompt-runtime（候选）**：负责模块、优先级、版本、运行能力投影和 Prompt/Schema 一致性；
  ReAct 只发送构建结果。
- **subagent**：每个子 Agent 有独立 run state、预算和取消；父 Agent 负责 join、递归守卫和结果汇总。
- **user**：提供可信 actor/tenant/session 上下文；等待用户输入应建模为 Interrupt。
- **rbac**：授权结论进入 `PolicyPort`，但 ReAct 仍要执行本地 fail-closed 门禁。
- **rag**：RAG 可以作为工具或模型前上下文提供者接入，不能把检索权限和内容可信度隐含在循环中。

推荐组合顺序：

1. `user + rbac` 冻结可信运行上下文；
2. `tool-search` 形成工具可见性快照；
3. `react` 驱动模型与工具；
4. `subagent` 在明确预算和递归边界后接入；
5. `rag` 按工具或上下文边界接入。

灵活探索中发现的候选新功能域：

- `agent-runtime`：Job、Execution、Step、Interrupt 和权威终态；
- `context-management`：预算、压缩、checkpoint 和恢复；
- `tool-runtime`：schema、执行、错误、幂等、缓存和并行；
- `skill-runtime`：Skill 发布、审核、版本、内容投影和资源生命周期；
- `prompt-runtime`：Prompt 模块、优先级、版本、动态上下文和对抗验证；
- `runtime-events`：事件契约、重放、补偿与 metrics；
- `sandbox`：执行隔离和资源生命周期。

这些候选尚未在 CBB 中完成采集，不能因为本目录提到就视为已验证。

## 11. 来源与证据

以 [evidence/source-map.md](evidence/source-map.md) 为唯一详细索引。核心来源包括：

- `kernel/core/agent/react.py`
- `kernel/core/agent/state.py`
- `kernel/core/agent/context.py`
- `kernel/core/prompts/builder.py`
- `kernel/core/prompts/system/runtime_projection.py`
- `kernel/core/prompts/system/tool_policy.py`
- `kernel/core/skills/`
- `kernel/core/tools/tool_search.py`
- `kernel/core/tools/builtin/read.py`
- `docs/agent/tool-search-runtime-policy.md`
- `kernel/core/agent/middleware/`
- `kernel/core/services/task_runner.py`
- `kernel/core/services/job/job_executor.py`
- `kernel/tests/agent/`
- `test/api-test/agent/`
- `docs/bug/` 下与循环、工具和终态有关的真实事故记录

本目录示例为重新编写的概念提炼，没有逐段复制内部源码。来源仓库属于内部项目，若未来对外发布 CBB，必须重新确认许可和脱敏范围。

## 12. AI 使用指引

### 阅读顺序

1. 本文件；
2. [BOUNDARIES.md](BOUNDARIES.md)；
3. [PROMPT_SKILL_TOOL_FLOW.md](PROMPT_SKILL_TOOL_FLOW.md)；
4. [evidence/incidents-and-red-lines.md](evidence/incidents-and-red-lines.md)；
5. [examples/minimal_react_loop.py](examples/minimal_react_loop.py)；
6. [examples/prompt_skill_tool_flow.py](examples/prompt_skill_tool_flow.py)；
7. 对应 `tests/`；
8. 需要核验证据时再读 [evidence/source-map.md](evidence/source-map.md) 和源项目。

### 灵活探索

固定的是证据和完成门禁，不是文件阅读路线。AI 可以从循环入口继续追踪模型适配、工具执行、测试、事故、事件或调用方，也可以从故障反向追踪修复。

探索时：

- 主动寻找会改变边界、不变量、红线或适用性的相邻事实；
- 新发现独立能力时记录为候选功能域，不把完整实现硬塞进 ReAct；
- 代码、文档和测试矛盾时保留矛盾并降低状态，不能选一个顺眼的说法；
- 运行验证失败时记录失败条件，不能只报告通过项；
- 当继续探索不再影响结论时及时收敛。

### 在新项目中的输出要求

AI 应先给出：

- 目标项目的运行时边界图；
- 状态和终态表；
- 模型/工具/策略/事件接口；
- Prompt/Skill/Tool 的运行时快照、投影和轮次关系；
- 不变量与红线清单；
- timeout、取消、重试和恢复策略；
- 最小测试矩阵；
- 对本示例的采用、改写和未采用内容。

### 禁止事项

- 不复制 Moss `ReactAgent` 整个类或目录结构；
- 不把 Moss 的产品特有工具、Prompt、事件名和状态名当成通用标准；
- 不因为本目录引用了来源测试就声称目标项目已验证；
- 不绕过本文件红线；
- 不擅自修改来源仓库。

## 13. 变更记录

- 2026-07-29：纠正目录含义为 ReAct Agent Loop；从 Corevo Platform 当前 `upstream/dev` 采集循环、边界、事故和测试证据；新增边界文档、证据索引及标准库最小示例；状态设为 `待验证`。
- 2026-07-29：继续采集 Skill 元数据/正文、Prompt 构建、companion Tool、Tool Search
  direct/dynamic 模式和 ReAct 轮次关系；新增关系文档、标准库示例、12 个测试及四类真实事故，
  状态仍为 `待验证`。
