"""v2.15 的三条链：日程按 AstrBot 人格排、说得出今天干了什么、记得住聊过的事。

这些功能都属于「组件被测到、装配没被测到」最容易翻车的地方（v2.13.2 的精力就是这么
整天不动的），所以每条都从插件真正的入口走一遍，不只测纯函数。
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from humanoid.config import HumanoidConfig
from humanoid.core_instance import HumanoidCoreInstance
from humanoid.link import build_contract
from humanoid.persona import Persona, PersonaSource, resolve_persona, truncate
from humanoid.services.schedule import (
    SOURCE_LLM,
    ScheduleService,
    day_lines,
    day_phrases,
    segment_prompt,
)
from humanoid.slots import normalize_slots
from humanoid.state import StateStore
from humanoid.recall import note_said, recall_lines, snippet

from .fakes import (
    FakeContext,
    FakeConversationManager,
    FakePersonaManager,
    FakeProvider,
    FrozenClock,
    RecordingLogger,
    fake_persona,
)

TZ = ZoneInfo("Asia/Shanghai")
MOMENT = datetime(2026, 8, 22, 15, 20, tzinfo=TZ)
TODAY = "2026-08-22"

PERSONA_TEXT = "你是林小满，24 岁，在一家设计公司做平面，养一只叫饭团的橘猫，住在出租屋里。"

DAY_SCHEDULE = [
    {"start": "00:00", "end": "07:30", "event": "睡眠休息", "location": "卧室", "emotion": "沉睡", "energy_rate": 0.15},
    {"start": "07:30", "end": "09:00", "event": "洗漱与早餐", "location": "家里", "emotion": "迷糊", "energy_rate": 0.05},
    {"start": "09:00", "end": "12:00", "event": "改第三季度的海报", "location": "公司", "emotion": "专注", "energy_rate": -0.1},
    {"start": "12:00", "end": "13:30", "event": "午餐与午休", "location": "公司楼下", "emotion": "放松", "energy_rate": 0.08},
    {"start": "13:30", "end": "17:30", "event": "跟客户过方案", "location": "会议室", "emotion": "紧绷", "energy_rate": -0.12},
    {"start": "17:30", "end": "19:30", "event": "通勤与买菜", "location": "路上", "emotion": "散漫", "energy_rate": -0.05},
    {"start": "19:30", "end": "23:00", "event": "做饭与看剧", "location": "家里", "emotion": "松", "energy_rate": 0.02},
    {"start": "23:00", "end": "24:00", "event": "睡眠", "location": "卧室", "emotion": "困", "energy_rate": 0.15},
]

GOOD_SCHEDULE = json.dumps(
    [
        {"start": "00:00", "end": "07:30", "event": "睡眠", "location": "卧室", "emotion": "平静", "energy_rate": 0.15},
        {"start": "07:30", "end": "12:00", "event": "接商稿", "location": "书房", "emotion": "专注", "energy_rate": -0.1},
        {"start": "12:00", "end": "13:00", "event": "午餐", "location": "厨房", "emotion": "松", "energy_rate": 0.05},
        {"start": "13:00", "end": "18:00", "event": "继续改稿", "location": "书房", "emotion": "烦", "energy_rate": -0.1},
        {"start": "18:00", "end": "23:00", "event": "遛猫与做饭", "location": "小区", "emotion": "开心", "energy_rate": -0.02},
        {"start": "23:00", "end": "24:00", "event": "洗漱入睡", "location": "卧室", "emotion": "困", "energy_rate": 0.15},
    ],
    ensure_ascii=False,
)


def freeze(core, moment=MOMENT):
    """把角色的所有服务换成同一个假时钟。

    各服务在构造时就捕获了 clock 对象，只换 core.clock 会让它们各看各的时间。
    """
    clock = FrozenClock(moment)
    core.clock = clock
    for service in (core.schedule, core.soma, core.energy, core.process, core.mood, core.social, core.weather):
        service._clock = clock
    return core


def cfg(**overrides) -> HumanoidConfig:
    return HumanoidConfig.from_raw({"timezone_city": "北京", **overrides})


class PersonaSourceTest(unittest.TestCase):
    """人格解析与缓存：多角色下各取各的，不能被一个全局值压成同一个人。"""

    def context(self, marker_for: str = "") -> FakeContext:
        return FakeContext(
            persona_manager=FakePersonaManager(
                personas={"cat": fake_persona("林小满", PERSONA_TEXT)},
                default=fake_persona("默认", "你是一个乐于助人的AI助手。"),
                marker_for=marker_for,
            ),
            conversation_manager=FakeConversationManager({"qq:FriendMessage:1": "cat"}),
        )

    async def asyncSetUp(self):
        pass

    def run_it(self, coro):
        import asyncio

        return asyncio.run(coro)

    def test_uses_conversation_persona_of_the_recorded_umo(self):
        ctx = self.context()
        src = PersonaSource(ctx, RecordingLogger())
        src.note_umo("bot1", "qq:FriendMessage:1")
        persona = self.run_it(src.persona("bot1"))
        self.assertEqual(persona.name, "林小满")
        self.assertIn("饭团", persona.prompt)

    def test_no_umo_falls_back_to_default_persona(self):
        ctx = self.context()
        src = PersonaSource(ctx, RecordingLogger())
        persona = self.run_it(src.persona("bot1"))
        self.assertEqual(persona.name, "默认")

    def test_none_marker_is_not_the_default_persona(self):
        """[%None] 的含义是「这个会话显式不用人格」，回退到默认人格是反的。"""
        ctx = self.context(marker_for="qq:GroupMessage:9")
        src = PersonaSource(ctx, RecordingLogger())
        src.note_umo("bot1", "qq:GroupMessage:9")
        persona = self.run_it(src.persona("bot1"))
        self.assertFalse(persona.usable)
        self.assertEqual(persona.name, "")

    def test_roles_do_not_share_one_persona(self):
        ctx = self.context()
        src = PersonaSource(ctx, RecordingLogger())
        src.note_umo("bot1", "qq:FriendMessage:1")
        src.note_umo("bot2", "")
        one = self.run_it(src.persona("bot1"))
        two = self.run_it(src.persona("bot2"))
        self.assertEqual(one.name, "林小满")
        self.assertEqual(two.name, "默认")

    def test_cached_until_the_umo_moves(self):
        ctx = self.context()
        src = PersonaSource(ctx, RecordingLogger())
        src.note_umo("bot1", "qq:FriendMessage:1")
        self.run_it(src.persona("bot1"))
        self.run_it(src.persona("bot1"))
        self.assertEqual(ctx.persona_manager.calls, 1, "同一个会话来源不该每条消息都重解一次")
        src.note_umo("bot1", "qq:GroupMessage:2")
        self.run_it(src.persona("bot1"))
        self.assertEqual(ctx.persona_manager.calls, 2)

    def test_broken_persona_manager_does_not_break_generation(self):
        class Boom:
            async def resolve_selected_persona(self, **kwargs):
                raise RuntimeError("炸了")

        src = PersonaSource(FakeContext(persona_manager=Boom()), RecordingLogger())
        src.note_umo("bot1", "qq:FriendMessage:1")
        self.assertFalse(self.run_it(src.persona("bot1")).usable)

    def test_missing_persona_manager_returns_empty(self):
        self.assertIsNone(FakeContext().persona_manager)
        self.assertEqual(self.run_it(resolve_persona(FakeContext(), "")), Persona())

    def test_truncate_cuts_on_line_boundary(self):
        text = "\n".join(f"第{i}行人设内容写得很清楚" for i in range(200))
        cut = truncate(text, 120)
        self.assertLessEqual(len(cut), 160)
        self.assertTrue(cut.endswith("（以上是完整人设的开头部分。）"))
        self.assertFalse(cut.split("（")[0].endswith("写"), "不该切在一句话中间")


class SchedulePersonaTest(unittest.TestCase):
    """日程 prompt 里到底有没有她这个人。"""

    def build_service(self, conf=None, persona=None, providers=()):
        import tempfile
        from pathlib import Path

        from humanoid.llm import LLMGateway, ProviderResolver
        from humanoid.role_scope import RoleScope

        store = StateStore(Path(tempfile.mkdtemp()) / "state.json", lambda: 0.01)
        store.load(TODAY, 28)
        scope = RoleScope(store.data, "bot1")
        clock = FrozenClock(MOMENT)
        log = RecordingLogger()
        conf = conf or cfg()
        resolver = ProviderResolver(FakeContext(chat_providers=list(providers)), log)
        gateway = LLMGateway(resolver, lambda: conf, log)
        service = ScheduleService(
            scope,
            lambda: conf,
            clock,
            logger=log,
            monotonic=lambda: 1000.0,
            persona_provider=persona,
        )
        service.set_resolver_gateway(resolver, gateway)
        return service, scope

    def test_prompt_carries_the_persona_not_a_personality_label(self):
        prompt = segment_prompt(cfg(), now_text="15:20", weekday="六", persona=Persona("林小满", PERSONA_TEXT, "会话生效人格"))
        self.assertIn("林小满", prompt)
        self.assertIn("饭团", prompt)
        self.assertIn("她是「林小满」", prompt)
        # 旧版那句「请为「温柔体贴」这个人设规划今天」不该再以另一种形式回来
        self.assertNotIn("温柔体贴", prompt)
        # 原来这里断言的是「每天都沿用同一个答案」——那句出自
        # 「设定里没写的，按最合理、最省心的方式补一个出来」，是在命令模型替她造身份。
        # 现在人设照旧整段进 prompt，但没提到的部分要求留白。
        self.assertIn("不要替他编", prompt)

    def test_prompt_without_persona_stays_sane(self):
        for persona in (None, Persona(), Persona(name="空壳", prompt="", source="未取到人设")):
            prompt = segment_prompt(cfg(), now_text="15:20", weekday="六", persona=persona)
            # 无人设时仍要给得出可执行的指引（否则模型不知道该排什么），
            # 但不能假设她的职业——旧句「按一份普通上班族/学生的真实日子补」会给她安身份
            self.assertIn("只输出一个 JSON 对象", prompt)
            self.assertIn("有事要做", prompt)
            self.assertNotIn("上班族", prompt)
            self.assertNotIn("学生", prompt)

    def test_giant_persona_is_clamped_by_the_prompt_builder(self):
        """日程一天要跑 1~2 次，不能因为谁把人设写成一万字就撑成七千 token 的请求。

        resolve_persona 已经截过一次，但生成日程这段不该信任调用方递进来的东西。
        """
        from humanoid.persona import PERSONA_PROMPT_MAX
        from humanoid.prompt_builder import estimate_tokens

        unit = "你是一个温柔体贴的女孩子，说话很短。"
        huge = Persona("小娜", unit * 360, "会话生效人格")        # ~9000 字
        bigger = Persona("小娜", unit * 4000, "会话生效人格")     # ~10 万字
        self.assertGreater(len(huge.prompt), 5000, "构造的样本本身要够长才有意义")
        prompt = segment_prompt(cfg(), now_text="15:20", weekday="六", persona=huge)
        # 真正要保证的是「不随输入膨胀」：再大十倍，请求不该跟着变大
        self.assertLessEqual(estimate_tokens(segment_prompt(cfg(), now_text="15:20", weekday="六", persona=bigger)),
                             estimate_tokens(prompt) + 50)
        # 绝对量：日程一天只跑 1~2 次，输入留在两千 token 内就够安全
        self.assertLessEqual(estimate_tokens(prompt), 2000, f"实测 {estimate_tokens(prompt)}")
        # 人设最多只能把 prompt 撑大它自己那点上界，超出部分该被截掉
        self.assertLessEqual(len(prompt) - len(segment_prompt(cfg(), now_text="15:20", weekday="六")),
                             PERSONA_PROMPT_MAX + 120)
        self.assertIn("以上是完整人设的开头部分", prompt)
        # 带换行的人设也要按行边界切，不能把一句话砍一半
        lines = Persona("小娜", "\n".join(["第%d行人设内容" % i for i in range(2000)]), "x")
        cut = segment_prompt(cfg(), now_text="15:20", weekday="六", persona=lines)
        self.assertLessEqual(len(cut) - len(segment_prompt(cfg(), now_text="15:20", weekday="六")),
                             PERSONA_PROMPT_MAX + 120)
        self.assertFalse(cut.rstrip().endswith("内容写"), "不该切在一句话中间")

    def test_extra_preference_still_applies(self):
        prompt = segment_prompt(cfg(schedule_prompt_extra="最近在准备考研"), now_text="15:20", weekday="六")
        self.assertIn("最近在准备考研", prompt)

    def test_generation_asks_with_persona_and_records_it(self):
        provider = FakeProvider("p", reply=GOOD_SCHEDULE)
        called = []

        async def persona_source():
            called.append(1)
            return Persona("林小满", PERSONA_TEXT, "会话生效人格")

        service, scope = self.build_service(
            cfg(schedule_provider_name="p", schedule_allow_global_fallback=False),
            persona=persona_source,
            providers=[provider],
        )
        import asyncio

        asyncio.run(service.ensure_fresh(force=True, ignore_cooldown=True))
        self.assertEqual(service.source, SOURCE_LLM)
        self.assertIn("林小满", provider.last_kwargs["prompt"])
        self.assertEqual(scope.get_self("schedule_persona"), "林小满")
        self.assertEqual(service.status()["persona"], "林小满")

    def test_switch_off_sends_no_persona(self):
        provider = FakeProvider("p", reply=GOOD_SCHEDULE)

        async def persona_source():
            return Persona("林小满", PERSONA_TEXT, "会话生效人格")

        service, scope = self.build_service(
            cfg(
                schedule_provider_name="p",
                schedule_allow_global_fallback=False,
                schedule_use_persona=False,
            ),
            persona=persona_source,
            providers=[provider],
        )
        import asyncio

        asyncio.run(service.ensure_fresh(force=True, ignore_cooldown=True))
        self.assertEqual(service.source, SOURCE_LLM)
        self.assertNotIn("林小满", provider.last_kwargs["prompt"])

    def test_broken_persona_source_still_generates(self):
        provider = FakeProvider("p", reply=GOOD_SCHEDULE)

        async def persona_source():
            raise RuntimeError("人格服务没起来")

        service, _ = self.build_service(
            cfg(schedule_provider_name="p", schedule_allow_global_fallback=False),
            persona=persona_source,
            providers=[provider],
        )
        import asyncio

        asyncio.run(service.ensure_fresh(force=True, ignore_cooldown=True))
        self.assertEqual(service.source, SOURCE_LLM, "读不到人设不该让日程也停掉")


class DayNarrativeTest(unittest.TestCase):
    """「说得出今天干了什么」——同一份日程在不同钟点该给出不同的话。"""

    def slots(self):
        return normalize_slots(DAY_SCHEDULE, max_slots=16)

    def phrases_at(self, hour: int, minute: int = 0):
        return day_phrases(self.slots(), hour * 60 + minute)

    def test_mid_afternoon_names_the_meeting_and_the_previous_block(self):
        got = self.phrases_at(15, 20)
        self.assertEqual(got["doing"], "跟客户过方案")
        self.assertIn("中午在午餐与午休", got["done"])
        self.assertTrue(got["next"], "15:20 离 17:30 不到三小时，该有「接下来」")
        lines = day_lines(got)
        self.assertEqual(len(lines), 1)
        self.assertIn("今天到这会：", lines[0])
        self.assertIn("现在在跟客户过方案", lines[0])

    def test_sleep_block_is_not_reported_as_an_activity(self):
        got = self.phrases_at(8, 0)
        self.assertNotIn("睡眠休息", " ".join(got["done"]))
        self.assertEqual(got["doing"], "洗漱与早餐")

    def test_morning_is_not_cluttered_with_ancient_history(self):
        """早上九点不该把昨晚的做饭看剧算成「刚做过」。"""
        got = self.phrases_at(9, 0)
        self.assertNotIn("做饭与看剧", " ".join(got["done"]))

    def test_empty_schedule_produces_no_line(self):
        self.assertEqual(day_lines({"doing": "", "done": [], "next": []}), [])
        self.assertEqual(day_lines(day_phrases([], 600)), [])

    def test_injection_does_not_narrate_the_day(self):
        """日程进上下文的是事实（今天到这会过了什么），不带任何过程要求。"""
        import asyncio
        import tempfile
        from pathlib import Path

        store = StateStore(Path(tempfile.mkdtemp()) / "state.json", lambda: 0.01)
        store.load(TODAY, 28)
        conf = cfg(inject_activity_context="low", timezone_city="北京")
        core = HumanoidCoreInstance(
            role_id="bot1",
            state_store=store,
            config_provider=lambda: conf,
            logger=RecordingLogger(),
            stop_event=asyncio.Event(),
            resolver=FakeContext(),
            gateway=None,
        )
        freeze(core)
        slots = normalize_slots(DAY_SCHEDULE, max_slots=16)
        core.scope.update_self(today_date=TODAY, daily_schedule=slots, schedule_source=SOURCE_LLM)
        text = core.build_injection("42", is_group=False)
        self.assertIn("她今天", text)
        self.assertIn("跟客户过方案", text)
        # 只给事实，不给规矩：不出现「要/别/不要」这类过程指令。
        self.assertNotIn("不要", text)
        self.assertNotIn("接着聊", text)
        # 数据源还在：day_phrases 照常能算出今天干了什么（契约/诊断用）。
        got = day_phrases(slots, 15 * 60 + 20)
        self.assertIn("跟客户过方案", got["doing"])


class RecallTest(unittest.TestCase):
    """记得住聊过的事：按间隔采样对方原话，不为此多调一次模型。"""

    def test_short_replies_are_not_memorised(self):
        self.assertEqual(note_said([], "嗯", 1000.0), [])
        self.assertEqual(note_said([], "好的", 1000.0), [])

    def test_sampling_gap_keeps_it_out_of_recent_history(self):
        got = note_said([], "我明天上午十点有一场面试", 1000.0)
        self.assertEqual(len(got), 1)
        # 三分钟内接着聊的每句话都记，就只是最近几条聊天记录而已
        again = note_said(got, "面试要考两小时的专业课", 1120.0)
        self.assertEqual(len(again), 1, "采样间隔内的消息不该一条条都记下去")
        later = note_said(got, "面试要考两小时的专业课", 4000.0)
        self.assertEqual(len(later), 2)
        self.assertEqual(later[0]["said"], "面试要考两小时的专业课")

    def test_list_is_capped(self):
        got = []
        for i in range(10):
            got = note_said(got, f"这是第{i}条要记住的事情啊", 1000.0 + i * 7200.0)
        self.assertLessEqual(len(got), 4)
        self.assertIn("第9", got[0]["said"])

    def test_snippet_ends_on_a_clause_boundary(self):
        body = "今天跟产品经理吵了一架，他说这个需求本周就要，我真的很烦"
        cut = snippet(body)
        self.assertLessEqual(len(cut), 26)
        self.assertTrue(body.startswith(cut.rstrip("…")))
        self.assertTrue(cut.endswith("…") or cut.endswith("要"), "要么收到子句边界，要么标上省略号")
        self.assertNotIn("我真的很烦", cut)

    def test_recall_line_reports_who_said_it(self):
        got = note_said([], "我猫今天吐了，带去医院看了一下", 1000.0)
        lines = recall_lines(got, 1000.0 + 600)
        self.assertEqual(len(lines), 1)
        self.assertIn("TA之前说过", lines[0])
        self.assertIn("我猫今天吐了", lines[0])

    def test_stale_memories_are_not_injected(self):
        got = note_said([], "我猫今天吐了，带去医院看了一下", 1000.0)
        self.assertEqual(recall_lines(got, 1000.0 + 90 * 3600.0), [])

    def test_on_message_records_and_injection_uses_it(self):
        import asyncio
        import tempfile
        from pathlib import Path

        async def go():
            store = StateStore(Path(tempfile.mkdtemp()) / "state.json", lambda: 0.01)
            store.load(TODAY, 28)
            conf = cfg(inject_activity_context="low", timezone_city="北京")
            core = HumanoidCoreInstance(
                role_id="bot1",
                state_store=store,
                config_provider=lambda: conf,
                logger=RecordingLogger(),
                stop_event=asyncio.Event(),
                resolver=FakeContext(),
                gateway=None,
            )
            core.on_message("42", "我明天要去面试了，有点紧张", umo="qq:FriendMessage:42")
            self.assertTrue(core.mood.said("42"), "聊过的原话该被记下来")
            text = core.build_injection("42", is_group=False)
            self.assertIn("TA之前说过", text)
            self.assertIn("我明天要去面试了", text)
            self.assertEqual(store.data["roles"]["bot1"]["self"]["last_umo"], "qq:FriendMessage:42")
            await asyncio.sleep(0)

        asyncio.run(go())

    def test_group_without_group_mood_records_nothing(self):
        import asyncio
        import tempfile
        from pathlib import Path

        async def go():
            store = StateStore(Path(tempfile.mkdtemp()) / "state.json", lambda: 0.01)
            store.load(TODAY, 28)
            conf = cfg(mood_enabled_in_group=False, timezone_city="北京")
            core = HumanoidCoreInstance(
                role_id="bot1",
                state_store=store,
                config_provider=lambda: conf,
                logger=RecordingLogger(),
                stop_event=asyncio.Event(),
                resolver=FakeContext(),
                gateway=None,
            )
            core.on_message("42", "我明天要去面试了，有点紧张", is_group=True)
            self.assertEqual(core.mood.said("42"), [])
            await asyncio.sleep(0)

        asyncio.run(go())

    def test_prune_drops_the_memory_too(self):
        import asyncio
        import tempfile
        from pathlib import Path

        async def go():
            path = Path(tempfile.mkdtemp()) / "state.json"
            store = StateStore(path, lambda: 0.01)
            store.load(TODAY, 28)
            conf = cfg(mood_data_retention_days=7, timezone_city="北京")
            core = HumanoidCoreInstance(
                role_id="bot1",
                state_store=store,
                config_provider=lambda: conf,
                logger=RecordingLogger(),
                stop_event=asyncio.Event(),
                resolver=FakeContext(),
                gateway=None,
            )
            core.on_message("42", "面试的事谢谢关心啦")
            self.assertTrue(core.mood.said("42"))
            user = store.data["roles"]["bot1"]["users"]["42"]
            user["last_interaction"] = core.mood._time() - 30 * 86400.0
            self.assertGreater(core.mood.prune_expired(), 0)
            self.assertEqual(core.mood.said("42"), [])
            await asyncio.sleep(0)

        asyncio.run(go())


class EmotionLineTest(unittest.TestCase):
    """情绪该改变说话，而不是只贴一个标签词。"""

    def core_with_mood(self, **mood):
        import asyncio
        import tempfile
        from pathlib import Path

        store = StateStore(Path(tempfile.mkdtemp()) / "state.json", lambda: 0.01)
        store.load(TODAY, 28)
        conf = cfg(inject_activity_context="low", timezone_city="北京")
        core = HumanoidCoreInstance(
            role_id="bot1",
            state_store=store,
            config_provider=lambda: conf,
            logger=RecordingLogger(),
            stop_event=asyncio.Event(),
            resolver=FakeContext(),
            gateway=None,
        )
        record = core.mood.profile("42")
        record.update(mood)
        return core

    def test_still_angry_shows_in_the_label_not_a_script(self):
        """情绪信到标签为止：状态照给，怎么开口不由插件写。"""
        core = self.core_with_mood(affection=55.0, libido=10.0, aggression=40.0, base_aggression=28.0)
        text = core.build_injection("42", is_group=False)
        self.assertIn("她对TA", text)
        # 攻击性偏高的那一档，标签本身就得能看出不对劲（如「不讲理」「较劲」）。
        self.assertTrue(any(w in text for w in ("不讲理", "较劲", "炸毛", "嘴硬", "别扭")), text)
        for script in ("积了点火", "攒着点事", "火压在底下", "说话会短", "顶回去"):
            self.assertNotIn(script, text)

    def test_attitude_line_stops_at_the_feeling(self):
        """插件只说她心里怎么样，不替她决定这句话怎么说出口。"""
        core = self.core_with_mood(affection=55.0, libido=10.0, aggression=40.0, base_aggression=28.0)
        text = core.build_injection("42", is_group=False)
        self.assertNotIn("说话会短、会顶回去", text)
        self.assertNotIn("保持距离就好", text)

    def test_neutral_mood_adds_no_attitude_line(self):
        core = self.core_with_mood(affection=50.0, libido=25.0, aggression=20.0)
        text = core.build_injection("42", is_group=False)
        self.assertNotIn("说话会短、会顶回去", text)

    def test_framing_survives_where_it_belongs(self):
        """许可与「怎么读」那句搬到 system_prompt 后，事实块里不该再重复它。"""
        from humanoid.prompt_builder import FRAMING_TEXT

        core = self.core_with_mood()
        text = core.build_injection("42", is_group=False)
        self.assertNotIn("不必逐条回应", text)
        self.assertIn("由她自己判断", FRAMING_TEXT)
        self.assertIn("处境", FRAMING_TEXT)



class ContractDayTest(unittest.TestCase):
    """契约里要带上今天这条时线，社交层才有得说。"""

    def build_core(self):
        import asyncio
        import tempfile
        from pathlib import Path

        store = StateStore(Path(tempfile.mkdtemp()) / "state.json", lambda: 0.01)
        store.load(TODAY, 28)
        conf = cfg(soma_enabled=True, contract_enabled=True, timezone_city="北京")
        core = HumanoidCoreInstance(
            role_id="bot1",
            state_store=store,
            config_provider=lambda: conf,
            logger=RecordingLogger(),
            stop_event=asyncio.Event(),
            resolver=FakeContext(),
            gateway=None,
        )
        freeze(core)
        slots = normalize_slots(DAY_SCHEDULE, max_slots=16)
        core.scope.update_self(today_date=TODAY, daily_schedule=slots, schedule_source=SOURCE_LLM)
        return core

    def test_contract_carries_day_and_persona(self):
        core = self.build_core()
        contract = build_contract(core)
        self.assertIn("day", contract)
        self.assertEqual(contract["day"]["doing"], "跟客户过方案")
        self.assertTrue(contract["day"]["done"])
        self.assertEqual(contract["persona"], "")
        self.assertIn("user_said", contract["paths"])

    def test_contract_version_and_existing_keys_untouched(self):
        core = self.build_core()
        contract = build_contract(core)
        for key in ("v", "body", "feelings", "form", "activity", "time", "routine", "paths"):
            self.assertIn(key, contract)
        for key in ("sleep_pressure", "social_desire", "arousal"):
            self.assertIn(key, contract["body"])


if __name__ == "__main__":
    unittest.main()
