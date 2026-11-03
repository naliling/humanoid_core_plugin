"""v3.0.3（B3 / R1）：main.py 侧的三处「读路径与建档、出站判据」回归。

锁的是审计里 main.py 的三条：

* #99  群聊给陌生人无条件建档：`auto_nickname` 也是建档，必须走与注入侧同一道
       `mood.readable` 闸（群聊 `mood_enabled_in_group=false` 时不给陌生人留条目）。
* #100 `her_said_at` 被情绪开关连带挡掉：它是社交层「她多久前开过口」的唯一来源，
       必须在 `mood.readable` 之前记——mood 关掉时照样要记。
* #128 兄弟 bot 的回复不进她的账：入站有护栏、出站也要用同一道 `_is_sibling_peer`
       判据，否则跨 bot 互聊每句话都在改她的关系数值。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from .fakes import FakeContext, FakeProvider
from .test_main_integration import (
    GOOD_SCHEDULE,
    SKIP_REASON,
    FakeEvent,
    main_module,
)

SIBLING = "88888"   # 兄弟 bot：必须登记进 `_known_roles` 才被认出来
HUMAN = "10001"
SELF_ID = "99999"


class _Result:
    """够 `capture_reply_on_send` 用的最小结果对象（只认 LLM_RESULT）。"""

    def __init__(self, text: str, name: str = "LLM_RESULT") -> None:
        self.result_content_type = type("CT", (), {"name": name})()
        self._text = text

    def get_plain_text(self) -> str:
        return self._text


class _EventWithResult(FakeEvent):
    def __init__(self, *args, result=None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._result = result

    def get_result(self):
        return self._result


class _Response:
    def __init__(self, text: str) -> None:
        self.completion_text = text


@unittest.skipIf(main_module is None, SKIP_REASON)
class B3MainWiringTest(unittest.IsolatedAsyncioTestCase):
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

    def _user_bucket(self, star, user_id: str) -> dict:
        return (star._state_store.data.get("roles", {}).get(SELF_ID, {}) or {}).get(
            "users", {}
        ).get(user_id, {}) or {}

    # -------------------- #99 auto_nickname 建档闸 --------------------

    async def test_auto_nickname_does_not_build_a_group_stranger(self):
        """群聊 + 默认 `mood_enabled_in_group=false`：认定称呼也不许建档。"""
        star = await self._star()
        core = star._core(FakeEvent("/你的状态"))
        self.assertTrue(star._config.auto_nickname, "前置条件：认定称呼默认是开的")
        self.assertFalse(star._config.mood_enabled_in_group, "前置条件：群聊情绪默认关")
        await star.on_message(
            FakeEvent("大家好", sender="30003", private=False, sender_name="小明")
        )
        self.assertEqual(core.mood.nickname("30003"), "", "群聊陌生人不能被认定称呼")
        self.assertFalse(
            core.mood.has_profile("30003"), "群聊陌生人不能因为一条消息留下档案"
        )

    async def test_auto_nickname_still_works_in_private(self):
        """私聊不受群聊那道闸影响：不许把功能修没了。"""
        star = await self._star()
        core = star._core(FakeEvent("/你的状态"))
        await star.on_message(FakeEvent("在吗", sender="30004", sender_name="小红"))
        self.assertEqual(core.mood.nickname("30004"), "小红")

    # -------------------- #100 her_said_at 先于情绪闸 --------------------

    async def test_her_said_at_advances_even_when_mood_is_off(self):
        """mood 关掉时她说过话仍要留时间戳：社交层靠它判「她多久前开过口」。"""
        star = await self._star(mood_enabled=False)
        core = star._core(FakeEvent("/你的状态"))
        self.assertFalse(star._config.mood_enabled, "前置条件：情绪层是关的")
        await star.note_her_reply(
            FakeEvent("她说了句话", sender=HUMAN), _Response("我在呢")
        )
        self.assertGreater(
            float(self._user_bucket(star, HUMAN).get("her_said_at", 0.0) or 0.0),
            0.0,
            "情绪关掉也得记 her_said_at（审核 main.py 5）",
        )
        self.assertFalse(core.mood.has_profile(HUMAN), "情绪关着就不该建情绪档案")

    async def test_her_said_at_is_not_advanced_for_a_sibling_bot(self):
        """兄弟 bot 的回复不是「她对这个用户说的话」——不记账（#128）。"""
        star = await self._star()
        star._known_roles.add(SIBLING)
        await star.note_her_reply(
            FakeEvent("兄弟说话", sender=SIBLING), _Response("我是另一个 bot")
        )
        self.assertNotIn(
            "her_said_at",
            self._user_bucket(star, SIBLING),
            "兄弟 bot 的回复不该写 her_said_at",
        )

    async def test_send_hook_skips_a_sibling_bot(self):
        """发送前兜底用同一道判据：兄弟 bot 的 LLM 结果也不进她的账。"""
        star = await self._star()
        star._known_roles.add(SIBLING)
        event = _EventWithResult(
            "兄弟说话", sender=SIBLING, result=_Result("我是另一个 bot")
        )
        await star.capture_reply_on_send(event)
        self.assertNotIn(
            "her_said_at",
            self._user_bucket(star, SIBLING),
            "发送前兜底也要认兄弟 bot（审核 跨插件契约 12）",
        )

    async def test_send_hook_still_records_a_human_reply(self):
        """真人这一路不许被兄弟判据误伤。"""
        star = await self._star()
        event = _EventWithResult("在吗", sender=HUMAN, result=_Result("我在呢"))
        await star.capture_reply_on_send(event)
        self.assertGreater(
            float(self._user_bucket(star, HUMAN).get("her_said_at", 0.0) or 0.0), 0.0
        )
