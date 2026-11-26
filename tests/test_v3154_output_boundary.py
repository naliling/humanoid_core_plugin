"""Core v3.1.54 regression tests for an unmodified model-output boundary."""
from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from .fakes import FakeContext, FakeProvider
from .test_main_integration import GOOD_SCHEDULE, SKIP_REASON, FakeEvent, main_module

HUMAN = "10001"


class StaticSpeechBoundaryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        source = Path(__file__).resolve().parents[1] / "main.py"
        cls.tree = ast.parse(source.read_text(encoding="utf-8"))
        cls.plugin = next(
            node for node in cls.tree.body
            if isinstance(node, ast.ClassDef) and node.name == "HumanoidCore"
        )
        cls.methods = {
            node.name: node for node in cls.plugin.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

    def test_context_builder_is_not_registered_or_called(self) -> None:
        method = self.methods["inject_context"]
        self.assertEqual(method.decorator_list, [])
        calls = [
            node for node in ast.walk(self.tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "inject_context"
        ]
        self.assertEqual(calls, [])

    def test_only_nonmutating_request_hook_remains_registered(self) -> None:
        registered = []
        for method in self.methods.values():
            if any(
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "on_llm_request"
                for decorator in method.decorator_list
            ):
                registered.append(method.name)
        self.assertEqual(registered, ["show_input_state"])
        self.assertEqual(self.methods["guard_sibling_chat"].decorator_list, [])

        method = self.methods["show_input_state"]
        body_nodes = [node for stmt in method.body for node in ast.walk(stmt)]
        self.assertFalse(any(isinstance(node, ast.Name) and node.id == "req" for node in body_nodes))
        self.assertFalse(
            any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "stop_event"
                for node in body_nodes
            )
        )

    def test_response_hooks_never_write_or_replace_output(self) -> None:
        mutators = {"set_result", "set_result_content", "set_plain_text"}
        for name in ("note_her_reply", "capture_reply_on_send"):
            method = self.methods[name]
            for node in ast.walk(method):
                if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    self.assertFalse(
                        any(isinstance(t, ast.Attribute) and t.attr == "completion_text" for t in targets),
                        f"{name} must not assign completion_text",
                    )
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    self.assertNotIn(node.func.attr, mutators, f"{name} must not mutate the host result")


@unittest.skipIf(main_module is None, SKIP_REASON)
class RuntimeSpeechBoundaryTest(unittest.IsolatedAsyncioTestCase):
    async def _star(self):
        self.tmp = Path(tempfile.mkdtemp())
        provider = FakeProvider("p1", reply=GOOD_SCHEDULE)
        ctx = FakeContext(chat_providers=[provider])
        main_module.get_astrbot_data_path = lambda: str(self.tmp)
        star = main_module.HumanoidCore(ctx, {
            "timezone_city": "北京",
            "schedule_provider_name": "p1",
            "admin_qq": [HUMAN],
            "state_flush_interval_seconds": 1,
        })
        await star.initialize()
        self.addAsyncCleanup(star.terminate)
        return star

    async def test_repeated_text_is_not_trimmed(self) -> None:
        star = await self._star()
        original = "我在厨房呢，刚把碗洗完。"
        response = SimpleNamespace(completion_text=original)
        event = FakeEvent("在吗", sender=HUMAN)
        await star.note_her_reply(event, response)
        self.assertEqual(response.completion_text, original)
        self.assertTrue(star._core(event).mood.spoke(HUMAN))

    async def test_model_refusal_text_is_not_suppressed(self) -> None:
        star = await self._star()
        original = "Your request involves sexually suggestive roleplay. I am refusing to continue."
        response = SimpleNamespace(completion_text=original)
        await star.note_her_reply(FakeEvent("在吗", sender=HUMAN), response)
        self.assertEqual(response.completion_text, original)

    async def test_send_hook_leaves_host_result_unchanged(self) -> None:
        star = await self._star()
        original = "窗外的雪下大了，我把炉子添了块柴。"
        result = SimpleNamespace(
            result_content_type=SimpleNamespace(name="LLM_RESULT"),
            get_plain_text=lambda: original,
        )
        event = FakeEvent("在吗", sender=HUMAN)
        event.get_result = lambda: result
        await star.capture_reply_on_send(event)
        self.assertIs(event.get_result(), result)
        self.assertEqual(result.get_plain_text(), original)
        self.assertTrue(star._core(event).mood.spoke(HUMAN))
