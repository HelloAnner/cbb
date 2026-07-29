from __future__ import annotations

import asyncio
import json
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
import sys
from typing import Any

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
sys.path.insert(0, str(EXAMPLES_DIR))

from minimal_react_loop import (  # noqa: E402
    AgentLoop,
    AllowListPolicy,
    ModelTurn,
    PolicyDeniedError,
    RunContext,
    RunStatus,
    ToolRequest,
    ToolSpec,
)


class ScriptedModel:
    def __init__(self, turns: Sequence[ModelTurn]) -> None:
        self._turns = list(turns)
        self.calls: list[tuple[list[dict[str, Any]], list[Mapping[str, Any]]]] = []

    async def complete(self, messages, tools):
        self.calls.append(
            (
                [dict(message) for message in messages],
                list(tools),
            )
        )
        if not self._turns:
            raise AssertionError("model script exhausted")
        return self._turns.pop(0)


class RecordingEvents:
    def __init__(self) -> None:
        self.items: list[tuple[str, dict[str, Any]]] = []

    async def emit(self, event_type: str, payload: Mapping[str, Any]) -> None:
        self.items.append((event_type, dict(payload)))


class DenyDeletePolicy(AllowListPolicy):
    async def authorize_tool(self, context, tool, arguments) -> None:
        await super().authorize_tool(context, tool, arguments)
        if tool.name == "delete":
            raise PolicyDeniedError("delete requires an approval")


def run_context(*allowed_tools: str) -> RunContext:
    return RunContext(
        run_id="run-1",
        actor_id="user-1",
        tenant_id="tenant-1",
        allowed_tools=frozenset(allowed_tools),
    )


async def echo(arguments: Mapping[str, Any]) -> Any:
    return {"echo": arguments.get("value")}


class AgentLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_plain_text_finishes_successfully(self) -> None:
        model = ScriptedModel([ModelTurn(content="完成")])
        loop = AgentLoop(model=model, tools=[])

        result = await loop.run(run_context(), "回答问题")

        self.assertTrue(result.success)
        self.assertEqual(result.state.status, RunStatus.COMPLETED)
        self.assertEqual(result.state.output, "完成")
        self.assertEqual(len(result.state.iterations), 1)

    async def test_tool_result_is_paired_before_next_model_call(self) -> None:
        model = ScriptedModel(
            [
                ModelTurn(
                    content="我先查询",
                    tool_calls=(
                        ToolRequest(id="call-1", name="echo", arguments={"value": 7}),
                    ),
                ),
                ModelTurn(content="结果是 7"),
            ]
        )
        loop = AgentLoop(
            model=model,
            tools=[ToolSpec(name="echo", description="echo", handler=echo)],
        )

        result = await loop.run(run_context("echo"), "查询")

        self.assertTrue(result.success)
        second_input = model.calls[1][0]
        assistant = second_input[-2]
        observation = second_input[-1]
        self.assertEqual(assistant["role"], "assistant")
        self.assertEqual(assistant["tool_calls"][0]["id"], "call-1")
        self.assertEqual(observation["role"], "tool")
        self.assertEqual(observation["tool_call_id"], "call-1")
        self.assertEqual(json.loads(observation["content"])["value"]["echo"], 7)

    async def test_content_with_tool_calls_is_not_treated_as_final(self) -> None:
        model = ScriptedModel(
            [
                ModelTurn(
                    content="这不是最终回答",
                    tool_calls=(ToolRequest(id="call-1", name="echo"),),
                ),
                ModelTurn(content="最终回答"),
            ]
        )
        loop = AgentLoop(
            model=model,
            tools=[ToolSpec(name="echo", description="echo", handler=echo)],
        )

        result = await loop.run(run_context("echo"), "执行")

        self.assertEqual(len(model.calls), 2)
        self.assertEqual(result.state.output, "最终回答")

    async def test_unknown_tool_becomes_structured_observation(self) -> None:
        model = ScriptedModel(
            [
                ModelTurn(
                    tool_calls=(ToolRequest(id="missing-1", name="missing"),),
                ),
                ModelTurn(content="工具不可用"),
            ]
        )
        loop = AgentLoop(model=model, tools=[])

        result = await loop.run(run_context("missing"), "执行")

        self.assertTrue(result.success)
        error = json.loads(model.calls[1][0][-1]["content"])["error"]
        self.assertEqual(error["code"], "TOOL_NOT_FOUND")
        self.assertFalse(error["retryable"])

    async def test_policy_denial_never_calls_handler(self) -> None:
        called = False

        async def delete(_arguments):
            nonlocal called
            called = True
            return "deleted"

        model = ScriptedModel(
            [
                ModelTurn(
                    tool_calls=(ToolRequest(id="delete-1", name="delete"),),
                ),
                ModelTurn(content="已被拒绝"),
            ]
        )
        loop = AgentLoop(
            model=model,
            tools=[ToolSpec(name="delete", description="delete", handler=delete)],
            policy=DenyDeletePolicy(),
        )

        result = await loop.run(run_context("delete"), "删除")

        self.assertTrue(result.success)
        self.assertFalse(called)
        error = json.loads(model.calls[1][0][-1]["content"])["error"]
        self.assertEqual(error["code"], "TOOL_DENIED")

    async def test_max_iterations_is_not_success(self) -> None:
        model = ScriptedModel(
            [
                ModelTurn(tool_calls=(ToolRequest(id="call-1", name="echo"),)),
                ModelTurn(tool_calls=(ToolRequest(id="call-2", name="echo"),)),
            ]
        )
        loop = AgentLoop(
            model=model,
            tools=[ToolSpec(name="echo", description="echo", handler=echo)],
            max_iterations=2,
        )

        result = await loop.run(run_context("echo"), "循环")

        self.assertFalse(result.success)
        self.assertEqual(result.state.status, RunStatus.MAX_ITERATIONS_REACHED)
        self.assertEqual(result.state.error_code, "MAX_ITERATIONS_REACHED")

    async def test_empty_response_retries_then_fails(self) -> None:
        model = ScriptedModel([ModelTurn(), ModelTurn()])
        loop = AgentLoop(
            model=model,
            tools=[],
            empty_response_retries=1,
        )

        result = await loop.run(run_context(), "执行")

        self.assertFalse(result.success)
        self.assertEqual(result.state.status, RunStatus.FAILED)
        self.assertEqual(result.state.error_code, "MODEL_EMPTY_RESPONSE")
        self.assertEqual(len(model.calls), 2)

    async def test_cooperative_cancel_stops_before_model(self) -> None:
        model = ScriptedModel([ModelTurn(content="不应该调用")])
        loop = AgentLoop(
            model=model,
            tools=[],
            should_cancel=lambda: True,
        )

        result = await loop.run(run_context(), "执行")

        self.assertEqual(result.state.status, RunStatus.CANCELLED)
        self.assertEqual(model.calls, [])

    async def test_tool_timeout_is_observed_and_can_be_recovered(self) -> None:
        async def slow_tool(_arguments):
            await asyncio.sleep(1)
            return "late"

        model = ScriptedModel(
            [
                ModelTurn(tool_calls=(ToolRequest(id="slow-1", name="slow"),)),
                ModelTurn(content="工具超时，已降级"),
            ]
        )
        events = RecordingEvents()
        loop = AgentLoop(
            model=model,
            tools=[
                ToolSpec(
                    name="slow",
                    description="slow",
                    handler=slow_tool,
                    timeout_seconds=0.01,
                )
            ],
            events=events,
        )

        result = await loop.run(run_context("slow"), "执行")

        self.assertTrue(result.success)
        error = json.loads(model.calls[1][0][-1]["content"])["error"]
        self.assertEqual(error["code"], "TOOL_TIMEOUT")
        self.assertTrue(error["retryable"])
        self.assertEqual(
            [payload["status"] for event, payload in events.items if event == "tool.finished"],
            ["failed"],
        )

    async def test_external_cancel_propagates_and_closes_open_tool_event(self) -> None:
        entered = asyncio.Event()

        async def blocking_tool(_arguments):
            entered.set()
            await asyncio.Event().wait()

        model = ScriptedModel(
            [ModelTurn(tool_calls=(ToolRequest(id="block-1", name="block"),))]
        )
        events = RecordingEvents()
        loop = AgentLoop(
            model=model,
            tools=[ToolSpec(name="block", description="block", handler=blocking_tool)],
            events=events,
        )

        task = asyncio.create_task(loop.run(run_context("block"), "执行"))
        await entered.wait()
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task

        finished = [
            payload for event, payload in events.items if event == "tool.finished"
        ]
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0]["status"], "cancelled")

    async def test_duplicate_tool_call_id_fails_before_execution(self) -> None:
        called = False

        async def handler(_arguments):
            nonlocal called
            called = True

        model = ScriptedModel(
            [
                ModelTurn(
                    tool_calls=(
                        ToolRequest(id="dup", name="one"),
                        ToolRequest(id="dup", name="one"),
                    )
                )
            ]
        )
        loop = AgentLoop(
            model=model,
            tools=[ToolSpec(name="one", description="one", handler=handler)],
        )

        result = await loop.run(run_context("one"), "执行")

        self.assertFalse(called)
        self.assertEqual(result.state.status, RunStatus.FAILED)
        self.assertEqual(result.state.error_code, "DUPLICATE_TOOL_CALL_ID")


if __name__ == "__main__":
    unittest.main()
