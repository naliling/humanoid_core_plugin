"""v3.1.31：出站复读拦截——她自己说过的话，短窗口内不许原样再来一遍。

锁两件事：

* 纯函数 `reply_guard.trim_repeats`：地点复读与小句复读都删句；删完一句不剩时
  原样返回（不发空消息）；没历史时一个字都不动。
* 线上接线：`note_her_reply` 里真的把删过的文本写回 `response.completion_text`，
  宿主发出去的就是删过的版本，并且账本记的也是删过的那句。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from humanoid import reply_guard

from .fakes import FakeContext, FakeProvider
from .test_main_integration import GOOD_SCHEDULE, SKIP_REASON, FakeEvent, main_module

HUMAN = "10001"
SELF_ID = "99999"


class _Response:
    def __init__(self, text: str) -> None:
        self.completion_text = text


class TrimRepeatsTest(unittest.TestCase):
    def test_a_sticky_place_drops_that_clause(self):
        recent = ["我在厨房呢", "厨房里还有点活儿"]
        new_text, dropped = reply_guard.trim_repeats(
            "我在厨房呢，刚把碗洗完。", recent
        )
        self.assertEqual(new_text, "刚把碗洗完。")
        self.assertEqual(dropped, ["我在厨房呢"])

    def test_a_place_mentioned_only_once_is_not_sticky(self):
        recent = ["我在厨房呢", "刚把碗洗完"]
        new_text, dropped = reply_guard.trim_repeats("我在厨房呢", recent)
        # 「我在厨房呢」与 recent 第一句完全相同 → 仍按小句复读删掉，但保留空后
        # 不能发空：整条都是复读时原样返回。
        self.assertEqual(new_text, "我在厨房呢")
        self.assertEqual(dropped, ["我在厨房呢"])

    def test_a_verbatim_clause_from_last_reply_is_dropped(self):
        recent = ["刚把碗洗完，手上还湿着。"]
        new_text, dropped = reply_guard.trim_repeats(
            "刚把碗洗完，你要不要也来喝口水？", recent
        )
        self.assertNotIn("刚把碗洗完", new_text)
        self.assertIn("你要不要也来喝口水", new_text)
        self.assertTrue(dropped)

    def test_unique_reply_is_left_alone(self):
        recent = ["我在厨房呢", "厨房里还有点活儿"]
        text = "窗外的雪下大了，我把炉子添了块柴。"
        new_text, dropped = reply_guard.trim_repeats(text, recent)
        self.assertEqual(new_text, text)
        self.assertEqual(dropped, [])

    def test_no_history_changes_nothing(self):
        text = "我在厨房呢，刚把碗洗完。"
        self.assertEqual(reply_guard.trim_repeats(text, []), (text, []))

    def test_never_returns_an_empty_reply(self):
        recent = ["我在厨房呢", "厨房里还有点活儿"]
        new_text, _ = reply_guard.trim_repeats("我在厨房呢。", recent)
        self.assertTrue(new_text.strip(), "宁可复读，也不许发空消息")

    def test_short_fillers_are_not_treated_as_repeats(self):
        recent = ["嗯", "好呀"]
        text = "嗯，好呀。"
        new_text, dropped = reply_guard.trim_repeats(text, recent)
        self.assertEqual(dropped, [])
        self.assertEqual(new_text, text)

    def test_a_repeated_nickname_is_kept(self):
        """连着叫对方的名字是称呼，不是卡带：不能把「Regnator」当复读删掉。"""
        recent = ["Regnator，我刚泡了壶热红茶才醒。", "Regnator，你先坐。"]
        text = "Regnator，我把炉子添了块柴。"
        new_text, dropped = reply_guard.trim_repeats(text, recent)
        self.assertIn("Regnator", new_text, "称呼不许删")
        self.assertEqual(dropped, [])
        self.assertEqual(new_text, text)

    def test_a_repeated_chinese_clause_is_still_dropped(self):
        """称呼不删，但同样的汉字小句照删——两道规则不互相干扰。"""
        recent = ["Regnator，我刚泡了壶热红茶才醒。"]
        text = "我刚泡了壶热红茶才醒，你回来啦？"
        new_text, dropped = reply_guard.trim_repeats(text, recent)
        self.assertNotIn("我刚泡了壶热红茶才醒", new_text)
        self.assertIn("你回来啦", new_text)
        self.assertTrue(dropped)


@unittest.skipIf(main_module is None, SKIP_REASON)
class GuardWiringTest(unittest.IsolatedAsyncioTestCase):
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

    async def test_the_sent_text_loses_the_repeated_place(self):
        star = await self._star()
        core = star._core(FakeEvent("/你的状态"))
        core.mood.note_spoke(HUMAN, "我在厨房呢")
        core.mood.note_spoke(HUMAN, "厨房里还有点活儿")
        response = _Response("我在厨房呢，刚把碗洗完。")
        await star.note_her_reply(FakeEvent("在吗", sender=HUMAN), response)
        self.assertNotIn("厨房", response.completion_text)
        self.assertIn("刚把碗洗完", response.completion_text)

    async def test_a_unique_reply_is_untouched(self):
        star = await self._star()
        core = star._core(FakeEvent("/你的状态"))
        core.mood.note_spoke(HUMAN, "我在厨房呢")
        core.mood.note_spoke(HUMAN, "厨房里还有点活儿")
        response = _Response("窗外的雪下大了，我把炉子添了块柴。")
        await star.note_her_reply(FakeEvent("在吗", sender=HUMAN), response)
        self.assertEqual(response.completion_text, "窗外的雪下大了，我把炉子添了块柴。")


class RefusalTest(unittest.TestCase):
    """v3.1.36：模型拒绝/整句英文不是她的话。"""

    def test_a_provider_refusal_is_recognised(self):
        text = (
            "Your request involves sexually suggestive roleplay within a "
            "sexualized context. I am refusing to continue."
        )
        self.assertTrue(reply_guard.looks_like_refusal(text))

    def test_a_whole_english_sentence_is_not_a_refusal(self):
        # v3.1.42：整句英文不再一律当拒绝——那会把英文 / 非中文人设永久静音。
        # 只有带拒绝语义特征的样板才拦（见 test_v3142_guard）。
        self.assertFalse(
            reply_guard.looks_like_refusal("The kettle is on the stove, come sit.")
        )

    def test_a_short_english_word_is_not(self):
        self.assertFalse(reply_guard.looks_like_refusal("Wind"))

    def test_a_chinese_reply_is_not_a_refusal(self):
        self.assertFalse(
            reply_guard.looks_like_refusal("窗外的雪下大了，我把炉子添了块柴。")
        )

    def test_empty_text_is_not_a_refusal(self):
        self.assertFalse(reply_guard.looks_like_refusal(""))


class RefusalWiringTest(unittest.IsolatedAsyncioTestCase):
    """线上实例：一条英文拒绝被当成她的话记进 `spoke_log` 还发了出去。"""

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

    async def test_a_refusal_is_neither_sent_nor_recorded(self):
        star = await self._star()
        core = star._core(FakeEvent("/你的状态"))
        response = _Response(
            "Your request involves sexually suggestive roleplay within a "
            "sexualized context. I am refusing to continue."
        )
        await star.note_her_reply(FakeEvent("在吗", sender=HUMAN), response)
        self.assertEqual(response.completion_text, "")
        self.assertEqual(core.mood.spoke(HUMAN), [])

    async def test_a_chinese_reply_is_still_sent(self):
        star = await self._star()
        core = star._core(FakeEvent("/你的状态"))
        response = _Response("窗外的雪下大了，我把炉子添了块柴。")
        await star.note_her_reply(FakeEvent("在吗", sender=HUMAN), response)
        self.assertEqual(response.completion_text, "窗外的雪下大了，我把炉子添了块柴。")
        self.assertTrue(core.mood.spoke(HUMAN))
