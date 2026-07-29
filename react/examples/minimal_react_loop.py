"""一个边界清晰、可测试的最小 ReAct Agent Loop。

本示例根据 Moss/Corevo Platform 的成熟实现提炼，但不是源码复制。它只保留
ReAct 循环本身必须承担的职责：

1. 调用模型；
2. 将模型产生的工具调用交给受控工具边界；
3. 把每个工具结果按 call_id 写回上下文；
4. 在有界迭代、超时、取消和显式终态之间收敛。

生产系统中的 Job 持久化、权威终态事件、沙箱、上下文压缩、重试策略、子 Agent
和流式协议属于循环外部。具体边界见 ../BOUNDARIES.md。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

Message = dict[str, Any]
ToolHandler = Callable[[Mapping[str, Any]], Awaitable[Any]]
ToolValidator = Callable[[Mapping[str, Any]], dict[str, Any]]


class RunStatus(str, Enum):
    """循环自身可判断的终态；外层 Job 可以映射成自己的状态协议。"""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    MAX_ITERATIONS_REACHED = "max_iterations_reached"


@dataclass(frozen=True)
class RunContext:
    """由外层运行时提供的身份和本次授权快照。"""

    run_id: str
    actor_id: str
    tenant_id: str
    allowed_tools: frozenset[str]


@dataclass(frozen=True)
class ToolRequest:
    """模型在一轮中提出的一次工具调用。"""

    id: str
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelTurn:
    """模型的一轮输出。

    content 和 tool_calls 可以同时存在；只要存在 tool_calls，本轮就不能直接作为
    最终完成态，必须先执行工具并把 observation 交回下一轮。
    """

    content: str | None = None
    tool_calls: tuple[ToolRequest, ...] = ()


@dataclass(frozen=True)
class ToolOutcome:
    """一次工具调用的结构化终态。"""

    call_id: str
    tool_name: str
    ok: bool
    value: Any = None
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False

    def for_model(self) -> str:
        payload: dict[str, Any] = {"ok": self.ok}
        if self.ok:
            payload["value"] = self.value
        else:
            payload["error"] = {
                "code": self.error_code,
                "message": self.error_message,
                "retryable": self.retryable,
            }
        return json.dumps(payload, ensure_ascii=False, default=str)


@dataclass
class IterationRecord:
    """一轮模型输出以及由它触发的全部工具结果。"""

    number: int
    model_turn: ModelTurn
    tool_outcomes: list[ToolOutcome] = field(default_factory=list)


@dataclass
class RunState:
    """循环状态只在单次 run 内创建，禁止跨 run 或跨租户共享。"""

    status: RunStatus = RunStatus.RUNNING
    iterations: list[IterationRecord] = field(default_factory=list)
    output: str = ""
    error_code: str | None = None
    error_message: str | None = None

    @property
    def success(self) -> bool:
        return self.status is RunStatus.COMPLETED

    def finish(
        self,
        status: RunStatus,
        *,
        output: str = "",
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        if self.status is not RunStatus.RUNNING:
            raise RuntimeError(f"terminal state is immutable: {self.status}")
        if status is RunStatus.RUNNING:
            raise ValueError("finish() requires a terminal status")
        self.status = status
        self.output = output
        self.error_code = error_code
        self.error_message = error_message


@dataclass(frozen=True)
class RunResult:
    """循环返回值；外层运行时负责持久化并发送权威终态事件。"""

    state: RunState
    messages: tuple[Message, ...]

    @property
    def success(self) -> bool:
        return self.state.success


class ModelPort(Protocol):
    """模型适配器边界。

    Provider 特有的流式事件、tool-call 拼装和 finish_reason 解析应留在适配器，
    不进入 AgentLoop。
    """

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[Mapping[str, Any]],
    ) -> ModelTurn: ...


class LoopPolicy(Protocol):
    """上下文治理和工具授权边界。"""

    async def prepare_model_input(
        self,
        context: RunContext,
        state: RunState,
        messages: Sequence[Message],
    ) -> Sequence[Message]: ...

    async def authorize_tool(
        self,
        context: RunContext,
        tool: "ToolSpec",
        arguments: Mapping[str, Any],
    ) -> None: ...


class LoopEventSink(Protocol):
    """循环内部的观测事件，不等同于外层 Job 权威终态。"""

    async def emit(self, event_type: str, payload: Mapping[str, Any]) -> None: ...


class NullEventSink:
    async def emit(self, event_type: str, payload: Mapping[str, Any]) -> None:
        del event_type, payload


class PolicyDeniedError(Exception):
    """工具调用没有通过本次运行的授权策略。"""


class AllowListPolicy:
    """最小的默认拒绝策略。

    工具既要出现在 RunContext.allowed_tools 中，也要通过此处的授权检查。生产系统
    应在此基础上加入租户、用户、资源、计划模式和数据权限判断。
    """

    async def prepare_model_input(
        self,
        context: RunContext,
        state: RunState,
        messages: Sequence[Message],
    ) -> Sequence[Message]:
        del context, state
        return tuple(dict(message) for message in messages)

    async def authorize_tool(
        self,
        context: RunContext,
        tool: "ToolSpec",
        arguments: Mapping[str, Any],
    ) -> None:
        del arguments
        if tool.name not in context.allowed_tools:
            raise PolicyDeniedError(f"tool is not allowed in this run: {tool.name}")


def _copy_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    return dict(arguments)


@dataclass(frozen=True)
class ToolSpec:
    """工具定义快照。

    side_effect 和 parallel_safe 是调度策略必需信息。最小示例保守地串行执行所有
    工具；生产系统只有在工具显式声明 parallel_safe 时才可并行。
    """

    name: str
    description: str
    handler: ToolHandler
    input_schema: Mapping[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    validate: ToolValidator = _copy_arguments
    timeout_seconds: float = 30.0
    side_effect: str = "unknown"
    parallel_safe: bool = False

    def for_model(self) -> Mapping[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": dict(self.input_schema),
        }


class AgentLoop:
    """与框架无关的 ReAct 循环骨架。"""

    def __init__(
        self,
        *,
        model: ModelPort,
        tools: Sequence[ToolSpec],
        policy: LoopPolicy | None = None,
        events: LoopEventSink | None = None,
        max_iterations: int = 20,
        run_timeout_seconds: float = 300.0,
        model_timeout_seconds: float = 60.0,
        empty_response_retries: int = 1,
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:
        if max_iterations <= 0:
            raise ValueError("max_iterations must be positive")
        if run_timeout_seconds <= 0 or model_timeout_seconds <= 0:
            raise ValueError("timeouts must be positive")
        if empty_response_retries < 0:
            raise ValueError("empty_response_retries cannot be negative")

        tool_map = {tool.name: tool for tool in tools}
        if len(tool_map) != len(tools):
            raise ValueError("tool names must be unique")

        self._model = model
        self._tools = tool_map
        self._policy = policy or AllowListPolicy()
        self._events = events or NullEventSink()
        self._max_iterations = max_iterations
        self._run_timeout_seconds = run_timeout_seconds
        self._model_timeout_seconds = model_timeout_seconds
        self._empty_response_retries = empty_response_retries
        self._should_cancel = should_cancel or (lambda: False)

    async def run(
        self,
        context: RunContext,
        task: str,
        *,
        system_prompt: str | None = None,
        history: Sequence[Message] = (),
    ) -> RunResult:
        """运行一次 ReAct 循环。

        asyncio.CancelledError 不会被转换成普通失败；它会在关闭正在执行的工具
        观测事件后继续向外传播，由 Job Runner 判定是用户取消、超时还是进程退出。
        """

        state = RunState()
        messages = [dict(message) for message in history]
        if system_prompt:
            messages.insert(0, {"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": task})

        deadline = time.monotonic() + self._run_timeout_seconds
        empty_responses = 0

        for iteration_number in range(1, self._max_iterations + 1):
            if self._should_cancel():
                state.finish(
                    RunStatus.CANCELLED,
                    error_code="RUN_CANCELLED",
                    error_message="run was cooperatively cancelled",
                )
                return RunResult(state=state, messages=tuple(messages))

            try:
                prepared_messages = await self._await_bounded(
                    lambda: self._policy.prepare_model_input(context, state, messages),
                    self._model_timeout_seconds,
                    deadline,
                )
            except asyncio.CancelledError:
                raise
            except (TimeoutError, asyncio.TimeoutError):
                state.finish(
                    RunStatus.FAILED,
                    error_code="CONTEXT_PREPARATION_TIMEOUT",
                    error_message="context preparation exceeded its deadline",
                )
                return RunResult(state=state, messages=tuple(messages))
            except Exception:
                state.finish(
                    RunStatus.FAILED,
                    error_code="CONTEXT_PREPARATION_FAILED",
                    error_message="context preparation failed",
                )
                return RunResult(state=state, messages=tuple(messages))

            exposed_tools = tuple(
                tool.for_model()
                for name, tool in self._tools.items()
                if name in context.allowed_tools
            )
            try:
                turn = await self._await_bounded(
                    lambda: self._model.complete(prepared_messages, exposed_tools),
                    self._model_timeout_seconds,
                    deadline,
                )
            except asyncio.CancelledError:
                raise
            except (TimeoutError, asyncio.TimeoutError):
                state.finish(
                    RunStatus.FAILED,
                    error_code="MODEL_OR_RUN_TIMEOUT",
                    error_message="model call or run deadline exceeded",
                )
                return RunResult(state=state, messages=tuple(messages))
            except Exception:
                state.finish(
                    RunStatus.FAILED,
                    error_code="MODEL_CALL_FAILED",
                    error_message="model call failed",
                )
                return RunResult(state=state, messages=tuple(messages))

            record = IterationRecord(number=iteration_number, model_turn=turn)

            if turn.tool_calls:
                duplicate_id = _first_duplicate_call_id(turn.tool_calls)
                if duplicate_id:
                    state.iterations.append(record)
                    state.finish(
                        RunStatus.FAILED,
                        error_code="DUPLICATE_TOOL_CALL_ID",
                        error_message=f"duplicate tool call id: {duplicate_id}",
                    )
                    return RunResult(state=state, messages=tuple(messages))

                messages.append(_assistant_tool_message(turn))

                for index, call in enumerate(turn.tool_calls):
                    if self._should_cancel():
                        for pending in turn.tool_calls[index:]:
                            outcome = ToolOutcome(
                                call_id=pending.id,
                                tool_name=pending.name,
                                ok=False,
                                error_code="TOOL_CANCELLED",
                                error_message="run was cancelled before tool execution",
                            )
                            record.tool_outcomes.append(outcome)
                            messages.append(_tool_result_message(outcome))
                        break

                    outcome = await self._execute_tool(
                        context=context,
                        call=call,
                        iteration_number=iteration_number,
                        deadline=deadline,
                    )
                    record.tool_outcomes.append(outcome)
                    messages.append(_tool_result_message(outcome))

                state.iterations.append(record)

                if self._should_cancel():
                    state.finish(
                        RunStatus.CANCELLED,
                        error_code="RUN_CANCELLED",
                        error_message="run was cooperatively cancelled",
                    )
                    return RunResult(state=state, messages=tuple(messages))

                # 有工具调用时，即使同时存在 content，也必须把 observation 交给下一轮。
                continue

            content = (turn.content or "").strip()
            if content:
                messages.append({"role": "assistant", "content": turn.content})
                state.iterations.append(record)
                state.finish(RunStatus.COMPLETED, output=turn.content or "")
                return RunResult(state=state, messages=tuple(messages))

            state.iterations.append(record)
            if empty_responses < self._empty_response_retries:
                empty_responses += 1
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "上一轮既没有正文也没有工具调用。请输出最终回答，"
                            "或调用工具继续完成任务。"
                        ),
                    }
                )
                continue

            state.finish(
                RunStatus.FAILED,
                error_code="MODEL_EMPTY_RESPONSE",
                error_message="model returned no content and no tool calls",
            )
            return RunResult(state=state, messages=tuple(messages))

        state.finish(
            RunStatus.MAX_ITERATIONS_REACHED,
            error_code="MAX_ITERATIONS_REACHED",
            error_message=f"run stopped after {self._max_iterations} iterations",
        )
        return RunResult(state=state, messages=tuple(messages))

    async def _execute_tool(
        self,
        *,
        context: RunContext,
        call: ToolRequest,
        iteration_number: int,
        deadline: float,
    ) -> ToolOutcome:
        await self._events.emit(
            "tool.started",
            {
                "run_id": context.run_id,
                "iteration": iteration_number,
                "call_id": call.id,
                "tool_name": call.name,
            },
        )
        terminal_event_emitted = False

        try:
            tool = self._tools.get(call.name)
            if tool is None:
                outcome = ToolOutcome(
                    call_id=call.id,
                    tool_name=call.name,
                    ok=False,
                    error_code="TOOL_NOT_FOUND",
                    error_message="requested tool is not registered",
                )
            else:
                try:
                    arguments = tool.validate(call.arguments)
                except (TypeError, ValueError):
                    outcome = ToolOutcome(
                        call_id=call.id,
                        tool_name=call.name,
                        ok=False,
                        error_code="TOOL_ARGUMENTS_INVALID",
                        error_message="tool arguments did not pass validation",
                    )
                else:
                    try:
                        await self._policy.authorize_tool(context, tool, arguments)
                    except PolicyDeniedError:
                        outcome = ToolOutcome(
                            call_id=call.id,
                            tool_name=call.name,
                            ok=False,
                            error_code="TOOL_DENIED",
                            error_message="tool call was denied by runtime policy",
                        )
                    except Exception:
                        # 策略基础设施异常时 fail closed，不能绕过授权继续执行。
                        outcome = ToolOutcome(
                            call_id=call.id,
                            tool_name=call.name,
                            ok=False,
                            error_code="TOOL_POLICY_FAILED",
                            error_message="tool policy check failed; execution denied",
                        )
                    else:
                        try:
                            value = await self._await_bounded(
                                lambda: tool.handler(arguments),
                                tool.timeout_seconds,
                                deadline,
                            )
                            outcome = ToolOutcome(
                                call_id=call.id,
                                tool_name=call.name,
                                ok=True,
                                value=value,
                            )
                        except asyncio.CancelledError:
                            await self._events.emit(
                                "tool.finished",
                                {
                                    "run_id": context.run_id,
                                    "iteration": iteration_number,
                                    "call_id": call.id,
                                    "tool_name": call.name,
                                    "status": "cancelled",
                                },
                            )
                            terminal_event_emitted = True
                            raise
                        except (TimeoutError, asyncio.TimeoutError):
                            outcome = ToolOutcome(
                                call_id=call.id,
                                tool_name=call.name,
                                ok=False,
                                error_code="TOOL_TIMEOUT",
                                error_message="tool execution timed out",
                                retryable=True,
                            )
                        except Exception:
                            outcome = ToolOutcome(
                                call_id=call.id,
                                tool_name=call.name,
                                ok=False,
                                error_code="TOOL_EXECUTION_FAILED",
                                error_message="tool execution failed",
                            )

            await self._events.emit(
                "tool.finished",
                {
                    "run_id": context.run_id,
                    "iteration": iteration_number,
                    "call_id": call.id,
                    "tool_name": call.name,
                    "status": "succeeded" if outcome.ok else "failed",
                    "error_code": outcome.error_code,
                },
            )
            terminal_event_emitted = True
            return outcome
        except asyncio.CancelledError:
            # 如果取消发生在 started 之后、handler 之前，也要补齐本地观测终态；
            # handler 分支已经发过时则避免重复。
            if not terminal_event_emitted:
                await self._events.emit(
                    "tool.finished",
                    {
                        "run_id": context.run_id,
                        "iteration": iteration_number,
                        "call_id": call.id,
                        "tool_name": call.name,
                        "status": "cancelled",
                    },
                )
            raise

    @staticmethod
    async def _await_bounded(
        call: Callable[[], Awaitable[Any]],
        per_call_timeout: float,
        deadline: float,
    ) -> Any:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("run deadline exceeded")
        timeout = min(per_call_timeout, remaining)
        return await asyncio.wait_for(call(), timeout=timeout)


def _first_duplicate_call_id(calls: Sequence[ToolRequest]) -> str | None:
    seen: set[str] = set()
    for call in calls:
        if call.id in seen:
            return call.id
        seen.add(call.id)
    return None


def _assistant_tool_message(turn: ModelTurn) -> Message:
    return {
        "role": "assistant",
        "content": turn.content,
        "tool_calls": [
            {
                "id": call.id,
                "name": call.name,
                "arguments": dict(call.arguments),
            }
            for call in turn.tool_calls
        ],
    }


def _tool_result_message(outcome: ToolOutcome) -> Message:
    return {
        "role": "tool",
        "tool_call_id": outcome.call_id,
        "name": outcome.tool_name,
        "content": outcome.for_model(),
    }


__all__ = [
    "AgentLoop",
    "AllowListPolicy",
    "LoopEventSink",
    "LoopPolicy",
    "ModelPort",
    "ModelTurn",
    "PolicyDeniedError",
    "RunContext",
    "RunResult",
    "RunState",
    "RunStatus",
    "ToolOutcome",
    "ToolRequest",
    "ToolSpec",
]
