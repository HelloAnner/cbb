# ReAct Agent Loop 来源与验证索引

## 1. 来源快照

- 项目：`AI/corevo-platform`
- 本地只读来源：`/Users/anner/fine/ai/dev`
- 目标分支：`dev`
- 采集日期：2026-07-29
- 精确 commit：`c7e869b54139e89743f8ae45c09e6fc2aef68320`
- 采集时：
  - `git status --porcelain` 为空；
  - `HEAD == refs/remotes/upstream/dev`；
  - `git ls-remote upstream refs/heads/dev` 返回相同 SHA。
- 来源使用限制：内部项目。本次没有逐段复制生产源码；CBB 示例是根据接口、状态、测试和事故重新编写的概念提炼。

## 2. 核心源码地图

行号只对应上述精确 commit；后续版本优先按 symbol 定位。

| 关注点 | 来源位置 | Symbol / 当时行号 | 说明 |
|---|---|---|---|
| 主对象与模型快照 | `kernel/core/agent/react.py` | `ReactAgent`，606 | 构造时绑定 LLM，声明会话内不重建 |
| 运行入口 | `kernel/core/agent/react.py` | `ReactAgent.run`，1056 | 初始化可信上下文、Prompt、历史、checkpoint 和安全前置 |
| 核心循环 | `kernel/core/agent/react.py` | `_run_loop`，1447 | middleware → model → tool → observation → terminal |
| 工具批次 | `kernel/core/agent/react.py` | `_execute_tools`，2280；`_execute_tools_inner`，2370 | ExecutionContext、分组和生命周期 |
| 单工具防御链 | `kernel/core/agent/react.py` | `_run_one_tool`，2783 | 暴露、参数、权限、配额、timeout、错误和回调 |
| 重复调用软保护 | `kernel/core/agent/react.py` | `_should_skip_repeated_tool_action`，3621 | 根据副作用与结果设置阈值 |
| checkpoint | `kernel/core/agent/react.py` | `_save_checkpoint`，3980 | 节流保存循环上下文 |
| 终止关键词 | `kernel/core/agent/react.py` | `_should_terminate`，6035 | 当前实现仍有关键词终止逻辑 |
| 状态模型 | `kernel/core/agent/state.py` | `AgentStatus` 27；`AgentState` 123；`AgentResult` 194 | 状态、轮次、token 和结果 |
| 消息上下文 | `kernel/core/agent/context.py` | `Message` / `AgentContext` | system/user/assistant/tool 与压缩状态 |
| 生命周期接口 | `kernel/core/agent/middleware/base_middleware.py` | `AgentMiddleware`，20 | 8 个 hook |
| 生命周期顺序 | `kernel/core/agent/middleware/chain.py` | `MiddlewareChain`，26 | before 正序、after 逆序、wrap 洋葱 |
| 取消 | `kernel/core/agent/middleware/cancel_checker.py` | `CancelCheckerMiddleware`，19 | 每轮模型前检查 |
| 上下文预算 | `kernel/core/agent/middleware/context_compression.py` | `ContextCompressionMiddleware`，22 | 模型前压缩 |
| 模型错误 | `kernel/core/agent/middleware/error_retry.py` | `ErrorRetryMiddleware`，21 | 错误分类、紧急压缩和有限重试 |
| 过早结束 | `kernel/core/agent/middleware/transition_guard.py` | `TransitionGuardMiddleware`，19 | 拦截过渡性声明 |
| 工具错误 | `kernel/core/agent/middleware/tool_error.py` | `ToolErrorMiddleware`，259 | 透明重试与结构化 observation |
| 工具安全 | `kernel/core/agent/middleware/security_middleware.py` | `SecurityMiddleware`，149 | 高风险执行前门禁，异常时 fail closed |
| 开放工具补偿 | `kernel/core/services/task_runner.py` | `on_terminal_open_tool_results`，1256 | 外层终止前补齐 result |
| 权威终态 | `kernel/core/services/job/job_executor.py` | `_prepare_runtime_terminal`，674 附近及各终态出口 | timeout/cancel/fail/completed 事件收敛 |

## 3. 测试证据

### 3.1 本次通过的来源测试

运行目录：`/Users/anner/fine/ai/dev/kernel`

重复调用和任务意图门禁子集：

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q \
  tests/agent/test_react_tools_cache.py \
  -k 'repeat_guard or failure_cache or task_intent_block'
```

```text
8 passed, 30 deselected
```

覆盖重点：

- 分页参数不错误合并；
- 只读/写入成功的不同重复阈值；
- 可重试/不可重试失败；
- 失败缓存等待确认性重试；
- 任务意图拒绝写入结构化错误来源。

其他边界测试：

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q \
  tests/agent/test_user_waiting_tools.py \
  tests/agent/test_transition_guard_middleware.py \
  tests/agent/test_disallowed_tools_runtime.py \
  tests/agent/test_middleware_tool_lifecycle.py::test_tool_lifecycle_hooks_run_forward_then_reverse \
  tests/agent/test_middleware_tool_lifecycle.py::test_before_tool_call_can_return_blocking_result \
  ../test/unit-test/services/test_task_runner_terminal_tools.py
```

结果：

```text
10 passed, 3 failed
```

通过项证明：

- middleware before 正序、after 逆序；
- before-tool 可以阻断实际执行；
- 未暴露/禁用工具在执行前拒绝；
- Transition Guard 的 TODO 和子 Agent 输出门禁；
- 外层 timeout 对开放工具只补发一次结果。

### 3.2 当前失败和限制

失败文件：

```text
kernel/tests/agent/test_user_waiting_tools.py
```

失败的 3 个用例：

- `ask_user_question` 后停止循环；
- `exit_plan_mode` 后停止循环；
- 空响应有界重试后失败。

共同错误：

```text
AttributeError: 'ReactAgent' object has no attribute '_delegation_runtime_policy'
```

原因是测试通过 `ReactAgent.__new__()` 绕过构造函数，再手工设置大量字段；当前 `_run_loop` 已新增 `_delegation_runtime_policy` 依赖，但测试 factory 没有同步。

这不直接证明生产路径失效，但说明：

- 当前来源测试基线不是全绿；
- 巨型可变对象和半初始化测试夹具存在边界脆弱性；
- 本 CBB 功能域不能标记为 `已验证`。

来源仓库在测试后仍保持 `git status` 干净，本次没有修改来源。

### 3.3 未运行的来源测试

以下 API 测试包含真实 LLM 或环境依赖，本次采集没有执行：

- `test/api-test/agent/test_basic_chat.py`
- `test/api-test/agent/test_middleware.py`
- `test/api-test/agent/test_tool_error_degradation.py`
- `test/api-test/agent/test_checkpoint.py`
- `test/api-test/agent/test_tenant_isolation.py`

它们可作为后续真实验证入口，但不能把“文件存在”写成“本次已通过”。

### 3.4 CBB 示例测试

入口：

```bash
python3 -m unittest discover -s react/tests -v
```

2026-07-29 使用系统 `Python 3.9.6` 运行：

```text
Ran 11 tests
OK
```

示例同时保持 Python 3.11+ 可用；新项目仍应按自身运行时版本重新验证。每次修改示例后必须重跑。

## 4. 事故证据

| 主题 | 来源文档 | 可迁移结论 |
|---|---|---|
| 工具循环 | `docs/bug/20260617-agent-tool-loop-guard.md` | action key + 副作用感知软保护 |
| 政策工具重复 | `docs/bug/20260610-policy-tool-duplicate-calls.md` | 循环、缓存、Tool Search 和 schema 要联合治理 |
| Plan 跨 Pod 丢失 | `docs/bug/20260512-plan模式状态跨Pod丢失.md` | 跨请求会话状态需要共享权威源 |
| 安全早退未知失败 | `docs/bug/20260623-task-intent-guard-policy-error.md` | 每个拒绝出口都要有结构化错误来源 |
| 工具终态缺失 | `docs/bug/20260629-tool-timeout-sse-metrics.md` | 外层 Job 补齐开放工具 |
| 终态被 delta 阻塞 | `docs/bug/20260425-任务成功但会话管理显示失败-终态事件未发出.md` | best-effort 工作不能阻塞权威终态 |
| 工具元信息绕过 | `docs/bug/20260427-工具元信息查询绕过.md` | Prompt 不能作为唯一安全边界，需要多层防护 |

## 5. 提炼限制

- 来源 `ReactAgent` 同时承载 Moss 产品特有的 Skill、Tool Search、Subagent、Plan、Widget、文件、RuntimeEvent 和 Sandbox 行为。
- CBB 示例只提炼通用循环和边界，不复刻这些产品行为。
- 当前没有执行生产 trace 回放、真实 LLM 测试或多 Pod 测试。
- 事故文档描述的是对应日期的历史状态；当前代码可能已经修复，但迁移教训仍有效。
- 后续来源 SHA 变化时，应重新定位 symbol、运行测试并更新状态，不能只改 commit 字符串。
