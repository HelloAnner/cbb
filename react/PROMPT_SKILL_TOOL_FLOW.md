# Prompt、Skill、Tool 与 ReAct Loop 的关系

## 1. 一句话结论

```text
Skill 告诉模型“应该怎样做”
Prompt 告诉模型“本轮有哪些规则和入口”
Tool Schema 告诉模型“本轮可以请求什么”
服务端授权与执行门禁决定“实际上允许做什么”
ReAct Loop 负责让这些事实按轮次闭合
```

这四层不能合并成一个 Prompt，也不能互相代替。尤其要记住：

> Skill 被启用，不等于 Tool 被授权；Prompt 提到某个 Tool，不等于模型可以调用；Tool
> Search 找到某个 Tool，也不等于它可以突破本 run 的授权快照。

本文结论基于 Corevo Platform
`upstream/dev@c7e869b54139e89743f8ae45c09e6fc2aef68320`。其中“来源事实”描述
Moss 当前实现；“迁移建议”是 CBB 根据代码、测试和事故提炼的新项目设计。

## 2. 关键术语

| 术语 | 含义 | 不代表什么 |
|---|---|---|
| 已发布 Skill | 有可追溯版本的 Skill 包，含 `name`、`description`、正文及可选资源 | 已经分配给当前 Agent |
| 已启用 Skill | 本次运行的 SkillRegistry 中存在，允许模型看到其索引或正文 | 自动获得其配套 Tool 权限 |
| Skill 路由元数据 | `name + description + load_path`，用于判断是否加载正文 | 可执行指令或授权事实 |
| 已加载 Skill | 主 Agent 已读取正文，或子 Agent 已把正文预展开到 Prompt | 正文可以覆盖系统安全策略 |
| Tool Registry | Tool 实现、名称、Schema、描述、副作用和 `skill_ref` | 用户/租户授权事实源 |
| 数据库授权工具 | 平台针对 Agent 和实例解析出的本 run 工具上限 | 每个工具都要在首轮暴露 |
| Skill 配套 Tool | `skill_ref` 指向已启用 Skill，且同时在数据库授权集合内的 Tool | `skill_ref` 自动扩权 |
| 模型可见 Tool | 某次模型调用真实收到的 Tool Schema | 永久可用或可跳过执行校验 |
| Tool Search 已加载 | 搜索命中且仍处于本 run 授权集合内，记入会话搜索状态 | 已在产生当前响应的旧 Tool Schema 中 |

建议在新项目中避免把“启用”“加载”“暴露”“授权”都叫作 `active`。它们是不同状态，
需要不同字段和测试。

## 3. 完整链路

```text
Platform / Control Plane
  ├─ 解析本 Agent 已启用 Skill
  └─ 解析本 Agent 数据库授权 Tool
                 │
                 ▼
Job Runner 冻结 RunSnapshot
  ├─ enabled_skill_refs
  ├─ authorized_tool_names
  ├─ actor / tenant / role
  └─ tool_search mode
                 │
                 ├─────────────┐
                 ▼             ▼
Prompt Builder                 Tool Exposure
  ├─ 系统规则                    ├─ Core Tool
  ├─ Agent 自定义指令            ├─ 已授权 Skill 配套 Tool
  ├─ Skill 元数据索引            └─ Tool Search 已加载 Tool
  └─ 与运行模式一致的工具说明             │
                 │                       │
                 └──────────┬────────────┘
                            ▼
                    Model Turn N
                messages + tool schemas
                            │
          ┌─────────────────┴──────────────────┐
          ▼                                    ▼
  read Skill 正文                         调用 Tool Search
          │                                    │
  按当前能力投影正文                   只从授权候选中加载
          │                                    │
          └──────────── observation ───────────┘
                            │
                            ▼
                    Model Turn N+1
          看到 Skill 正文和更新后的 Tool Schema
                            │
                            ▼
             Tool Call → 执行时再次鉴权 → Result
                            │
                            └──── 回到下一轮模型
```

最重要的轮次语义是：

1. Prompt 与 Tool Schema 都是一次模型调用的快照。
2. `read SKILL.md` 或 `tool_search` 的结果是 observation，只影响后续模型判断。
3. direct 模式下，Tool Search 新加载的真实 Tool 从下一次模型调用才进入顶层 Schema。
4. dynamic 模式下，模型调用的是稳定分发器；执行器还要校验真实目标已加载且已授权。
5. 执行模型 Tool Call 时，必须按“产生该响应的那一轮”暴露快照校验，不能使用后来扩大的集合。

## 4. Run 启动：先冻结资产，再构建 Prompt

### 来源事实

Moss 的 Job Runner 在创建 `ReactAgent` 前解析 Tool/Skill 资产：

- Tool 权限来自 Platform `runtime-assets` 的数据库结果；
- Skill 形成过滤后的 `SkillRegistry`；
- `ReactAgent` 同时接收 `enabled_tool_names`、`skill_registry` 和 Tool Search 配置；
- Platform 工具边界不可用时，Tool 集合按空集合 fail closed，不回退文件或全局 Registry；
- Skill 兼容投影和 Tool 权限不是同一个事实源。

`ReactAgent.skill_registry` 的 fallback 只扫描租户目录，不自动加入所有 builtin Skill。源码注释
明确说明：否则被用户关闭的内置 Skill 会重新进入 system prompt。

### 迁移建议

在新项目中显式定义不可变 `RunSnapshot`：

```text
RunSnapshot
  actor / tenant / role
  agent_version
  enabled_skill_ids + exact skill versions/content hashes
  authorized_tool_names + policy version
  model/provider
  prompt template version
  tool_search mode/config version
```

同一个 run 中不热切换这些事实。配置更新从下一次新 run 生效。需要恢复时，checkpoint 必须带
快照版本或可验证 digest，不能只保存几个名称。

## 5. Prompt 构建：索引与正文分开

### 5.1 来源事实：主 Agent 使用渐进式加载

Moss 的普通主 Agent 采用三级 Skill 加载：

1. Level 1：Prompt 中始终只放已启用 Skill 的 `name + description + SKILL.md path`；
2. Level 2：模型判断任务明确匹配 description 时，通过 `read` 加载 `SKILL.md`；
3. Level 3：Skill 指示需要时，再读取 `references/` 或使用 `scripts/`、`assets/`。

Prompt Builder 当前主要拼接顺序是：

```text
内核模块
  → agents.md
  → 企业上下文
  → 非管理员边界
  → Skill 索引
  → 可用伙伴
  → 长期记忆
  → 自动化/部署/输出环境
```

Tool policy 不是一段永远相同的静态文本。它会根据 Tool Search 是否启用、是否使用动态分发器、
是否是纯文本输出模式生成不同说明。

### 5.2 来源事实：读取正文时再次按运行能力投影

当 `read` 读取 `skills/*/SKILL.md` 时，Moss 会按当前 Tool Search/动态分发能力以及委派能力
投影正文，避免 Skill 继续要求不存在的 `invoke_dynamic_tool` 或已关闭的委派路径。

当前实现用已知短语替换完成一部分 Tool 运行模式适配。它有定向测试，但这类自由文本替换无法
证明任意自定义 Skill 都会被正确改写。

### 5.3 迁移建议：结构化能力声明优于自由文本重写

推荐 Skill 把“方法步骤”和“需要的能力”分开：

```yaml
steps:
  - instruction: 查询企业风险事实并保留来源
    capability: risk_data
```

运行时再把 `risk_data` 投影为：

- 当前可直接调用的真实 Tool；
- 需要先通过 Tool Search 发现；
- 当前运行环境不可用，禁止声称已经执行。

这比在任意 Markdown 中全局替换工具名可靠。CBB 示例
[examples/prompt_skill_tool_flow.py](examples/prompt_skill_tool_flow.py) 展示了这种做法。

## 6. Skill 的触发与加载

### 主 Agent

主 Agent 的“触发”主要是模型根据 Skill description 与用户任务做语义判断，不是服务端固定
关键词路由。简单问候和通用问题不应加载任何 Skill。

推荐循环：

```text
Turn N:
  Prompt 提供 Skill 索引
  模型选择 read(skills/x/SKILL.md)

Tool Result:
  返回按本轮能力投影过的 Skill 正文

Turn N+1:
  模型根据正文制定步骤、发现工具或执行工具
```

模型重复读取同一 Skill 不会增加权限，也通常不会增加信息。来源事故已经出现重复读取
`SKILL.md` 的无进展循环，因此仍要进入通用 action repeat guard。

### 子 Agent

Moss 的即时子 Agent 为节省一到两轮模型调用，会把已启用 Skill 正文按字典序预展开到 system
prompt，默认总预算 12,000 字符；超预算的 Skill 只列路径，仍按需 `read`。预展开前同样执行
Tool Search/委派能力投影。

这是一项性能选择，不应变成安全例外：

- 子 Agent 只能得到过滤后的 SkillRegistry；
- 显式 Tool whitelist 不因 Skill whitelist 自动扩大；
- 预展开正文不能扩大 Tool 权限；
- 超预算必须可观测，不能静默截断成半份方法论；
- 长时或可恢复子 Agent 应冻结精确 Skill 内容和 Hash，不依赖执行时重新扫描目录。

## 7. Skill 与 Tool 的关系

### 7.1 `skill_ref` 是实现关系，不是授权关系

来源事实源给出的集合关系是：

```text
Skill 配套工具
  = 已启用 Skill 的 skill_ref 工具
  ∩ 数据库授权工具
```

Tool Registry 的 `skill_ref` 只能说明 Tool 属于哪项 Skill。它不能把 Tool 自动加入数据库授权
集合，也不能覆盖用户角色、租户或资源权限。

### 7.2 为什么配套 Tool 可以直接暴露

Moss 曾要求 Skill Creator 方法论直接调用一组生命周期 Tool，但这些 Tool 虽已授权，direct 模式
首轮仍不可见，模型未必会先 Tool Search。长期修复是：

- Platform 先验证已启用 Skill 的全部配套 Tool 都在数据库授权集合；
- 缺少任何配套 Tool 时报告策略不完整，拒绝半启用状态；
- Kernel 只把“已授权且 Skill 已启用”的配套 Tool 首轮直接暴露；
- 这一步是收窄已授权集合，不是根据 Skill 扩权。

### 7.3 新项目的两种合理策略

新项目可以选择：

1. **强配套**：Skill 启用时所有 companion Tool 必须授权，缺项启动失败；
2. **可选能力**：Skill 声明 required/optional capability，required 缺项失败，optional 在正文投影为
   不可用或降级步骤。

不能采用第三种模糊状态：Prompt 宣称“必须调用 X”，运行时却既不暴露 X，也不告诉模型如何发现。

## 8. Tool Search 与循环

### direct 模式

```text
Turn N schemas: core + companion + previously-loaded
Model: tool_search(query)
Runtime: 从授权候选中加载 A/B
Tool Result: 返回 A/B 的描述和 Schema
Turn N+1 schemas: core + companion + previously-loaded + A/B
Model: 直接调用 A
```

加载结果需要使下一轮 Tool Schema 缓存失效。模型不能在 Turn N 的同一个响应里调用刚搜索到、
但当时未暴露的 A。

### dynamic 模式

```text
Turn N schemas: core + companion + tool_search + invoke_dynamic_tool
Model: tool_search(query)
Runtime: 从授权候选中加载 A/B
Turn N+1:
Model: invoke_dynamic_tool(tool_name=A, arguments=...)
Runtime:
  校验分发器在本轮可见
  校验 A 已加载
  校验 A 属于本 run 授权集合
  校验 A 当前仍启用、参数合法、权限通过
```

分发器外壳的授权不能代替真实目标授权。

### 搜索候选边界

Tool Search 的召回池必须先经过：

```text
数据库授权
  → Agent 实例收窄
  → admin/role
  → enabled Skill
  → 子 Agent/运行模式禁用
  → 当前 disallowed tools
```

搜索算法、Prompt 关键词、Tool aliases、Skill boost 都只能在这个池内排序，不能扩大候选。

## 9. 指令、数据与授权的信任关系

推荐把关系分成两条互不混淆的轴：

### 模型指导轴

```text
系统安全与产品协议
  → Agent 自定义说明
  → 已发布/已绑定 Skill 方法论
  → 用户任务
  → Tool/RAG/网页等外部内容
```

实际冲突规则要在产品中明确；不能只依赖“谁拼在 Prompt 后面”或模型 recency bias。

### 运行时授权轴

```text
可信身份/租户
  → 数据库 Agent 策略
  → 实例 binding 只收窄
  → 本轮模型可见性快照
  → 参数/资源级授权
  → 执行
```

Prompt、Skill、用户文本和 Tool Result 都不能写入或放大这条授权轴。

Skill description 和自定义 Skill 正文仍是 Prompt Injection 攻击面。来源代码对 Skill
description 当前只执行长度截断；安全文档曾描述“检测注入模式”，两者并不完全一致。因此 CBB
不能把“Skill 元数据已完成充分净化”写成已验证事实。新项目至少应：

- 只注入本 run 已绑定的 Skill；
- 对来源、发布者、版本和 Hash 可追溯；
- description 有长度/格式约束并以数据边界包裹；
- 自定义 Skill 经过发布审核或权限控制；
- 无论 Prompt 内容如何，Tool 权限仍由服务端强制执行；
- 用恶意 description/body 做对抗测试。

## 10. 生命周期与恢复

### 来源事实

- Tool Search 已加载状态可以进入 checkpoint；
- direct 模式恢复后，可把仍有效的已加载 Tool 放回下一轮顶层 Schema；
- 检测到任务明显切换时，来源实现会清空 Tool Search loaded state；
- SkillRegistry 在 run 启动时由资产解析结果注入；
- 来源建议新 Skill/新版本从下一轮 Job 生效，不在正在运行的同一轮热替换。

### 迁移建议

恢复时依次验证：

1. checkpoint owner 与 tenant/run/session 一致；
2. Tool/Skill/Prompt 模板版本仍可用；
3. 已加载 Tool 仍属于当前恢复策略允许的授权集合；
4. 旧历史中的 Skill 正文或 Tool 名称不能恢复已撤销权限；
5. 任务切换后清理只与旧任务相关的搜索状态；
6. 恢复失败时明确进入 capability-degraded/failed，不让 Prompt 继续承诺不可用能力。

## 11. 已确认的事故教训

### Skill 方法论可见、配套 Tool 不可用

2026-07-16，Skill Creator 已启用且模型能看到方法论，但数据库策略漏掉八个配套 Tool；只补授权
后，direct 模式首轮仍不暴露这些 Tool，真实回归仍失败。最终同时补齐：

- 数据库授权完整性；
- Skill/companion 一致性校验；
- 已授权 companion Tool 直接暴露；
- 回归 Job 与单元测试。

结论：**Prompt 正确、Skill 正确、Tool 实现存在，仍然可能整体不可用。**

### Skill description 在多层解析后损坏

2026-07-28，YAML 多行 `description` 被前端误读为单个 `>`，错误值进入数据库，再反向覆盖对象
存储中的 `SKILL.md`；最终 Prompt 中的 Skill 索引失去触发描述。

结论：Skill 元数据需要单一安全编解码器和明确事实源，必须验证“上传包 → 数据库 →
runtime-assets → Kernel → Prompt”端到端一致。

### Skill 文件可见但 `read` 返回 404

2026-04-18 及后续迁移回归中，Prompt 给出的 `skills/x/SKILL.md` 与沙箱 PathMapping/symlink
解析不一致，模型知道路径却无法加载正文。

结论：Prompt 路径只是契约的一半；沙箱物化、只读映射、PathGuard 和真实 `read` 必须做
端到端测试。跨仓库迁移时要把历史事故当回归清单。

### 重复读取 Skill 无法推进

2026-06 的生产 trace 包含重复读取同一 `SKILL.md`。结论：Skill load 仍是普通 Tool Action，
需要精确参数 repeat guard；不能因为它是“读取说明”就允许无上限重复。

## 12. 不变量与红线

任何实现都必须保持：

1. Skill、Prompt、Tool Search 或模型输出不能授予 Tool 权限。
2. Prompt 中只列出本 run 已启用且来源可追溯的 Skill。
3. Skill description 只能用于路由，不能成为身份、权限或数据事实。
4. Skill 正文在进入模型前必须按真实运行能力投影；不能强制调用不存在的路径。
5. 已启用 Skill 的 required companion Tool 缺失时必须 fail closed，不能半启用。
6. Tool Search 候选始终是本 run 授权集合的子集。
7. direct 模式新加载 Tool 只能在下一次模型调用中成为顶层 Schema。
8. dynamic 分发同时校验外壳和真实目标。
9. 执行必须按产生响应时的 `ModelTurnView` 校验，不使用后来的扩大集合。
10. 历史消息、旧 checkpoint 或旧 Skill 正文不能恢复已撤销权限。
11. Skill 文件路径必须经过真实沙箱读取测试，不能只测 Prompt 字符串。
12. Prompt 宣称的 Tool 调用协议必须与本 run 的 Tool Search 模式一致。
13. 自定义 Skill 内容必须视为潜在 Prompt Injection，服务端硬门禁不能缺席。
14. Skill/Tool/Prompt 版本和 Hash 应进入运行证据，使故障可以重放。

## 13. 最小验证矩阵

### Prompt 与 Skill

- 只列出已启用 Skill，禁用/越权 Skill 不出现；
- description 完整支持 YAML 多行并有长度上限；
- 简单请求不加载 Skill，匹配请求只加载必要 Skill；
- `read SKILL.md` 成功、404、拒绝和截断都有明确 observation；
- Skill 正文按 direct/dynamic/no-search 三种模式正确投影；
- 恶意 description/body 不能改变身份、租户和工具授权；
- 主 Agent 懒加载与子 Agent 预算预展开分别覆盖。

### Skill 与 Tool

- required companion Tool 完整时启动成功，缺一个即 fail closed；
- Skill 禁用后 companion Tool 不可见；
- Tool 已授权但 Skill 未启用时，`skill_ref` Tool 不可见；
- Skill 已启用但 Tool 未授权时，不能由 Registry 自动补权限；
- Prompt 中的旧 Tool 名不能绕过本轮暴露校验。

### Tool Search 与循环

- Search 只能返回授权候选；
- direct 模式加载后使下一轮 Schema 更新，旧轮调用仍拒绝；
- dynamic 模式真实目标未加载/未授权均拒绝；
- task shift 清理 loaded state；
- checkpoint 恢复会重新与当前授权求交集；
- 重复 search/read 不形成无界循环；
- Tool Search/Platform 故障时 Prompt 与状态明确降级，不生成假执行结果。

### 端到端

- `runtime-assets → SkillRegistry/Tool snapshot → Prompt → read Skill → search → Tool Call`
  完整链路；
- Prompt 记录的 Skill/Tool 与实际模型请求 Schema 一致；
- 事件中可核对 prompt version、skill version/hash、authorized count、exposed count、loaded count；
- 真实模型完成一项必须先读 Skill、再发现 Tool 才能完成的不可猜测任务。

## 14. 示例和不能照搬的部分

[examples/prompt_skill_tool_flow.py](examples/prompt_skill_tool_flow.py) 使用标准库实现：

- 不可变 `RunSnapshot`；
- Skill 元数据索引与正文分离；
- 结构化 capability 投影；
- companion Tool 完整性；
- direct/dynamic 两种 Tool Search 轮次语义；
- 原始 `ModelTurnView` 暴露校验；
- 子 Agent 预算预展开。

对应测试是
[tests/test_prompt_skill_tool_flow.py](tests/test_prompt_skill_tool_flow.py)。

它没有实现：

- 真实 Prompt 模板系统、LLM Provider 或流式 Tool Call；
- 数据库、上传发布、签名、审核或权限服务；
- Tool Search 排序质量；
- 沙箱路径、Skill scripts/assets 执行；
- 完整 Prompt Injection 防御；
- 与现有 [minimal_react_loop.py](examples/minimal_react_loop.py) 的框架绑定。

新项目应把这里的边界接入自己的 ReAct Loop，不应复制类名、数据结构或 Moss 的产品专有 Tool
名称。
