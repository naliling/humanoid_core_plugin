"""v2.22.0：注入层不再把同一件事说三遍，也不再说自相矛盾的话。

这里盯的全是**读起来**的毛病，不是某个函数的返回值：同一维度铺着两三个来源
（精力有四处、社交意愿有三处、关系有三处）、情绪门槛正好压在初始值上导致
「惦记/拧巴」变成常驻背景、事件句挂满一整天。这类问题写坏了不会抛异常，
只让她说的话越来越像预制菜——所以只能靠断言把住。
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from humanoid.config import HumanoidConfig
from humanoid.core_instance import HumanoidCoreInstance
from humanoid.prompt_builder import INJECT_MAX_CHARS, _short_weather
from humanoid.services.schedule import _event_phrase, _with_at
from humanoid.services.weather import parse_payload
from humanoid.state import StateStore

from .fakes import FakeContext, FrozenClock, RecordingLogger

TZ = ZoneInfo("Asia/Shanghai")
MOMENT = datetime(2026, 9, 26, 13, 3, tzinfo=TZ)
TODAY = MOMENT.strftime("%Y-%m-%d")

# 心气这一维的全部措辞：同一时刻只该出现其中一种。
HEART_WORDS = (
    "拧巴", "念头抣着", "又气又想靠近",
    "压着火", "一股气没处发",
    "惦记TA", "心思飘过去",
)


def cfg(**overrides) -> HumanoidConfig:
    return HumanoidConfig.from_raw({"timezone_city": "北京", **overrides})


class InjectionLayerTest(unittest.TestCase):
    def core(self, mode: str = "medium", **conf) -> HumanoidCoreInstance:
        store = StateStore(Path(tempfile.mkdtemp()) / "state.json", lambda: 0.01)
        store.load(TODAY, 28)
        config = cfg(inject_activity_context=mode, **conf)
        core = HumanoidCoreInstance(
            role_id="bot1",
            state_store=store,
            config_provider=lambda: config,
            logger=RecordingLogger(),
            stop_event=asyncio.Event(),
            resolver=FakeContext(),
            gateway=None,
        )
        core.clock = FrozenClock(MOMENT)
        return core

    def add_mood_log(self, core, user_id, event: str, minutes_ago: int) -> None:
        at = MOMENT - timedelta(minutes=minutes_ago)
        core.scope.user_state(user_id)["mood_logs"] = [{
            "time": at.strftime("%Y-%m-%d %H:%M:%S"),
            "event": event,
            "affection": 46.0,
            "libido": 34.0,
            "aggression": 45.0,
        }]

    # ------------------------------------------------------------------
    # 同一维度只留一个来源
    # ------------------------------------------------------------------

    def test_angry_and_devoted_do_not_both_get_said(self):
        """三轴全满时不能又「热烈」又「一股气没处发」——那是两句互相打架的话。"""
        core = self.core()
        core.mood.profile("42").update({"affection": 100.0, "libido": 50.0, "aggression": 50.0})
        text = core.build_injection("42", is_group=False)
        self.assertLessEqual(
            text.count("她对TA"), 1, f"关系位置说了不止一句：\n{text}"
        )
        hearts = [w for w in HEART_WORDS if w in text]
        self.assertLessEqual(
            len(hearts), 1, f"心气同一时刻出现了好几种：{hearts}\n{text}"
        )

    def test_default_mood_is_not_read_as_tense(self):
        """默认初始值（亲近欲 34、攻击性 28）不该被判成惦记/拧巴/矛盾。

        旧门槛是 30 和 32，正压在初始值上：于是「有点惦记TA」「她有点拧巴」「矛盾」
        几乎每条消息都在，而它们说的其实是常态。
        """
        core = self.core()
        record = core.mood.profile("42")
        self.assertEqual(record["libido"], 34.0)
        self.assertEqual(record["aggression"], 28.0)
        text = core.build_injection("42", is_group=False)
        for word in HEART_WORDS + ("矛盾",):
            self.assertNotIn(word, text, f"常态被说成了异常心气「{word}」：\n{text}")

    def test_energy_is_described_once(self):
        """精力充沛/人挺精神/精神饱满说的是同一件事，只能有一个。"""
        core = self.core()
        core.soma.data.update({"arousal": 90.0})
        core.scope.update_self(energy=95.0)
        text = core.build_injection("42", is_group=False)
        spoken = [w for w in ("精力充沛", "人挺精神的", "精神饱满", "状态挺好", "身上有劲") if w in text]
        self.assertLessEqual(len(spoken), 1, f"精力说了不止一遍：{spoken}\n{text}")

    def test_cycle_description_does_not_repeat_energy(self):
        """周期只说周期：精力已经由体感说过，周期描述里再写一遍就是两遍。"""
        core = self.core(mode="full")
        core.scope.update_self(energy=95.0, current_cycle_day=25)
        core.soma.data.update({"arousal": 90.0})
        text = core.build_injection("42", is_group=False)
        spoken = [w for w in ("精力充沛", "人挺精神的", "脑子转得挺快", "状态挺好", "身上有劲") if w in text]
        self.assertLessEqual(len(spoken), 1, f"full 档下精力说了不止一遍：{spoken}\n{text}")

    def test_willingness_to_talk_is_described_once(self):
        """想不想说话只有 initiative 那一套措辞了。"""
        core = self.core()
        core.soma.data.update({"social_desire": 98.0})
        text = core.build_injection("42", is_group=False)
        for gone in ("有交流意愿", "交流意愿一般", "倾向独处", "社交意愿"):
            self.assertNotIn(gone, text, f"第二套社交意愿措辞又回来了「{gone}」：\n{text}")

    def test_mood_tag_stays_out_of_the_context(self):
        """心情标签是给人看的（README 一直这么说），不进模型。"""
        core = self.core()
        core.mood._refresh_tag("42", core.mood.profile("42"))
        tag = core.mood.tag("42")
        self.assertTrue(tag, "心情标签没生成，测不到它是否泄漏")
        text = core.build_injection("42", is_group=False)
        self.assertNotIn(tag, text, f"心情标签又进上下文了：{tag}\n{text}")

    # ------------------------------------------------------------------
    # 情绪事件跟着情绪轴回退
    # ------------------------------------------------------------------

    def test_mood_event_expires_with_the_axis(self):
        """气消了就不该还说「刚才TA惹她不痛快了」。"""
        core = self.core()
        record = core.mood.profile("42")
        record.update({"aggression": 45.0, "base_aggression": 28.0})
        self.add_mood_log(core, "42", "攻击性上升至 45.0", minutes_ago=20)
        self.assertIsNotNone(core.mood.last_emotional_event("42"), "刚惹完就该说得出来")
        # 回缓完了：当前值落回基线附近
        record.update({"aggression": 28.0})
        self.assertIsNone(
            core.mood.last_emotional_event("42"), "气都消了还报「惹她不痛快」"
        )

    def test_mood_event_window_follows_decay(self):
        """窗口跟情绪回缓周期（默认 6 小时），不是固定一整天。"""
        core = self.core()
        record = core.mood.profile("42")
        record.update({"aggression": 45.0, "base_aggression": 28.0})
        self.add_mood_log(core, "42", "攻击性上升至 45.0", minutes_ago=8 * 60)
        self.assertIsNone(
            core.mood.last_emotional_event("42"), "8 小时前的事还当今天提"
        )

    # ------------------------------------------------------------------
    # 事实给准数、不给档位话
    # ------------------------------------------------------------------

    def test_days_never_reach_the_context(self):
        """认识多久**不进上下文**，只留在 `/你的状态` 面板。

        把天数递给模型，它就会拿去念（「我们认识这么久了」是聊天里最没劲的一句话）。
        关系深浅由注入里的位置词（关系亲密／普通）承载，那才是她表达时需要的东西。
        """
        from humanoid.prompt_builder import PromptBuilder

        core = self.core()
        record = core.mood.profile("42")
        record["first_met"] = core.now_epoch() - 400 * 86400  # 认识了一年多
        core.scope.mark_dirty()
        text = core.build_injection("42", is_group=False)
        for leaked in ("认识1年", "认识400", "你们认识", "个月了", "天了"):
            self.assertNotIn(leaked, text, f"认识天数漏进上下文：{leaked}\n{text}")
        # 面板上照给准数
        panel = "\n".join(core.status_lines("42"))
        self.assertIn("认识 1 年", panel, f"面板上该看得到认识多久：\n{panel}")
        # 档位话（「认识几天了」）在任何地方都不该再出现
        self.assertNotIn("认识几天了", panel)
        self.assertEqual(
            PromptBuilder._days_line(3 * 86400), "认识 3 天",
            "面板上要的是准数，不是档位话",
        )

    # ------------------------------------------------------------------
    # 天气
    # ------------------------------------------------------------------

    def test_weather_text_has_no_english_city(self):
        """配置里是「Zelenogradsk,RU」，而城市名上一句已经用中文说过了。"""
        parsed = parse_payload(
            {"weather": [{"description": "多云"}], "main": {"temp": 12, "humidity": 60}}
        )
        self.assertIsNotNone(parsed)
        env = parsed["env"]
        self.assertNotIn("Zelenogradsk", env)
        self.assertNotIn("[", env, f"天气句里还带方括号地名：{env}")
        short = _short_weather({"env": env})
        self.assertIn("气温 12℃", short, f"温度被截掉了：{short}")
        self.assertNotIn("天气：", short, f"前缀没去掉：{short}")

    # ------------------------------------------------------------------
    # 日程句子
    # ------------------------------------------------------------------

    def test_schedule_phrase_has_no_double_particle(self):
        """日程里写「坐在客厅沙发上看书」，外面不能再套一个「在」。"""
        # 同一层不出现两个「在」：外层补的介词与动作自带的介词不能撞上。
        self.assertEqual(
            _with_at("现在", "坐在客厅沙发上看书并吃点水果"),
            "现在坐在客厅沙发上看书并吃点水果",
        )
        # 动作已经自带动词或介词时不再补，否则就是「现在在坐在客厅」。
        self.assertEqual(_with_at("现在", "看书"), "现在看书")
        self.assertEqual(_with_at("现在", "在看海报"), "现在在看海报")
        self.assertEqual(_with_at("上午", "在看海报"), "上午在看海报")
        self.assertEqual(_with_at("上午", "准备水果和下午茶"), "上午在准备水果和下午茶")
        long_event = "坐在客厅沙发上看书并吃点水果喝点茶休息一下慢慢再说吧"
        phrase = _event_phrase(long_event)
        self.assertLessEqual(len(phrase), 24, f"硬截超出上限：{phrase}")
        self.assertTrue(phrase.startswith("坐在客厅沙发"), phrase)

    def test_finished_segment_is_not_said_twice(self):
        """日程里刚结束的那段，不该既说「她刚在X」又说「她今天上午X」。"""
        core = self.core()
        core.scope.update_self(
            today_date=TODAY,
            daily_schedule=[
                {"start": "10:30", "end": "12:45", "event": "起身去厨房准备水果和下午茶"},
                {"start": "12:45", "end": "14:30", "event": "看书"},
            ],
        )
        text = core.build_injection("42", is_group=False)
        self.assertNotIn("她刚在起身去厨房", text, f"同一件事在一句里说了两遍：\n{text}")
        self.assertIn("起身去厨房准备水果和下午茶", text, f"「今天」那层把这件事丢了：\n{text}")

    # ------------------------------------------------------------------
    # 默认状态不能被说成消极 / 自相矛盾
    # ------------------------------------------------------------------

    def test_default_state_is_not_read_as_cold(self):
        """默认状态.inject 开头不能是「她不太想开话头」。

        旧基线 0.42 卡在 AGENCY_INITIATIVE 的中档门槛 0.45 下方，于是**每一条消息**的
        注入都把这句推给模型，一句话就把语气往冷淡上带。
        """
        core = self.core()
        core.mood.profile("42")
        text = core.build_injection("42", is_group=False, text="你好")
        for cold in ("不太想开话头", "没什么特别想说的"):
            self.assertNotIn(cold, text, f"默认状态被说成冷淡「{cold}」：\n{text}")

    def test_default_state_has_no_self_contradiction(self):
        """「心思飘在别处」与「她现在很闲」不能同时出现（一个说没在听，一个说有空）。"""
        core = self.core()
        core.mood.profile("42")
        text = core.build_injection("42", is_group=False, text="你好")
        drifting = ("心思飘在别处", "注意力没放在对话上")
        idle = ("她现在很闲", "她手头没什么事")
        self.assertFalse(
            any(w in text for w in drifting) and any(w in text for w in idle),
            f"既说她没在听、又说她有空：\n{text}",
        )

    def test_first_meeting_is_not_labelled_distant(self):
        """新认识的人第一面不该被说成「有点距离」。

        默认初始好感 46 折算出的在意度约 0.35~0.47，旧门槛 0.45 会让一半的人
        第一句就拿到「较为生疏／有点距离」——常和「你管TA叫宝宝」并列在同一句里。
        """
        core = self.core()
        self.assertEqual(core.config.mood_initial_affection, 46)
        for uid in ("555", "777", "3881756548", "1423008208"):
            core.scope.user_state(uid)["mood"] = {
                "affection": 46.0, "libido": 34.0, "aggression": 28.0,
                "base_affection": 46.0, "base_libido": 34.0, "base_aggression": 28.0,
                "first_met": core.now_epoch(), "last_interaction": core.now_epoch(),
                "last_decay": core.now_epoch(), "turn_count": 0, "messages_since_llm": 0,
            }
            core.scope.set_user(uid, "attention", {"care": 0.40, "care_at": core.now_epoch()})
        text = core.build_injection("555", is_group=False, text="你好")
        for distant in ("较为生疏", "有点距离", "关系疏远", "不常联系"):
            self.assertNotIn(distant, text, f"初始好感 46 却给出「{distant}」：\n{text}")

    def test_low_arousal_never_says_fine(self):
        """精疲力尽的人不能被描述成「状态平稳」——那和同一句里的「她很累」打架。"""
        from humanoid.wording import FEELING_WORDS, scale_word

        self.assertNotIn("状态平稳", scale_word(10.0, FEELING_WORDS["arousal"]))
        core = self.core()
        core.soma.data.update({"arousal": 10.0, "sleep_pressure": 95.0, "sleep_debt": 8.0})
        core.scope.update_self(energy=15.0)
        core.mood.profile("42")
        text = core.build_injection("42", is_group=False, text="你好")
        self.assertNotIn("状态平稳", text, f"很累却说她状态平稳：\n{text}")

    def test_pick_seed_uses_the_band_not_the_live_value(self):
        """同一天里数值小幅漂移不该换措辞（换个说法就像换了个人）。"""
        from humanoid.wording import FEELING_WORDS, band_index

        ladder = FEELING_WORDS["sleepy"]
        self.assertEqual(band_index(72.0, ladder), band_index(74.9, ladder))
        self.assertNotEqual(band_index(72.0, ladder), band_index(88.0, ladder))

    def test_relation_label_does_not_flap(self):
        """关系标签带滞回：好感 +0.1 不该从「亲密」跳成「信赖」。

        三条轴都按 12.5 跳档查表，单条消息好感上限 2 分——不滞回的话，一段对话里
        标签能来回跳好几次，模型读到的是「她一会儿对我心动一会儿对我信赖」，那像换了个人。
        """
        core = self.core()
        record = core.mood.profile("42")
        record.update({"affection": 87.4, "libido": 50.0, "aggression": 0.0,
                       "base_affection": 87.4, "base_libido": 50.0, "base_aggression": 0.0})
        first = core.mood.stable_label("42")
        for affection in (87.5, 88.0, 90.0, 92.0):
            record["affection"] = affection
            self.assertEqual(
                core.mood.stable_label("42"), first,
                f"好感只动了 {affection - 87.4:.1f} 分就换了标签",
            )
        # 漂够了才承认新档
        record["affection"] = 99.0
        self.assertNotEqual(core.mood.stable_label("42"), first, "漂够 6 分后该换档了")

    def test_reading_a_nickname_creates_no_record(self):
        """群聊里给每个人查一次称呼，不该在状态文件里留下几百个空壳。

        那些空壳不含任何会被过期清理的字段，`prune_expired` 直接跳过，
        state.json 就随群规模一直涨。
        """
        core = self.core(mood_enabled_in_group=False)
        for i in range(50):
            core.mood.nickname(f"qq_{i}")
        self.assertEqual(core.scope.all_user_ids(), [], "纯查询不该建用户条目")
        core.scope.set_user("real", "nickname", "小明")
        self.assertEqual(core.mood.nickname("real"), "小明", "真设了就要查得到")

    # ------------------------------------------------------------------
    # 长期不活跃 / 群聊 / 天气 / 半球
    # ------------------------------------------------------------------

    def test_expiry_keeps_name_but_forgets_closeness(self):
        """7 天不活跃：情绪归零（关系退回中性），但称呼留着，天数在面板上还在。

        清理只删情绪与原话记忆（`mood_data_retention_days`）；`first_met` 会被搬到
        user 级，所以 `/你的状态` 仍然知道认识多久——只是那句话不进上下文了。
        """
        core = self.core()
        first_met = core.now_epoch() - 400 * 86400
        state = core.scope.user_state("42")
        state["mood"] = {
            "affection": 70.0, "libido": 30.0, "aggression": 10.0,
            "base_affection": 60.0, "base_libido": 30.0, "base_aggression": 10.0,
            "first_met": first_met, "last_interaction": core.now_epoch() - 10 * 86400,
            "last_decay": 0, "turn_count": 30, "messages_since_llm": 0,
        }
        state["mood_logs"] = [{"time": "2026-01-01 00:00:00", "event": "好感度上升至 70.0"}]
        core.scope.set_user("42", "nickname", "小明")
        core.mood.prune_expired(now=core.now_epoch())

        after = core.scope.user_state("42")
        self.assertNotIn("mood", after, "情绪档案应被清掉")
        self.assertEqual(after.get("first_met"), first_met, "认识时间应被搬到不会被清理的位置")
        self.assertEqual(core.scope.get_user("42", "nickname"), "小明", "称呼要留下")

        # 情绪归零后关系位置退回中性——她不记得你们有多熟，但她还记得你叫什么。
        text = core.build_injection("42", is_group=False, text="好久不见")
        self.assertIn("你管TA叫小明", text)
        self.assertIn("她对TA", text, f"关系位置仍要给（中性那档）：\n{text}")
        self.assertNotIn("关系亲密", text, f"情绪都清零了还说她亲密：\n{text}")
        panel = "\n".join(core.status_lines("42"))
        self.assertIn("认识 1 年", panel, f"面板上仍然知道认识多久：\n{panel}")

    def test_group_injection_has_no_role_level_chatter_count(self):
        """「TA今天话不少」是角色级计数，给群里的每个人都是同一句——私聊才给。"""
        core = self.core()
        core.scope.update_self(daily_msg_date=TODAY, daily_msg_count=50)
        core.mood.profile("42")
        # 「今天话不少」属于间隔那块，没有间隔事件时整块都不出现，先造一个回来事件。
        core.behavior.add_event("42", {
            "type": "user_returned", "timestamp": core.now_epoch() - 3600,
            "importance": 0.5, "data": {"gap_seconds": 3600.0},
        })
        group = core.build_injection("42", is_group=True, text="在吗")
        self.assertNotIn("TA今天话不少", group, f"群聊里混进了角色级计数：\n{group}")
        private = core.build_injection("42", is_group=False, text="在吗")
        self.assertIn("TA今天话不少", private, f"私聊里应该有这句：\n{private}")

    def test_weather_notice_never_reaches_the_context(self):
        """配置说明书按 notice 字段拦住，不再靠猜字符串。"""
        from humanoid.prompt_builder import _short_weather

        for notice in (
            {"weather": "", "env": "天气还没取到", "notice": True},
            {"weather": "", "env": "天气数据过期了", "notice": True},
            {"weather": "", "env": "天气未开启", "notice": True},
            {"weather": "", "env": "完全没见过的提示语", "notice": True},
        ):
            self.assertEqual(_short_weather(notice), "", f"说明书漏进上下文：{notice}")
        real = {"weather": "多云 🌡️ 12°C", "env": "天气：多云，气温 12℃", "notice": False}
        self.assertIn("气温 12℃", _short_weather(real))

    def test_southern_hemisphere_in_america(self):
        """美洲也有南半球城市：布宜诺斯艾利斯 6 月该是冬天。"""
        winter = datetime(2026, 6, 15, 12, 0, tzinfo=TZ)
        store = StateStore(Path(tempfile.mkdtemp()) / "s.json", lambda: 0.01)
        store.load(winter.strftime("%Y-%m-%d"), 28)
        conf = HumanoidConfig.from_raw({"timezone_city": "北京"})
        core = HumanoidCoreInstance(
            role_id="bot1", state_store=store, config_provider=lambda: conf,
            logger=RecordingLogger(), stop_event=asyncio.Event(),
            resolver=FakeContext(), gateway=None,
        )
        core.clock = FrozenClock(winter, city="America/Argentina/Buenos_Aires")
        text = core.build_injection("42", is_group=False)
        self.assertIn("入冬了", text, f"南半球 6 月该是冬天：\n{text}")
        self.assertNotIn("入夏了", text, f"南半球 6 月说成夏天：\n{text}")

    # ------------------------------------------------------------------
    # 场景
    # ------------------------------------------------------------------

    def test_season_follows_the_role_city(self):
        """角色单独设了城市时，季节按那个城市判，而不是按全局配置。

        全局配置是北京（北半球，9 月该说入秋），而这个角色自己设的是悉尼（南半球，
        9 月是春天）。读全局配置就会把南半球的角色说成入秋——修正前正是这么错的。
        """
        south = self.core()
        south.clock = FrozenClock(MOMENT, city="Australia/Sydney")
        text = south.build_injection("42", is_group=False)
        self.assertNotIn("入秋了", text, f"9 月的悉尼角色被告知入秋了：\n{text}")
        self.assertIn("开春了", text, f"南半球 9 月该是春天：\n{text}")

    def test_injection_stays_within_budget(self):
        for mode in ("medium", "full", "mood_only"):
            core = self.core(mode=mode)
            text = core.build_injection("42", is_group=False)
            self.assertLessEqual(
                len(text), INJECT_MAX_CHARS[mode], f"{mode} 档注入超长：{len(text)}"
            )


if __name__ == "__main__":
    unittest.main()
