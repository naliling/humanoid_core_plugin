"""v3.1.45：出站照抄闸——回复里逐字照抄「本轮注入背景」的小句要删掉。

注入侧（第一人称、频率闸门、去重、字数预算、数值不进上下文）做得再干净，也挡不住
弱模型把刚递给它的原文当台词照抄。这道闸在**出站**比对本轮注入原文，只删句不换词。

锁三件事：

* 纯函数 `trim_injection_echo`：照抄的长句删、她自己新生成的话留；短句/称呼/结构抬头
  不动；整条都在照抄时原样返回（不发空）；没注入块时一个字不动。
* 线上接线：`inject_context` 把本轮原文存到 event，`note_her_reply` 里真的把删过的
  文本写回 `response.completion_text`。
* 开关：关掉 `injection_echo_guard_enabled` 时这道闸不生效。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from humanoid import reply_guard

from .fakes import FakeContext, FakeProvider
from .test_main_integration import GOOD_SCHEDULE, SKIP_REASON, FakeEvent, main_module

HUMAN = "10001"


class _Response:
    def __init__(self, text: str) -> None:
        self.completion_text = text


class TrimInjectionEchoTest(unittest.TestCase):
    """纯函数：照抄的长句删，其余不动。"""

    def test_verbatim_injected_clause_is_dropped(self):
        blocks = ["〔我现在的身体 v13〕\n我这两天小腹坠着，腰也酸，人有点发懒。"]
        text = "我这两天小腹坠着，腰也酸，你今天过得怎么样？"
        new, dropped = reply_guard.trim_injection_echo(text, blocks)
        self.assertNotIn("小腹坠着", new)
        self.assertIn("你今天过得怎么样", new)
        self.assertTrue(dropped)

    def test_a_weather_line_copied_verbatim_is_dropped(self):
        blocks = ["【我现在的情况】\n今天外面下着雪，风有点大。"]
        text = "今天外面下着雪，风有点大，要不要听我说件事？"
        new, dropped = reply_guard.trim_injection_echo(text, blocks)
        self.assertNotIn("下着雪", new)
        self.assertIn("要不要听我说件事", new)
        self.assertTrue(dropped)

    def test_her_own_new_words_are_untouched(self):
        blocks = ["〔我现在的身体〕\n我这两天小腹坠着，腰也酸。"]
        text = "哼，你是不是故意气我的？"
        new, dropped = reply_guard.trim_injection_echo(text, blocks)
        self.assertEqual(new, text)
        self.assertEqual(dropped, [])

    def test_no_injected_blocks_changes_nothing(self):
        text = "我这两天小腹坠着，腰也酸。"
        self.assertEqual(reply_guard.trim_injection_echo(text, []), (text, []))

    def test_empty_injected_block_changes_nothing(self):
        text = "我这两天小腹坠着，腰也酸。"
        self.assertEqual(reply_guard.trim_injection_echo(text, ["", "   "]), (text, []))

    def test_short_injected_line_is_not_a_match(self):
        # 「我有点困」只有 4 字，逐字说出来是正常的，不该被当照抄删掉。
        blocks = ["我有点困"]
        text = "我有点困，想先躺一会儿。"
        new, dropped = reply_guard.trim_injection_echo(text, blocks)
        self.assertEqual(new, text)
        self.assertEqual(dropped, [])

    def test_whole_reply_is_echo_still_returns_original(self):
        # 整条都在照抄（一个单一长句，删了就一句不剩）：宁可复读，也不发空消息。
        single = "我这两天小腹坠着腰也酸人有点发懒"
        new, dropped = reply_guard.trim_injection_echo(single, [single])
        self.assertEqual(new.strip(), single)
        self.assertTrue(dropped)

    def test_structural_headers_are_not_treated_as_echo(self):
        # 结构抬头（〔〕【】）不是她说的话，不该参与比对，也不该被删。
        blocks = ["〔我现在的身体 v13〕"]
        text = "〔我现在的身体 v13〕今天过得怎么样？"
        new, dropped = reply_guard.trim_injection_echo(text, blocks)
        self.assertIn("我现在的身体", new)
        self.assertEqual(dropped, [])

    def test_address_is_kept(self):
        # 每句都叫对方名字是称呼，不是照抄。
        blocks = ["Regnator今天有点累，想歇一会儿"]
        text = "Regnator，我刚泡了壶热红茶。"
        new, dropped = reply_guard.trim_injection_echo(text, blocks)
        self.assertIn("Regnator", new)
        self.assertEqual(dropped, [])

    def test_partial_overlap_needs_both_sides_long(self):
        # 一边短一边长不算照抄——否则长句里任意一小段都会被误删。
        blocks = ["我今天下午去了一趟超市买东西"]
        text = "去了一趟？"
        new, dropped = reply_guard.trim_injection_echo(text, blocks)
        self.assertEqual(new, text)
        self.assertEqual(dropped, [])


@unittest.skipIf(main_module is None, SKIP_REASON)
class InjectionEchoWiringTest(unittest.IsolatedAsyncioTestCase):
    """线上接线：注入原文存到 event，出站把删过的文本写回 completion_text。"""

    async def _star(self, **raw):
        self.tmp = Path(tempfile.mkdtemp())
        provider = FakeProvider("p1", reply=GOOD_SCHEDULE)
        ctx = FakeContext(chat_providers=[provider])
        base = {
            "timezone_city": "北京",
            "schedule_provider_name": "p1",
            "admin_qq": [HUMAN],
            "state_flush_interval_seconds": 1,
        }
        base.update(raw)
        main_module.get_astrbot_data_path = lambda: str(self.tmp)
        star = main_module.HumanoidCore(ctx, base)
        await star.initialize()
        self.addAsyncCleanup(star.terminate)
        return star

    async def test_echo_is_stripped_from_sent_text_when_event_has_blocks(self):
        star = await self._star()
        event = FakeEvent("在吗", sender=HUMAN)
        # 模拟本轮注入过的原文（真实路径由 inject_context 存进来）。
        setter = getattr(event, "set_extra", None)
        if callable(setter):
            setter("_hcore_injected_blocks", ["我这两天小腹坠着，腰也酸，人有点发懒"])
        response = _Response("我这两天小腹坠着，腰也酸，你今天过得怎样？")
        await star.note_her_reply(event, response)
        self.assertNotIn("小腹坠着", response.completion_text)
        self.assertIn("你今天过得怎样", response.completion_text)

    async def test_unique_reply_is_untouched_even_with_blocks(self):
        star = await self._star()
        event = FakeEvent("在吗", sender=HUMAN)
        setter = getattr(event, "set_extra", None)
        if callable(setter):
            setter("_hcore_injected_blocks", ["我这两天小腹坠着，腰也酸"])
        response = _Response("窗外的雪下大了，我把炉子添了块柴。")
        await star.note_her_reply(event, response)
        self.assertEqual(response.completion_text, "窗外的雪下大了，我把炉子添了块柴。")

    async def test_guard_can_be_disabled(self):
        star = await self._star(injection_echo_guard_enabled=False)
        event = FakeEvent("在吗", sender=HUMAN)
        setter = getattr(event, "set_extra", None)
        if callable(setter):
            setter("_hcore_injected_blocks", ["我这两天小腹坠着，腰也酸，人有点发懒"])
        original = "我这两天小腹坠着，腰也酸，你今天过得怎样？"
        response = _Response(original)
        await star.note_her_reply(event, response)
        self.assertEqual(response.completion_text, original)


if __name__ == "__main__":
    unittest.main()
