"""v3.1.15 — 群聊情绪归属 / 会话分桶 / 结束语义 / 身体唤醒。

这一批改的不是阈值，是**归属**：一条群消息算不算她的情绪输入、攒的那批消息算进
哪个会话、她说过的话什么时候算结束。所以断言全落在「有没有建档、批次里是哪几条、
话头有没有被收掉」上，不落在数值大小上。

线上症状（2026-11-09 取证）：
* 群聊默认 `mood_enabled_in_group=False`，她在一个群待了几小时、被人聊了几百句，
  情绪层一条都没收到——回复里只剩「我好累」这类日程/身体读数在转。
* 待分析缓冲挂在 `record["llm_batch"]` 上，键里没有 umo，同一个人的私聊 + A 群 +
  B 群的话混成一批送给模型。
* 群消息每条 `soma.note_message()` 无条件 +1.5 唤醒，四角色同群时各自爆到 100。
* 用户说「已经解决了 / 不用继续 / 算了」，系统零识别，挂着的话头继续追问。
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from humanoid.config import HumanoidConfig
from humanoid.core_instance import HumanoidCoreInstance
from humanoid.persona import PersonaSource
from humanoid.services.mood import (
    LLM_BATCH_BY_SESSION_KEY,
    MESSAGES_SINCE_BY_SESSION_KEY,
    MoodService,
)
from humanoid.state import StateStore
from tests.fakes import FrozenClock, RecordingLogger, ScopeStore

FEMALE = "你是小满，24岁。她说话很短，喜欢安静。"


def _cfg(**overrides) -> HumanoidConfig:
    return HumanoidConfig.from_raw({"timezone_city": "北京", **overrides})


def _core(**extra) -> HumanoidCoreInstance:
    conf = _cfg(**extra)
    st = StateStore(Path(tempfile.mkdtemp()) / "s.json", 0.01)
    st.load("2026-09-26", 28)

    class M:
        async def resolve_selected_persona(self, **k):
            return ("p", {"name": "", "prompt": FEMALE, "begin_dialogs": []}, None, False)

        async def get_default_persona_v3(self):
            return {"name": "", "prompt": FEMALE, "begin_dialogs": []}

    class C:
        persona_manager = M()
        conversation_manager = None

        def get_config(self, *a, **k):
            return None

    c = C()
    src = PersonaSource(c, lambda: RecordingLogger())
    core = HumanoidCoreInstance(
        role_id="bot1", state_store=st, config_provider=lambda: conf,
        logger=RecordingLogger(), stop_event=asyncio.Event(),
        resolver=c, gateway=None, persona_source=src,
    )
    asyncio.run(src.persona("bot1"))
    core.clock = FrozenClock(datetime(2026, 9, 26, 23, 0))
    return core


def _profile(core, uid: str = "u1", **kw) -> dict:
    """给一个人建好完整档案（= 老熟人）。"""
    v = {
        "affection": 92.0, "base_affection": 46.0, "libido": 47.0,
        "base_libido": 34.0, "aggression": 20.0, "base_aggression": 10.0,
        "turn_count": 60, "last_interaction": 0.0, "mood_tag": "", "last_decay": 0.0,
    }
    v.update(kw)
    core._scope.user_state(uid)["mood"] = v
    return v


class GroupMoodTracks(unittest.TestCase):
    """群里谁算「她在乎的人」。

    原来群聊是**一刀切关闭**：整个群都不进情绪层，所以群里被骂、被撒娇、被
    无视，她的情绪一点都不动。改成「只跟已经有档案的人」，既不让陌生人一句
    「你好」就建档（线上 4125 用户里 41 个有档案，就是这个比例），又让常聊的人
    在群里也真的影响她。
    """

    def test_stranger_in_group_is_not_tracked(self):
        core = _core()
        self.assertFalse(core._mood_tracks("u9", True))

    def test_stranger_in_group_does_not_get_a_profile(self):
        core = _core()
        core.on_message("u9", "你好呀", is_group=True, umo="bot1:GroupMessage:1")
        self.assertFalse(core.mood.has_profile("u9"), "陌生人一句群消息不该建档")

    def test_known_profile_in_group_is_tracked(self):
        core = _core()
        _profile(core)
        self.assertTrue(core._mood_tracks("u1", True))

    def test_private_is_always_tracked(self):
        core = _core()
        self.assertTrue(core._mood_tracks("u9", False), "私聊不设这道门")

    def test_group_switch_still_wins(self):
        """显式打开「群聊也分析」时，陌生人也进——配置优先于默认策略。"""
        core = _core(mood_enabled_in_group=True)
        self.assertTrue(core._mood_tracks("u9", True))

    def test_master_switch_off_tracks_nothing(self):
        core = _core(mood_enabled=False)
        _profile(core)
        self.assertFalse(core._mood_tracks("u1", False))
        self.assertFalse(core._mood_tracks("u1", True))

    def test_dispatch_only_spawns_for_tracked_users(self):
        """`_dispatch_async` 是情绪层的唯一入口，闸必须在这里生效。"""
        core = _core()
        spawned: list[str] = []

        def _capture(coro, name):
            spawned.append(name)
            coro.close()

        core._spawn_background = _capture
        core._dispatch_async("u9", "陌生人在群里说话", is_group=True, umo="bot1:GroupMessage:1")
        self.assertEqual(spawned, [], "陌生人：不该起分析任务")
        _profile(core)
        core._dispatch_async("u1", "熟人 在群里说话", is_group=True, umo="bot1:GroupMessage:1")
        self.assertEqual(spawned, ["mood-update"], "熟人：该起分析任务")


class SessionBuckets(unittest.TestCase):
    """攒的那批消息按会话分开。

    原来缓冲键是 `record["llm_batch"]`，一个人的私聊、A 群、B 群全混一批。线上
    表现是她对着私聊对象说「你在群里跟别人聊得挺欢」——那批里混着群消息。
    """

    def build(self, **overrides):
        conf = _cfg(**overrides)
        store = ScopeStore(Path(tempfile.mkdtemp()) / "state.json")
        store.load("2026-09-26", conf.cycle_length)
        service = MoodService(
            store.scope, lambda: conf, None,
            time_source=lambda: 1_000_000.0, logger=RecordingLogger(),
        )
        service.profile("u1")
        return service, store

    def test_batches_are_split_by_session(self):
        service, _ = self.build()
        service._append_llm_batch("u1", "私聊里的一句", umo="bot1:FriendMessage:1")
        service._append_llm_batch("u1", "群 A 里的一句", umo="bot1:GroupMessage:2")
        self.assertEqual(
            service._peek_llm_batch("u1", "群 A 里的一句", umo="bot1:GroupMessage:2"),
            ["群 A 里的一句"],
        )
        self.assertEqual(
            service._peek_llm_batch("u1", "私聊里的一句", umo="bot1:FriendMessage:1"),
            ["私聊里的一句"],
        )

    def test_legacy_path_keeps_the_old_field(self):
        """`umo` 为空（老调用方 / 老测试）时行为必须和改动前完全一致。"""
        service, store = self.build()
        service._append_llm_batch("u1", "没有会话信息的一句")
        record = store.scope.user_state("u1")["mood"]
        self.assertIn("没有会话信息的一句", record["llm_batch"])
        self.assertNotIn(LLM_BATCH_BY_SESSION_KEY, record)

    def test_counters_are_per_session(self):
        service, store = self.build(mood_use_llm_for_delta=False)

        async def _run():
            await service.update_from_message_async("u1", "A 群 1", umo="sA", is_group=True)
            await service.update_from_message_async("u1", "A 群 2", umo="sA", is_group=True)
            await service.update_from_message_async("u1", "私聊 1", umo="sP", is_group=False)

        asyncio.run(_run())
        counters = store.scope.user_state("u1")["mood"][MESSAGES_SINCE_BY_SESSION_KEY]
        self.assertEqual(counters["sA"], 2, "群 A 攒到 2 条")
        self.assertEqual(counters["sP"], 1, "私聊只攒到 1 条，不该被群里的条数带着走")

    def test_group_interval_is_wider_than_private(self):
        """群消息占入站九成以上，共用私聊间隔会让分析调用翻十倍。"""
        service, _ = self.build(mood_use_llm_for_delta=False, mood_llm_interval_messages=4)

        async def _run():
            for i in range(4):
                await service.update_from_message_async(
                    "u1", f"群里第 {i} 句", umo="sG", is_group=True
                )

        asyncio.run(_run())
        record = service._scope.user_state("u1")["mood"]
        self.assertEqual(
            record[MESSAGES_SINCE_BY_SESSION_KEY]["sG"], 4,
            "群聊阈值是 20 条，攒到 4 条还不该触发",
        )


class EndIntent(unittest.TestCase):
    """TA 说「这事到此为止」→ 收掉话头、不再回灌。

    原来「已经解决 / 我自己来 / 不用继续 / 别问了」在代码里**零命中**（v3.1.14
    取证：`humanoid/` 下没有一处匹配），她说过的话只能靠「对方下一条消息一到」
    才关掉——用户说「不用了」，在她那边等于没说。
    """

    def build(self):
        conf = _cfg()
        store = ScopeStore(Path(tempfile.mkdtemp()) / "state.json")
        store.load("2026-09-26", conf.cycle_length)
        service = MoodService(
            store.scope, lambda: conf, None,
            time_source=lambda: 1_000_000.0, logger=RecordingLogger(),
        )
        return service, store

    def test_ordinary_chat_is_not_an_end_signal(self):
        service, _ = self.build()
        self.assertFalse(service.note_end_intent("u1", "今天好累啊"))
        self.assertEqual(service.end_intent_at("u1"), 0.0)

    def test_end_signal_closes_every_pending_spoke(self):
        service, store = self.build()
        service.note_spoke("u1", "她说过的一句话", now=999_000.0)
        service.note_spoke("u1", "她又说了一句", now=999_500.0)
        self.assertTrue(service.note_end_intent("u1", "这事已经解决了", now=1_000_000.0))
        rows = store.scope.user_state("u1")["spoke_log"]
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertFalse(row["pending"], "全部 pending 都要收掉，不只是最后一条")
            self.assertFalse(row["landed"], "「她说了但没落地」这个事实要保留")
        self.assertEqual(service.end_intent_at("u1"), 1_000_000.0)

    def test_open_items_before_the_cutoff_are_not_reinjected(self):
        core = _core()
        stamp = 1_000_000.0
        core.life_items = lambda: [
            {"uid": "u1", "at": stamp - 3600, "text": "和TA约好周末一起去看海"},
            {"uid": "u1", "at": stamp + 10, "text": "TA说下周要出差"},
        ]
        before = "\n".join(core.prompt_builder._open_lines("u1"))
        self.assertIn("一起去看海", before, "先确认这条本来会注入")
        core.mood.note_end_intent("u1", "这事已经解决了", now=stamp)
        after = "\n".join(core.prompt_builder._open_lines("u1"))
        self.assertNotIn("一起去看海", after, "说结束之前的未完事不该再回灌")
        self.assertIn("下周要出差", after, "说结束之后新提的事照常注入")


class BodyArousal(unittest.TestCase):
    """群消息的唤醒增益。

    线上 `soma.note_message()` 对每条消息无条件 +1.5，而群消息占入站九成以上，
    四个角色又同在一个群——各自身上的 arousal 全被群消息顶到 100，与「她此刻
    到底被谁打动了」无关。
    """

    def test_group_message_raises_arousal_less_than_private(self):
        core = _core()
        core.soma._set("arousal", 50.0)
        core.soma.note_message(is_group=True)
        self.assertAlmostEqual(core.soma.data["arousal"], 50.4, places=2)

        core.soma._set("arousal", 50.0)
        core.soma.note_message()
        self.assertAlmostEqual(core.soma.data["arousal"], 51.5, places=2)

    def test_arousal_is_capped(self):
        core = _core()
        core.soma._set("arousal", 99.9)
        core.soma.note_message(is_group=True)
        self.assertLessEqual(core.soma.data["arousal"], 100.0)


class ConfigSurface(unittest.TestCase):
    def test_group_interval_default_is_wider(self):
        conf = _cfg()
        self.assertEqual(conf.mood_llm_interval_messages, 4)
        self.assertEqual(conf.mood_group_llm_interval_messages, 20)

    def test_schema_exposes_the_group_interval(self):
        raw = json.loads(
            (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text("utf-8")
        )
        field = raw.get("mood_group_llm_interval_messages")
        self.assertIsNotNone(field, "面板里要能调群聊间隔")
        self.assertEqual(field.get("default"), 20)
        self.assertEqual(field.get("type"), "int")


if __name__ == "__main__":
    unittest.main()
