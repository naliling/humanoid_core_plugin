"""核心侧接上长期记忆：只读检索进事实块，外加把「她生活里的事」写回去。

记忆插件是别人的，这里只调它的两个入口（`compose_context` / `record_external_memory`），
所以这一层要守住的是**接得住、接不住也不塌**：

1. **没装就是没装。** 桥接只能从宿主注册表拿（`get_all_stars` / `get_registered_star`）。
   用 importlib 去 import 一个没装的插件，Python 会当场造一个空模块出来——「没装却说装
   了」，后面所有调用都变成静默失败。
2. **拿到的文本不能带标题。** 记忆插件拼出来的东西是给它自己用的，可能带【标题】和
   markdown 列表。事实块的规矩是「没有标题、没有标签」（见 `prompt_builder._sentence`），
   整段塞进去等于把念稿子的入口又开回来。
3. **热路径不能等。** `build_injection` 是同步的，检索只能在 `inject_context` 那个 async
   入口做完再传进来；取不到就是空串，事实块照常拼。
4. **失败要留痕、要有上限。** 记忆插件不在时不该每条消息都去翻一次注册表；同一个会话
   连着两条消息也不该查两次库。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from humanoid import memory_link
from humanoid.prompt_builder import MEMORY_TEXT_MAX_CHARS, _flatten_memory

from .test_main_integration import main_module
from .test_v2273_life_note import _Harness


class FlattenMemoryTest(unittest.TestCase):
    """把记忆插件拼的文本压成事实块里的一句片段。"""

    def test_a_heading_line_is_dropped(self):
        raw = "【你记得的关于 TA 的事·长期记忆，可以自然带一句，别当成清单念】\nTA 的猫叫团子"
        self.assertEqual(_flatten_memory(raw), "TA 的猫叫团子")

    def test_a_bulleted_heading_is_dropped_too(self):
        self.assertEqual(_flatten_memory("- 【长期记忆】\n猫叫团子"), "猫叫团子")

    def test_bullets_are_stripped_and_lines_are_joined(self):
        raw = "- 猫叫团子\n- 讨厌香菜\n3. 生日在三月"
        self.assertEqual(_flatten_memory(raw), "猫叫团子，讨厌香菜，生日在三月")

    def test_markdown_rules_and_hash_headings_go_away(self):
        self.assertEqual(_flatten_memory("### 长期记忆\n---\n猫叫团子"), "猫叫团子")

    def test_runs_of_punctuation_collapse(self):
        self.assertEqual(_flatten_memory("甲。\n。乙"), "甲，乙")

    def test_it_is_capped(self):
        out = _flatten_memory("甲" * 500)
        self.assertEqual(len(out), MEMORY_TEXT_MAX_CHARS + 1, "截断后应当只多一个省略号")
        self.assertTrue(out.endswith("…"))

    def test_nothing_left_means_empty(self):
        for raw in ("", "   ", None, "\n\n", "【只有标题】", "---"):
            self.assertEqual(_flatten_memory(raw), "", f"{raw!r} 应当什么都不剩")


class MemoryTextInTheFactsBlockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.h = _Harness()
        self.core = self.h.core

    def tearDown(self) -> None:
        self.h.store.flush_sync()

    def test_no_memory_text_means_no_fragment(self):
        block = self.core.build_injection("10001", memory_text="")
        self.assertNotIn("关于TA以前的事", block, "取不到就不该凭空多出一句")

    def test_memory_text_reaches_the_facts_block(self):
        block = self.core.build_injection("10001", memory_text="TA 的猫叫团子")
        self.assertIn("关于TA以前的事：TA 的猫叫团子", block)

    def test_the_fragment_brings_no_heading_of_its_own(self):
        block = self.core.build_injection(
            "10001", memory_text="【你记得的关于 TA 的事】\n- 猫叫团子\n- 讨厌香菜"
        )
        body = block.split("\n", 1)[1]
        self.assertNotIn("【", body)
        self.assertNotIn("】", body)
        self.assertNotIn("\n", body, "事实块是一句话，不能因为记忆文本被撑成好几行")

    def test_it_is_present_in_every_mode(self):
        for mode in ("mood_only", "medium", "full"):
            self.h.box.reload({"timezone_city": "北京", "inject_activity_context": mode})
            block = self.core.build_injection("10001", memory_text="TA 的猫叫团子")
            self.assertIn("关于TA以前的事", block, f"{mode} 档没带上长期记忆")

    def test_instruction_words_stay_out(self):
        block = self.core.build_injection("10001", memory_text="TA 的猫叫团子")
        for word in ("不要", "必须", "务必"):
            self.assertNotIn(word, block, f"事实块里不该出现指令词「{word}」")


class FindMemoryBridgeTest(unittest.TestCase):
    """拿桥接只能问注册表；问不到就认「没装」。"""

    @staticmethod
    def _meta(module, **kw):
        base = dict(
            name=memory_link.MEMORY_PLUGIN_ID,
            display_name="",
            root_dir_name="",
            module_path="",
            activated=True,
            module=module,
        )
        base.update(kw)
        return SimpleNamespace(**base)

    def test_no_registry_means_not_installed(self):
        self.assertIsNone(memory_link.find_memory_bridge(SimpleNamespace()))

    def test_none_context_is_none(self):
        self.assertIsNone(memory_link.find_memory_bridge(None))

    def test_a_registered_plugin_hands_back_its_bridge(self):
        bridge = object()
        module = SimpleNamespace(get_memory_companion_bridge=lambda: bridge)
        ctx = SimpleNamespace(get_all_stars=lambda: [self._meta(module)])
        self.assertIs(memory_link.find_memory_bridge(ctx), bridge)

    def test_another_plugin_is_not_mistaken_for_it(self):
        module = SimpleNamespace(get_memory_companion_bridge=lambda: object())
        ctx = SimpleNamespace(get_all_stars=lambda: [self._meta(module, name="astrbot_plugin_别的")])
        self.assertIsNone(memory_link.find_memory_bridge(ctx))

    def test_an_inactive_plugin_is_not_used(self):
        module = SimpleNamespace(get_memory_companion_bridge=lambda: object())
        ctx = SimpleNamespace(get_all_stars=lambda: [self._meta(module, activated=False)])
        self.assertIsNone(memory_link.find_memory_bridge(ctx))

    def test_a_registry_that_raises_is_survivable(self):
        def boom():
            raise RuntimeError("注册表坏了")

        self.assertIsNone(memory_link.find_memory_bridge(SimpleNamespace(get_all_stars=boom)))


class FetchMemoryTextTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        memory_link.clear_cache()

    async def test_it_returns_the_composed_text(self):
        class _Bridge:
            async def compose_context(self, **kw):
                return "TA 的猫叫团子"

        self.assertEqual(
            await memory_link.fetch_memory_text(_Bridge(), "umo", "u1", query="猫"),
            "TA 的猫叫团子",
        )

    async def test_the_query_and_bounds_reach_the_plugin(self):
        seen: dict = {}

        class _Bridge:
            async def compose_context(self, **kw):
                seen.update(kw)
                return "x"

        await memory_link.fetch_memory_text(_Bridge(), "umo", "u1", query="面试")
        self.assertEqual(seen["query"], "面试")
        self.assertEqual(seen["top_k"], memory_link.READ_TOP_K)
        self.assertEqual(seen["max_chars"], memory_link.READ_MAX_CHARS)
        self.assertEqual(seen["session_context"]["user_id"], "u1")

    async def test_a_broken_bridge_degrades_to_empty(self):
        class _Bad:
            async def compose_context(self, **kw):
                raise RuntimeError("坏了")

        self.assertEqual(await memory_link.fetch_memory_text(_Bad(), "umo", "u1"), "")

    async def test_a_bridge_without_the_method_degrades_to_empty(self):
        self.assertEqual(await memory_link.fetch_memory_text(object(), "umo", "u1"), "")

    async def test_the_same_session_is_asked_once(self):
        calls: list = []

        class _Bridge:
            async def compose_context(self, **kw):
                calls.append(1)
                return "x"

        bridge = _Bridge()
        await memory_link.fetch_memory_text(bridge, "umo", "u1")
        await memory_link.fetch_memory_text(bridge, "umo", "u1")
        self.assertEqual(len(calls), 1, "同一个会话连着两条消息不该查两次记忆库")

    async def test_no_user_means_no_lookup(self):
        class _Bridge:
            async def compose_context(self, **kw):
                raise AssertionError("不该被调用")

        self.assertEqual(await memory_link.fetch_memory_text(_Bridge(), "umo", ""), "")


class WriteLifeEventTest(unittest.IsolatedAsyncioTestCase):
    class _Recorder:
        def __init__(self) -> None:
            self.calls: list = []

        async def record_external_memory(self, **kw):
            self.calls.append(kw)

    async def test_it_writes_an_event_under_her_own_name(self):
        bridge = self._Recorder()
        await memory_link.write_life_event(bridge, "u1", "整理完了那份个案", umo="umo", key="p1")
        call = bridge.calls[0]
        self.assertIn("整理完了那份个案", call["content"])
        self.assertEqual(call["source_plugin"], memory_link.SOURCE_PLUGIN)
        self.assertEqual(call["tags"], ["life"])
        self.assertEqual(call["idempotency_key"], "life:umo:p1")

    async def test_a_stable_key_keeps_replays_idempotent(self):
        first, second = self._Recorder(), self._Recorder()
        await memory_link.write_life_event(first, "u1", "同一件事", umo="umo", key="p1")
        await memory_link.write_life_event(second, "u1", "同一件事", umo="umo", key="p1")
        self.assertEqual(
            first.calls[0]["idempotency_key"], second.calls[0]["idempotency_key"]
        )

    async def test_without_a_key_the_content_decides(self):
        first, second = self._Recorder(), self._Recorder()
        await memory_link.write_life_event(first, "u1", "甲", umo="umo")
        await memory_link.write_life_event(second, "u1", "乙", umo="umo")
        self.assertNotEqual(
            first.calls[0]["idempotency_key"], second.calls[0]["idempotency_key"]
        )

    async def test_empty_content_writes_nothing(self):
        bridge = self._Recorder()
        await memory_link.write_life_event(bridge, "u1", "   ")
        self.assertEqual(bridge.calls, [])

    async def test_a_broken_plugin_does_not_raise(self):
        class _Bad:
            async def record_external_memory(self, **kw):
                raise RuntimeError("坏了")

        await memory_link.write_life_event(_Bad(), "u1", "甲")


class PluginSideBridgeTest(unittest.TestCase):
    """插件侧那层缓存：没装时不该每条消息都翻一次注册表。"""

    def _plugin(self, ctx):
        obj = main_module.HumanoidCore.__new__(main_module.HumanoidCore)
        obj.context = ctx
        obj._memory_bridge_resolved = False
        obj._memory_bridge_obj = None
        return obj

    def test_a_missing_plugin_resolves_to_none_and_is_not_retried(self):
        calls: list = []

        class _Ctx:
            def get_all_stars(self):
                calls.append(1)
                return []

        obj = self._plugin(_Ctx())
        self.assertIsNone(obj._memory_bridge())
        self.assertIsNone(obj._memory_bridge())
        self.assertEqual(len(calls), 1, "没装时不该每条消息都去翻一次注册表")

    def test_the_bridge_is_handed_out_once(self):
        bridge = object()
        module = SimpleNamespace(get_memory_companion_bridge=lambda: bridge)
        meta = SimpleNamespace(
            name=memory_link.MEMORY_PLUGIN_ID,
            display_name="",
            root_dir_name="",
            module_path="",
            activated=True,
            module=module,
        )
        obj = self._plugin(SimpleNamespace(get_all_stars=lambda: [meta]))
        self.assertIs(obj._memory_bridge(), bridge)
        self.assertIs(obj._memory_bridge(), bridge)


class FinishedProcessWriteTest(unittest.IsolatedAsyncioTestCase):
    """她刚做完一件事 → 交一条给记忆库。这件事在聊天里看不见，只有她知道。"""

    class _Recorder:
        def __init__(self) -> None:
            self.calls: list = []

        async def record_external_memory(self, **kw):
            self.calls.append(kw)

    class _Process:
        def __init__(self, name: str) -> None:
            self.name = name

        def recent(self, limit=None):
            return [self.name] if self.name else []

    class _Core:
        role_id = "bot1"

        def __init__(self, name: str) -> None:
            self.process = FinishedProcessWriteTest._Process(name)

    def _plugin(self, bridge):
        obj = main_module.HumanoidCore.__new__(main_module.HumanoidCore)
        obj._memory_bridge_obj = bridge
        obj._memory_bridge_resolved = True
        obj._last_written_process = {}
        obj._life_write_stamps = {}
        return obj

    async def test_the_first_sighting_only_records(self):
        rec = self._Recorder()
        obj = self._plugin(rec)
        await obj._write_finished_process(self._Core("写方案"), "umo", "u1")
        self.assertEqual(rec.calls, [], "重启后第一次看到的名字不该当成刚做完")

    async def test_a_new_name_is_written_once(self):
        rec = self._Recorder()
        obj = self._plugin(rec)
        await obj._write_finished_process(self._Core("写方案"), "umo", "u1")
        await obj._write_finished_process(self._Core("整理个案"), "umo", "u1")
        self.assertEqual(len(rec.calls), 1)
        self.assertIn("整理个案", rec.calls[0]["content"])
        self.assertEqual(rec.calls[0]["tags"], ["life"])
        await obj._write_finished_process(self._Core("整理个案"), "umo", "u1")
        self.assertEqual(len(rec.calls), 1, "同一件事不该写第二条")

    async def test_a_day_is_capped(self):
        rec = self._Recorder()
        obj = self._plugin(rec)
        await obj._write_finished_process(self._Core("第零件"), "umo", "u1")  # 只记账
        for i in range(6):
            await obj._write_finished_process(self._Core(f"第{i}件"), "umo", "u1")
        self.assertEqual(len(rec.calls), obj.LIFE_WRITE_PER_DAY)

    async def test_no_bridge_and_no_user_are_both_silent(self):
        obj = self._plugin(None)
        await obj._write_finished_process(self._Core("写方案"), "umo", "u1")
        rec = self._Recorder()
        obj = self._plugin(rec)
        await obj._write_finished_process(self._Core("写方案"), "umo", "u1")
        await obj._write_finished_process(self._Core("整理个案"), "umo", "")
        self.assertEqual(rec.calls, [])

    async def test_a_broken_plugin_does_not_raise(self):
        class _Bad:
            async def record_external_memory(self, **kw):
                raise RuntimeError("坏了")

        obj = self._plugin(_Bad())
        await obj._write_finished_process(self._Core("写方案"), "umo", "u1")
        await obj._write_finished_process(self._Core("整理个案"), "umo", "u1")


if __name__ == "__main__":
    unittest.main()
