from __future__ import annotations

import sys
import unittest
from pathlib import Path


EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
sys.path.insert(0, str(EXAMPLES_DIR))

from prompt_skill_tool_flow import (  # noqa: E402
    ConfigurationError,
    ExposureMode,
    PolicyError,
    RunSnapshot,
    SkillSpec,
    SkillStep,
    SkillToolRuntime,
    ToolSpec,
    demo_runtime,
)


class PromptSkillToolFlowTests(unittest.TestCase):
    def test_prompt_lists_only_enabled_skill_metadata(self) -> None:
        runtime = demo_runtime()

        prompt = runtime.build_system_prompt()

        self.assertIn('"id": "risk-analysis"', prompt)
        self.assertNotIn('"id": "disabled-writing"', prompt)
        self.assertNotIn("先确认分析主体和时间范围", prompt)
        self.assertIn("Skill 只提供方法论，不授予 Tool 权限", prompt)

    def test_enabled_skill_companion_tool_is_visible_when_authorized(self) -> None:
        runtime = demo_runtime()

        view = runtime.begin_model_turn()

        self.assertIn("risk_lookup", view.exposed_tools)
        self.assertNotIn("public_search", view.exposed_tools)
        self.assertNotIn("invoke_dynamic_tool", view.exposed_tools)

    def test_half_enabled_skill_configuration_fails_closed(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "半启用"):
            SkillToolRuntime(
                snapshot=RunSnapshot(
                    enabled_skills=frozenset({"risk"}),
                    authorized_tools=frozenset({"read"}),
                    exposure_mode=ExposureMode.DIRECT,
                    tool_search_enabled=False,
                ),
                skills=[
                    SkillSpec(
                        "risk",
                        "风险分析",
                        (SkillStep("查风险", capability="risk"),),
                    )
                ],
                tools=[
                    ToolSpec("read", "read_text", "读取", core=True),
                    ToolSpec(
                        "risk_lookup",
                        "risk",
                        "查风险",
                        companion_skill="risk",
                    ),
                ],
            )

    def test_skill_projection_uses_runtime_capabilities(self) -> None:
        runtime = demo_runtime()
        view = runtime.begin_model_turn()

        projection = runtime.load_skill(
            "risk-analysis",
            visible_tools=view.exposed_tools,
        )

        self.assertIn("当前可直接调用: risk_lookup", projection.content)
        self.assertIn("先调用 tool_search", projection.content)
        self.assertNotIn("secret_admin", projection.content)

    def test_skill_cannot_enable_an_unauthorized_tool(self) -> None:
        runtime = demo_runtime()

        with self.assertRaisesRegex(PolicyError, "TOOL_NOT_EXPOSED"):
            runtime.validate_model_tool_call(
                runtime.begin_model_turn(),
                "secret_admin",
            )

    def test_direct_search_result_is_visible_only_on_next_turn(self) -> None:
        runtime = demo_runtime(ExposureMode.DIRECT)
        first = runtime.begin_model_turn()

        loaded = runtime.perform_tool_search(
            view=first,
            candidate_names=["public_search"],
        )

        self.assertEqual(loaded, ("public_search",))
        self.assertNotIn("public_search", first.exposed_tools)
        with self.assertRaisesRegex(PolicyError, "TOOL_NOT_EXPOSED"):
            runtime.validate_model_tool_call(first, "public_search")

        second = runtime.begin_model_turn()
        self.assertIn("public_search", second.exposed_tools)
        runtime.validate_model_tool_call(second, "public_search")

    def test_tool_search_cannot_load_outside_authorized_snapshot(self) -> None:
        runtime = demo_runtime()
        first = runtime.begin_model_turn()

        loaded = runtime.perform_tool_search(
            view=first,
            candidate_names=["secret_admin", "missing", "public_search"],
        )

        self.assertEqual(loaded, ("public_search",))
        self.assertNotIn("secret_admin", runtime.loaded_search_tools)

    def test_dynamic_dispatch_requires_loaded_authorized_target(self) -> None:
        runtime = demo_runtime(ExposureMode.DYNAMIC)
        first = runtime.begin_model_turn()

        self.assertIn("invoke_dynamic_tool", first.exposed_tools)
        self.assertNotIn("public_search", first.exposed_tools)
        with self.assertRaisesRegex(PolicyError, "未加载或未授权"):
            runtime.validate_dynamic_dispatch(
                view=first,
                target_tool_name="public_search",
            )

        runtime.perform_tool_search(
            view=first,
            candidate_names=["public_search"],
        )
        second = runtime.begin_model_turn()
        runtime.validate_dynamic_dispatch(
            view=second,
            target_tool_name="public_search",
        )

    def test_prompt_describes_the_actual_exposure_mode(self) -> None:
        direct_runtime = demo_runtime(ExposureMode.DIRECT)
        dynamic_runtime = demo_runtime(ExposureMode.DYNAMIC)
        direct = direct_runtime.build_system_prompt()
        dynamic = dynamic_runtime.build_system_prompt()

        self.assertIn("下一次模型调用开始作为顶层工具可见", direct)
        self.assertNotIn("随后只能通过 invoke_dynamic_tool", direct)
        self.assertIn("随后只能通过 invoke_dynamic_tool", dynamic)
        self.assertNotIn(
            "invoke_dynamic_tool",
            direct_runtime.begin_model_turn().exposed_tools,
        )
        self.assertIn(
            "invoke_dynamic_tool",
            dynamic_runtime.begin_model_turn().exposed_tools,
        )

    def test_disabled_skill_body_cannot_be_loaded_from_stale_context(self) -> None:
        runtime = demo_runtime()

        with self.assertRaisesRegex(PolicyError, "SKILL_NOT_ENABLED"):
            runtime.load_skill("disabled-writing")

    def test_subagent_preexpansion_respects_budget(self) -> None:
        runtime = demo_runtime()
        full = runtime.preexpand_for_subagent(max_chars=10000)
        none = runtime.preexpand_for_subagent(max_chars=0)

        self.assertIn("# Skill: risk-analysis", full.content)
        self.assertEqual(full.skipped, ())
        self.assertEqual(none.content, "")
        self.assertEqual(none.skipped, ("risk-analysis",))

    def test_dynamic_mode_requires_search_and_dispatcher_authorization(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "必须启用 Tool Search"):
            SkillToolRuntime(
                snapshot=RunSnapshot(
                    enabled_skills=frozenset(),
                    authorized_tools=frozenset(),
                    exposure_mode=ExposureMode.DYNAMIC,
                    tool_search_enabled=False,
                ),
                skills=[],
                tools=[],
            )


if __name__ == "__main__":
    unittest.main()
