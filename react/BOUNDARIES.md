# ReAct Agent Loop 边界设计

## 1. 为什么边界比 `while` 循环更重要

最小 ReAct 逻辑只有：

```text
模型判断 → 有工具就执行 → 把结果交回模型 → 无工具时输出回答
```

生产事故通常不发生在这四个箭头本身，而发生在它们与权限、timeout、取消、上下文、持久化、事件和资源生命周期的交界处。

因此 ReAct 设计的第一产物应是责任边界和状态协议，不是循环代码。

## 2. 推荐分层

```text
┌─────────────────────────────────────────────────────────────┐
│ Job Runner                                                  │
│ run/execution 身份、总 deadline、取消、lease、权威终态、补偿 │
└─────────────────────────────┬───────────────────────────────┘
                              │ RunContext / RunResult
┌─────────────────────────────▼───────────────────────────────┐
│ ReAct Loop                                                  │
│ 状态迁移、轮次、model/tool/observation 顺序、循环内终止       │
└──────────────┬──────────────────────┬───────────────────────┘
               │                      │
┌──────────────▼────────────┐  ┌──────▼──────────────────────┐
│ Model + Context Boundary  │  │ Tool + Policy Boundary      │
│ provider、stream、budget   │  │ exposure、schema、auth、执行 │
│ compression、checkpoint    │  │ timeout、side effect、结果   │
└──────────────┬────────────┘  └──────┬──────────────────────┘
               │                      │
┌──────────────▼──────────────────────▼───────────────────────┐
│ Prompt + Skill Projection                                  │
│ system modules、Skill index/body、runtime capability view   │
└─────────────────────────────┬───────────────────────────────┘
                              │
┌─────────────────────────────▼───────────────────────────────┐
│ Runtime Facilities                                         │
│ Event Sink、Sandbox、Artifact、Persistence、Observability    │
└─────────────────────────────────────────────────────────────┘
```

## 3. 责任表

| 组件 | 应负责 | 不应负责 |
|---|---|---|
| Job Runner | 全局 timeout、取消原因、权威终态、事件补偿、持久化与重试所有权 | 解释模型 tool calls |
| ReAct Loop | 轮次、状态迁移、工具调用与观察配对、循环内终止 | 数据库、MQ、对象存储和具体权限规则 |
| Model Adapter | Provider 消息格式、流式增量、usage、finish reason | 工具授权与 Job 终态 |
| Context Manager | 历史、token 预算、压缩、checkpoint | 决定工具是否可执行 |
| Prompt Builder | 按运行能力组装系统模块、Agent 指令、Skill 索引和动态上下文 | 用文本授予 Tool 权限 |
| Skill Runtime | Skill 版本、元数据、正文、资源、渐进加载与投影 | 自动扩大 Tool 白名单 |
| Tool Registry | 工具定义、schema、side effect、parallel safety | 用户/租户最终授权 |
| Tool Search | 在已授权候选内召回、加载并维护 run-scoped 状态 | 从全局 Registry 扩权 |
| Tool Executor | 参数校验、单工具 timeout、实际执行、结构化结果 | 模型下一轮和 Job 状态 |
| Policy/Security | 工具可见性、身份/租户/资源授权、高风险门禁 | 伪造工具结果或吞掉拒绝事实 |
| Event Sink | 事件 ID、序号、持久化、重放与投递 | 反向决定业务是否成功 |
| Sandbox/Artifact | 隔离、文件与资源生命周期 | ReAct 终止判定 |
| Subagent Runtime | 子 run、预算、递归、join、父子取消 | 共享父 Agent 可变状态 |

## 4. 一轮的协议

### 4.1 模型返回工具调用

```text
assistant(content?, tool_calls=[call-1, call-2])
tool(call-1, result-or-error)
tool(call-2, result-or-error)
下一次 model call
```

规则：

- `content` 可以保留给 UI 或上下文，但不能直接作为最终回答；
- 两个 call 都必须得到结果，包括未知、拒绝、timeout 和取消结果；
- 默认按原顺序串行；
- 只有工具元数据明确允许、调用之间无依赖且没有副作用屏障时才并行。

### 4.2 模型只返回正文

正文只是“终止候选”，还要确认：

- 没有未闭合工具；
- 没有等待回收的异步任务；
- 没有未完成计划/TODO；
- 没有等待用户或审批；
- 输出满足任务或子 Agent 结构契约。

### 4.3 模型返回空响应

允许有限次数的明确纠正。耗尽后是 `MODEL_EMPTY_RESPONSE`，不能生成“任务已处理完成”等假回答。

## 5. 状态与 Interrupt

推荐至少区分：

| 状态 | 语义 | 是否成功 |
|---|---|---|
| `RUNNING` | 循环仍可推进 | 否 |
| `COMPLETED` | 验收通过并输出最终结果 | 是 |
| `WAITING_INPUT` | 等待用户问卷/补充信息 | 否，属于 Interrupt |
| `WAITING_APPROVAL` | 等待计划或高风险操作审批 | 否，属于 Interrupt |
| `CANCELLED` | 用户或运行时取消 | 否 |
| `TIMED_OUT` | 总 deadline 到期 | 否 |
| `MAX_ITERATIONS_REACHED` | 预算耗尽 | 否 |
| `FAILED` | 决定性错误或不可恢复故障 | 否 |

Moss 当前部分等待用户场景映射到 `SELF_TERMINATED`。迁移到新项目时，推荐建模为显式 Interrupt，避免与“Agent 自主完成”混淆。这是迁移建议，不是对来源现状的描述。

## 6. Timeout 和取消的所有权

推荐形成四层上界：

```text
Job deadline
  └─ ReAct run deadline
       ├─ model call timeout
       └─ tool call timeout
```

- 外层 Job deadline 是最终裁决者；
- 内层 timeout 不能超过剩余 Job 时间；
- 透明重试必须计入同一个 deadline，不能每次重置总预算；
- `CancelledError` 向外传播，外层根据取消来源映射为 user cancel、timeout、shutdown 或 lease lost；
- started 工具在外层取消时必须被补成 cancelled/timeout/failed 结果。

## 7. 工具边界

工具安全链至少包含：

```text
模型可见性快照
  → 注册表存在性
  → 动态别名/真实目标解析
  → 参数 schema
  → 运行模式限制
  → 用户/租户/资源授权
  → 安全与配额
  → 重复调用保护
  → side-effect-aware 调度
  → timeout 执行
  → 结构化结果
```

动态分发工具尤其要在“外壳名”和“真实目标名”两层执行策略，不能让外壳绕过真实工具权限。

## 8. 重复调用和并发

真实事故证明模型会对同一工具和同一参数重复调用。

推荐 action key：

```text
real_tool_name + canonical_json(validated_arguments)
```

策略应考虑：

- 只读成功可以缓存或在有限重复后软拦截；
- 不可重试失败允许极少量确认性重试后软拦截；
- 可重试失败由明确 retry policy 控制；
- 写操作或未知副作用成功后，第二次相同 action 就应保守阻断；
- 分页、offset、sheet、range 等参数必须进入 key；
- 只做精确归一化，避免过度语义归一化误杀不同请求。

同轮并行规则：

- 默认串行；
- 只并行显式 `parallel_safe` 工具；
- 写工具、未知副作用和任务依赖形成屏障；
- 并行结果仍按原始 call 顺序写回上下文，除非 Provider 协议明确支持其他顺序。

## 9. 上下文和 checkpoint

上下文管理要保留：

- system/user/assistant/tool 的角色；
- tool call ID 和 tool result ID；
- 当前任务和关键决策；
- 未完成任务、Interrupt 和开放异步工作；
- 压缩历史和恢复版本。

压缩、清理大工具结果或恢复 checkpoint 时，不能留下孤儿 tool call，也不能把旧运行的工具结果关联到新 run。

会话级状态如果需要跨请求、跨 Pod 或恢复，就不能只放进程内内存。

## 10. 事件和权威终态

循环内部回调不等于可靠事件。

外层事件协议至少要保证：

- started/result 成对；
- `event_id` 幂等；
- 同一 execution 序号单调；
- usage、artifact 和工具结果在权威成功终态前完成或有明确补偿；
- timeout/cancel/fail 时补齐开放工具；
- 终态唯一且不可逆；
- best-effort delta 丢失不能阻断终态；
- 终态投递失败有有限重试和持久事实，可供 repair。

## 11. Prompt、Skill 与 Tool 暴露边界

完整说明见 [PROMPT_SKILL_TOOL_FLOW.md](PROMPT_SKILL_TOOL_FLOW.md)。循环侧必须知道的最小协议是：

```text
RunSnapshot
  ├─ enabled Skill + exact version/hash
  ├─ authorized Tool + policy version
  └─ Tool Search mode
        │
        ▼
ModelTurnView N
  ├─ system prompt / Skill index or projected body
  └─ exposed tool schemas
        │
        ├─ read Skill ──────┐
        └─ tool_search ─────┤ observation
                            ▼
                     ModelTurnView N+1
```

边界规则：

- Prompt 和 Tool Schema 都是一次模型调用的快照；
- 主 Agent 默认只注入 Skill 元数据，正文通过 `read` 进入后续 observation；
- 子 Agent 可以预算内预展开正文，但使用同一份运行能力投影；
- `skill_ref` 只表达实现归属，不能授予 Tool 权限；
- Skill required companion Tool 缺失时 fail closed；
- Tool Search 只在本 run 授权候选内工作；
- direct 模式召回 Tool 从下一次模型调用开始可见；
- dynamic 模式同时校验分发器外壳和真实目标；
- 执行按产生当前响应的 `ModelTurnView` 校验，禁止追溯性扩权；
- 历史 Skill 正文和旧 checkpoint 都不能恢复已撤销权限。

Skill description/body 既是产品能力，也可能是 Prompt Injection 输入。是否允许自定义、由谁发布审核、
如何签名和版本化属于 Skill Runtime；ReAct 必须坚持服务端授权和本轮暴露门禁，不能把这个风险
交给 Prompt 自律。

## 12. 灵活探索时的边界判断

探索 ReAct 时可以继续追踪相邻模块，但按下面规则决定归属：

- 如果信息改变“循环何时调用谁、何时停止”，记录在 ReAct；
- 如果信息只定义某个 Provider、工具、权限、存储或事件系统内部如何实现，记录接口后转成候选功能域；
- 如果事故横跨多个域，在 ReAct 记录循环侧教训，并在未来对应功能域记录另一侧事实；
- 如果源代码和文档冲突，保持 `待验证` 并记录冲突，不擅自替来源选择事实。
