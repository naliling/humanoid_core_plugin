"""v3.0.6 黄金注入快照：把 B3–B5 修出来的「注入正文长什么样」钉死。

R7 注入取舍层（humanoid/injection.py）现在是唯一出口，prompt_builder._finish
与 main._state_note 都走它。但「取舍正确」不等于「正文正确」：文本漂移不会让任何
单元测试变红，只会让线上的人设慢慢走样（docs/REGRESSIONS.md 里「同一个毛病换个地方
回来」大多是从这里开始的）。

所以这里存的是**逐字正文**。它故意不灵活：

    GOLDEN 里的每一行都是线上真实注入。改这里必须同时说明为什么注入文本该变
    ——如果只是「实现变干净了」，那注入文本就不该变；文本变了就是行为变了。

全部场景用 FrozenClock + 固定配置 + 固定档案，禁止真实时间与随机数。
"""

from __future__ import annotations

import asyncio
import re
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from humanoid import ccb, injection
from humanoid.config import HumanoidConfig
from humanoid.core_instance import HumanoidCoreInstance
from humanoid.state import StateStore

from .fakes import FakeContext, FrozenClock, RecordingLogger
from .test_main_integration import main_module

TZ = ZoneInfo("Asia/Shanghai")
DAY = datetime(2026, 9, 26, 13, 3, tzinfo=TZ)
NIGHT = datetime(2026, 9, 26, 23, 30, tzinfo=TZ)
UID = "42"
SAID = "在吗"

#: 黄金快照。改这里必须同时说明为什么注入文本该变（见模块 docstring）。
#:
#: v13 起注入正文是第一人称：原来写「她今天有点累」，模型读到的是「有个叫她的实体
#: 状态如何」，只能当旁白转述，思考里会写「我在扮演她」。改成「我」之后，人称映射
#: 这一步整个消失。所以抬头、正文、框架句一律用「我」。
GOLDEN = {
    # 默认配置、白天醒着、普通私聊：没有日程事件、没有情绪波动。
    "s1_default": (
        "〔我现在的身体 v13〕\n"
        "私聊，只有我和TA。我这几天人挺有精神，皮肤也顺眼。我对TA还不算熟，你们刚认识不久。我内心：有点想说话。前面聊的还能接上，我这会儿还挺专注，我现在很闲。"
    ),
    # 夜里 23:30 睡着（show_sleep_window 开）：只留处境与关系，互动前置的倾向整块不发。
    "s2_asleep": (
        "〔我现在的身体 v13〕\n"
        "私聊，只有我和TA。我对TA还不算熟，你们刚认识不久。我这会儿正睡着。我这几天人挺有精神，皮肤也顺眼。"
    ),
    # 同一场景但 show_sleep_window 关：正文里一个「睡」字都不该有。
    "s3_sleep_off": (
        "〔我现在的身体 v13〕\n"
        "私聊，只有我和TA。我对TA还不算熟，你们刚认识不久。我这几天人挺有精神，皮肤也顺眼。"
    ),
    # 刚睡醒、昨晚只睡了 4 小时：这件事只说一遍。
    "s4_short_sleep": (
        "〔我现在的身体 v13〕\n"
        "私聊，只有我和TA。我这几天人挺有精神，皮肤也顺眼。今天只睡了4小时。我对TA还不算熟，你们刚认识不久。我内心：有点想说话。前面聊的还能接上，我这会儿还挺专注，我现在很闲。"
    ),
    # 情绪冷静期：只出现一次，且不出现「为你刚才说的话的事」这种二次拼装。
    "s5_cooling": (
        "〔我现在的身体 v13〕\n"
        "私聊，只有我和TA。我这几天人挺有精神，皮肤也顺眼。我对TA还不算熟，你们刚认识不久。我内心：还在气头上（你刚才说的话），这会儿不想好好说话，过一会儿再讲。前面聊的还能接上，我这会儿还挺专注，我现在很闲。"
    ),
    # 群聊：另一个 user_id 的未完事项不许出现，自己的那条要在。
    "s7_group": (
        "〔我现在的身体 v13〕\n"
        "群聊，这个会话里还有别人。我这几天人挺有精神，皮肤也顺眼。前面聊的还能接上。我手上还有件事没完：我自己的事（刚刚）。"
    ),
    # 三档之一：最少。
    "s8_mood_only": (
        "〔我现在的身体 v13〕\n"
        "私聊，只有我和TA。我对TA还不算熟，你们刚认识不久，前面聊的还能接上，我这会儿还挺专注，我现在很闲。我内心：有点想说话。"
    ),
    # 三档之一：默认。
    "s8_medium": (
        "〔我现在的身体 v13〕\n"
        "私聊，只有我和TA。我这几天人挺有精神，皮肤也顺眼。我对TA还不算熟，你们刚认识不久。我内心：有点想说话。前面聊的还能接上，我这会儿还挺专注，我现在很闲。"
    ),
    # 三档之一：最多。
    "s8_full": (
        "〔我现在的身体 v13〕\n"
        "私聊，只有我和TA。我这几天人挺有精神，皮肤也顺眼。我对TA还不算熟，你们刚认识不久。我内心：有点想说话。前面聊的还能接上，我这会儿还挺专注，我现在很闲。"
    ),
    # 日程事件在身：事实块 + 【我现在的情况】合起来，事件短语只出现一次。
    "s6_schedule": (
        "〔我现在的身体 v13〕\n"
        "私聊，只有我和TA。我这几天人挺有精神，皮肤也顺眼。我对TA还不算熟，你们刚认识不久。我内心：有点想说话。前面聊的还能接上，我这会儿还挺专注，我现在很闲。\n"
        "\n"
        "【我现在的情况】\n"
        "我和TA还在互相认识，没到能说私事的地步。我这会儿整理个案笔记（书桌前）。今天是2026-09-26，现在是13:03:00；今天周六；入秋了；你在北京。"
    ),
}


def _pin_cycle(core: HumanoidCoreInstance, moment: datetime = DAY) -> None:
    """把生理周期日钉在快照录制时那一天。

    core 在构造时已经用**真实 Clock** 的今天播种了 current_cycle_day
    （core_instance.py:225-230 → state.seed_cycle_day），而这个测试只在构造之后才把
    时钟换成 FrozenClock，于是快照会随「跑测试那天」漂：2026-11-04 播到第 21 天
    （phase 6，注入里是「人有点沉，总想吃东西」），而快照录的是 phase 2（周期第
    9-11 天，「人挺有精神，皮肤也顺眼」）。固定成第 10 天，并把 last_cycle_update
    对齐到快照那天，让 energy.advance_cycle 不再推进。
    """
    core._scope.update_self(
        current_cycle_day=10,
        last_cycle_update=moment.strftime("%Y-%m-%d"),
    )


def _core(moment: datetime = DAY, **conf) -> HumanoidCoreInstance:
    """一个固定时钟、固定配置、空档案的 core（照 tests/test_v304_b4_prompt_time.py）。"""
    store = StateStore(Path(tempfile.mkdtemp()) / "state.json", lambda: 0.01)
    store.load(moment.strftime("%Y-%m-%d"), 28)
    config = HumanoidConfig.from_raw({"timezone_city": "北京", **conf})
    core = HumanoidCoreInstance(
        role_id="bot1",
        state_store=store,
        config_provider=lambda: config,
        logger=RecordingLogger(),
        stop_event=asyncio.Event(),
        resolver=FakeContext(),
        gateway=None,
    )
    clock = FrozenClock(moment)
    core.clock = clock
    # 服务各自缓存了构造时的时钟；不换掉的话时间会走真实墙钟。
    for name in ("schedule", "soma", "energy", "social", "process", "mood"):
        svc = getattr(core, name, None)
        if svc is not None and hasattr(svc, "_clock"):
            svc._clock = clock
    _pin_cycle(core, moment)
    return core


def _body(**conf) -> str:
    return _core(**conf).build_injection(UID, text=SAID)


def _asleep_body(**conf) -> str:
    core = _core(NIGHT, **conf)
    core.soma.data["asleep_since"] = core.soma.now - 3600.0
    return core.build_injection(UID, text=SAID)


def _short_sleep_body() -> str:
    core = _core()
    core.soma.data["wake_at"] = core.soma.now - 5 * 3600.0
    core.soma.data["last_wake_at"] = core.soma.now - 5 * 3600.0
    core.soma.data["last_sleep_hours"] = 4.0
    return core.build_injection(UID, text=SAID)


def _cooling_body() -> str:
    core = _core()
    core._scope.update_user(
        UID,
        mood={
            "affection": 35.0,
            "base_affection": 35.0,
            "cool_until": core.now_epoch() + 600.0,
            "cool_reason": "你刚才说的话",
        },
    )
    return core.build_injection(UID, text=SAID)


def _schedule_bodies() -> tuple:
    core = _core()
    core._scope.update_self(
        today_date="2026-09-26",
        schedule_source="llm",
        daily_schedule=[
            {"start": "12:30", "end": "14:00", "event": "整理个案笔记",
             "location": "书桌前", "emotion": "专注", "energy_rate": 0.0}
        ],
    )
    injection_body = core.build_injection(UID, text=SAID)
    note = main_module._state_note(core, UID, said=SAID)
    return injection_body, note


def _group_body() -> str:
    core = _core()
    core._scope.update_self(
        life_log=[
            {"text": "她自己的事", "uid": UID, "at": core.now_epoch(), "until": 0.0},
            {"text": "别人的私事", "uid": "99", "at": core.now_epoch(), "until": 0.0},
        ]
    )
    return core.build_injection(UID, text=SAID, is_group=True)


def _tier_body(mode: str) -> str:
    return _body(inject_activity_context=mode)


_EMOTION_SENTENCE = re.compile(r"我内心：[^。]*。")


def _without_emotion(body: str) -> str:
    """去掉「她内心：…。」那一句。

    同一状态连续渲染两次，正文**只该少这一句**：情绪句按「同一句 + 10 分钟」逐字
    去重（EMOTION_REPEAT_SECONDS，线上实测同一句被照抄 8 轮），这是刻意的设计——
    同一件事说一遍是状态，说到第八遍就是台词，模型会照着念。所以「不幂等」不是
    bug，但「除了这一句之外还变了」是 bug：那说明还有别的状态在两次渲染之间漂移。
    """
    return _EMOTION_SENTENCE.sub("", body)


def _injection_bodies() -> dict:
    """所有注入正文（不含【此刻的她】那段）。结构不变量逐条扫这一份。"""
    return {
        "s1": _body(),
        "s2": _asleep_body(),
        "s3": _asleep_body(show_sleep_window=False),
        "s4": _short_sleep_body(),
        "s5": _cooling_body(),
        "s6": _schedule_bodies()[0],
        "s7": _group_body(),
        "s8_mood_only": _tier_body("mood_only"),
        "s8_medium": _tier_body("medium"),
        "s8_full": _tier_body("full"),
    }


class GoldenSnapshotTest(unittest.TestCase):
    """逐字快照：每一档、每一个场景都存正文原文。"""

    def test_1_default_day_private(self):
        self.assertEqual(_body(), GOLDEN["s1_default"])

    def test_2_asleep_at_night(self):
        self.assertEqual(_asleep_body(), GOLDEN["s2_asleep"])

    def test_3_sleep_window_off_has_no_sleep_word(self):
        body = _asleep_body(show_sleep_window=False)
        self.assertEqual(body, GOLDEN["s3_sleep_off"])
        self.assertNotIn("睡", body, "关掉睡眠窗口后，任何一条路径都不许再提睡觉")

    def test_4_short_sleep_is_said_once(self):
        body = _short_sleep_body()
        self.assertEqual(body, GOLDEN["s4_short_sleep"])
        self.assertEqual(body.count("只睡了"), 1, "睡眠时长这件事被说了不止一遍")
        self.assertNotIn("刚睡醒", body, "同一轮里「只睡了 N 小时」与「刚睡醒」不该并存")

    def test_5_cooling_is_said_once(self):
        body = _cooling_body()
        self.assertEqual(body, GOLDEN["s5_cooling"])
        self.assertEqual(body.count("气头上"), 1, "冷静期从两条通道各说了一遍")
        self.assertNotIn("说的话的事", body, "reason 被二次拼装成「…的事」")

    def test_6_schedule_event_is_said_once(self):
        injection_body, note = _schedule_bodies()
        self.assertEqual(injection_body + note, GOLDEN["s6_schedule"])
        self.assertEqual((injection_body + note).count("整理个案笔记"), 1)
        self.assertNotIn("整理个案笔记", injection_body,
                         "日程是背景，走【此刻的她】；混进事实块会被当成要回应的内容")

    def test_7_group_hides_other_users_open_item(self):
        body = _group_body()
        self.assertEqual(body, GOLDEN["s7_group"])
        self.assertIn("我自己的事", body)
        self.assertNotIn("别人的私事", body, "群聊里把别人的私事递给了当前说话人")

    def test_8_tiers_stay_within_the_red_line(self):
        for mode, key in (("mood_only", "s8_mood_only"),
                          ("medium", "s8_medium"),
                          ("full", "s8_full")):
            body = _tier_body(mode)
            self.assertEqual(body, GOLDEN[key])
            cap = injection.INJECT_MAX_CHARS[mode]
            self.assertLessEqual(len(body), cap,
                                 "%s 档正文 %d 字，超过红线 %d" % (mode, len(body), cap))


class InjectionInvariantsTest(unittest.TestCase):
    """快照之外的结构不变量：不依赖具体措辞，只钉「什么不该出现」。"""

    #: 体检单读数：45/100、45/50 这类「数字斜杠数字」。
    READING = re.compile(r"\d+\s*/\s*\d+")

    def test_no_checkup_readings(self):
        for key, body in _injection_bodies().items():
            self.assertIsNone(self.READING.search(body), "%s 里出现了体检单读数：%s" % (key, body))

    def test_no_centimetres(self):
        for key, body in _injection_bodies().items():
            self.assertNotIn("cm", body, "%s 里出现了原始单位" % key)

    def test_no_ccb_phase_names(self):
        for key, body in _injection_bodies().items():
            for phase in ccb.PHASE_NAMES[1:]:
                self.assertNotIn(phase, body, "%s 里出现了阶段名「%s」" % (key, phase))

    def test_no_literal_sleep_filter_leftover(self):
        for key, body in _injection_bodies().items():
            self.assertNotIn("在睡", body, "%s 里残留了按字面判断睡眠的写法" % key)

    def test_punctuation_is_clean(self):
        for key, body in _injection_bodies().items():
            for bad in ("，，", "。。", "，。", "。，", "；"):
                self.assertNotIn(bad, body, "%s 里出现了不该有的标点「%s」" % (key, bad))
            self.assertTrue(body.endswith("。"), "%s 没有以「。」收尾：%r" % (key, body))


class GoldenIsStableTest(unittest.TestCase):
    """同一输入渲染两次必须一样；换档位不该留下残留。"""

    def test_same_state_two_cores_render_identically(self):
        self.assertEqual(_body(), _body(), "同样的固定状态，两次渲染出了不同正文")

    def test_same_instance_second_build_differs_only_by_the_emotion_sentence(self):
        """第二次只少「她内心」那句，别的逐字相同（去重是有意的，漂移不是）。"""
        core = _core()
        first = core.build_injection(UID, text=SAID)
        second = core.build_injection(UID, text=SAID)
        self.assertIn("我内心：", first, "第一次该带一句内心话")
        self.assertNotIn("我内心：", second, "同一句 10 分钟内不该再说一遍")
        self.assertEqual(_without_emotion(second), _without_emotion(first))

    def test_tier_switch_does_not_leak_tier_content(self):
        holder = {}
        store = StateStore(Path(tempfile.mkdtemp()) / "state.json", lambda: 0.01)
        store.load(DAY.strftime("%Y-%m-%d"), 28)
        holder["cfg"] = HumanoidConfig.from_raw({"timezone_city": "北京"})
        core = HumanoidCoreInstance(
            role_id="bot1",
            state_store=store,
            config_provider=lambda: holder["cfg"],
            logger=RecordingLogger(),
            stop_event=asyncio.Event(),
            resolver=FakeContext(),
            gateway=None,
        )
        clock = FrozenClock(DAY)
        core.clock = clock
        for name in ("schedule", "soma", "energy", "social", "process", "mood"):
            svc = getattr(core, name, None)
            if svc is not None and hasattr(svc, "_clock"):
                svc._clock = clock
        _pin_cycle(core, DAY)
        medium_first = core.build_injection(UID, text=SAID)
        holder["cfg"] = HumanoidConfig.from_raw(
            {"timezone_city": "北京", "inject_activity_context": "full"}
        )
        full = core.build_injection(UID, text=SAID)
        holder["cfg"] = HumanoidConfig.from_raw({"timezone_city": "北京"})
        medium_again = core.build_injection(UID, text=SAID)
        self.assertEqual(medium_first, GOLDEN["s8_medium"])
        # 情绪句按「同一句 + 10 分钟」逐字去重（EMOTION_REPEAT_SECONDS）：紧接着
        # 渲染第二次时它本来就会少这一句，所以比的是「除情绪句外」。
        self.assertEqual(
            _without_emotion(full), _without_emotion(GOLDEN["s8_full"]), "full 档正文变了"
        )
        # P2：周期句改由 body_sense 统一翻成感觉，full 档不再单出运营口径那句
        # （「激素在往上爬」「内膜最厚」）。这份夹具没有情绪档案，所以 full 与
        # medium 只差在「档位能多装」，正文相同——这不是漏档，是没东西可多。
        self.assertNotIn("激素", full, "运营口径的周期描述不该再进注入")
        self.assertNotIn("激素", medium_again, "full 档的内容漏进了 medium")
        # 第二次 medium 唯一少掉的是「她内心」那句（_emotion_repeated 的设计）。
        self.assertEqual(_without_emotion(medium_again), _without_emotion(medium_first))

    def test_tier_switch_medium_full_medium_leaves_no_residue(self):
        """换档不留下残留：回到 medium 时，除情绪句去重外与第一次逐字相同。"""
        core = _core()
        first = core.build_injection(UID, text=SAID)
        core.config_provider = lambda: HumanoidConfig.from_raw(
            {"timezone_city": "北京", "inject_activity_context": "full"}
        )
        core.build_injection(UID, text=SAID)
        core.config_provider = lambda: HumanoidConfig.from_raw({"timezone_city": "北京"})
        again = core.build_injection(UID, text=SAID)
        self.assertEqual(_without_emotion(again), _without_emotion(first))
        self.assertNotIn("激素", again, "full 档的内容漏回了 medium")


class AsleepConsistencyTest(unittest.TestCase):
    """睡着了就是睡着了：正文不该同时说她正专注于这场对话。"""

    def test_asleep_body_does_not_say_she_is_engaged(self):
        """睡着的人不会「还挺专注」「想说话」——两条互斥的事实不许同段出现。

        v3.0.6 之前：睡眠在 soma._sleep_state_lines 里算，兴趣在 interest、
        情绪在 emotion 两条线上各自算，彼此不知道对方，于是同一段正文既写
        「她这会儿正睡着」又写「她这会儿还挺专注」「她内心：有点想说话」。
        模型只能二选一，实测它选了后者，照着「想说话」的口气回话。
        现在睡着时倾向整块不注入、主动性这一维不进候选池（prompt_builder
        _append_sleep_essentials 与 emotion.candidate_with_kind 的 asleep 参数）。
        """
        body = _asleep_body()
        self.assertIn("我这会儿正睡着", body)
        bad = [word for word in ("还挺专注", "想说话", "很闲") if word in body]
        self.assertEqual(bad, [], "睡着时不该出现「%s」：%r" % ("、".join(bad), body))


class RepeatedBuildTest(unittest.TestCase):
    """连续两轮注入：稳定的状态不该被当成重复台词丢掉。"""

    def test_cooling_state_survives_the_second_message(self):
        core = _core()
        core._scope.update_user(
            UID,
            mood={
                "affection": 35.0,
                "base_affection": 35.0,
                "cool_until": core.now_epoch() + 15 * 60.0,
                "cool_reason": "你刚才说的话",
            },
        )
        first = core.build_injection(UID, text=SAID)
        second = core.build_injection(UID, text=SAID)
        self.assertIn("气头上", first, "第一条消息里就该有冷静期")
        # 冷静期是会过期的**状态**（cool_until），不是重复台词：原来它也走逐字去重，
        # 于是 cool 的 15 分钟里只有头 10 分钟能看见，最后 5 分钟注入一个字都不提
        # 她还在生气（实测 t0 有、t+2 无、t+9 无、t+11 有、t+13 无）。
        # 现在 candidate_with_kind 给它单独的 kind "cooling"，不参与去重。
        self.assertIn("气头上", second)


if __name__ == "__main__":
    unittest.main()
