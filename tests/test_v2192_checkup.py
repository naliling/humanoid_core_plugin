"""v2.19.2 全身体检的回归项。

核心是一条守卫：`_conf_schema.json` 上每一项都必须真的能改到 `HumanoidConfig`。
面板有 89 项，靠人眼核对「这项接没接线」迟早会漏——v2.19.1 之前 `inject_token_budget`
就是这么静默失效的（from_raw 忘了读它，面板改完永远是 3500）。剩下的项都是本轮体检
里实际跑出来的毛病：天气没配却报「晴朗 ☀️」、睡着时注入出「她这会儿她在睡」这种病句。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from humanoid.config import HumanoidConfig
from humanoid.engine import HumanoidEngine
from humanoid.link import build_contract
from humanoid.services.mood import validate_auto_nickname
from humanoid.services.weather import WeatherService, is_notice
from humanoid.state import StateStore

from .fakes import FrozenClock, FakeContext, RecordingLogger, ScopeStore
from .test_main_integration import FakeEvent, collect, main_module

SCHEMA: dict = json.loads(
    (Path(__file__).resolve().parent.parent / "_conf_schema.json").read_text(encoding="utf-8")
)
DEFAULTS = HumanoidConfig()

# 列表项要按各自的元素格式造，不能一律塞字符串。
LIST_PROBES = {
    "admin_qq": ["13579"],
    "energy_recovery_phase_multipliers": [0.5, 1.5, 2.5, 1.5, 0.5, 0.5],
    "mood_affection_override": ["13579:33"],
    "holidays": [{"date": "2026-10-01", "name": "体检节"}],
}


def probe_value(key: str) -> object:
    """给这一项造一个「与默认值明确不同」的合法值。"""
    spec = SCHEMA[key]
    kind = spec.get("type")
    default = spec.get("default")
    if key in LIST_PROBES:
        return LIST_PROBES[key]
    if kind == "bool":
        return not bool(default)
    if kind == "int":
        base = int(default or 0)
        low, high = spec.get("minimum"), spec.get("maximum")
        for cand in (base + 3, base - 3, (high - 1 if high is not None else None), (low + 1 if low is not None else None), 7):
            if cand is None or cand == base:
                continue
            if low is not None and cand < low:
                continue
            if high is not None and cand > high:
                continue
            return cand
        return base + 1
    if kind == "float":
        base = float(default or 0.0)
        low, high = spec.get("minimum"), spec.get("maximum")
        for cand in (base + 1.5, base - 1.5, 3.25):
            if cand == base:
                continue
            if low is not None and cand < low:
                continue
            if high is not None and cand > high:
                continue
            return cand
        return base + 0.5
    options = spec.get("options") or []
    if options:
        for option in options:
            if option != default:
                return option
        return options[0]
    if key == "weather_api_key":
        return "checkup-key-0123456789"
    if key in ("timezone_city",):
        return " Osaka"
    return "体检专用值"


def make_core(moment: datetime, **conf):
    """一份真 HumanoidCoreInstance：足够驱动注入与状态，不跑后台循环。"""

    import asyncio

    from humanoid.core_instance import HumanoidCoreInstance

    store = StateStore(Path(tempfile.mkdtemp()) / "state.json", lambda: 0.01)
    store.load(moment.strftime("%Y-%m-%d"), 28)
    config = HumanoidConfig.from_raw({"timezone_city": "北京", **conf})
    return HumanoidCoreInstance(
        role_id="bot1", state_store=store, config_provider=lambda: config,
        logger=RecordingLogger(), stop_event=asyncio.Event(), resolver=FakeContext(), gateway=None,
    )


def wrecked(moment: datetime, **conf):
    """把她推到「有事可说」：累、饿、困、手上有一串日程。"""

    core = make_core(moment, **conf)
    core.clock = FrozenClock(moment)
    now_min = moment.hour * 60 + moment.minute
    segs = []
    for index, event in enumerate(("改海报", "吃午饭", "楼下买东西", "洗澡", "刷手机")):
        start = max(0, now_min - 150 + index * 30)
        end = min(1439, start + 30)
        segs.append({
            "start": f"{start // 60:02d}:{start % 60:02d}",
            "end": f"{end // 60:02d}:{end % 60:02d}",
            "event": event, "location": "家中", "emotion": "平静", "energy_rate": -0.05,
        })
    core.schedule._scope.update_self(
        today_date=moment.strftime("%Y-%m-%d"), daily_schedule=segs
    )
    core.soma.data.update(
        {"sleep_pressure": 88.0, "hunger": 80.0, "discomfort": 70.0, "arousal": 20.0}
    )
    core.energy._scope.update_self(energy=30.0)
    core.mood.set_nickname("42", "小鱼")
    core.mood.note_said("42", "我明天上午要去面试，紧张", now=core.now_epoch() - 3600.0)
    core.mood._scope.set_user("42", "last_interaction", core.now_epoch() - 3 * 3600.0)
    return core


class SceneAwarenessTest(unittest.TestCase):
    """`enable_chat_awareness` 以前全项目零引用：面板上摆着，改了也不会变。"""

    MOMENT = datetime(2026, 9, 25, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    def test_group_scene_honors_the_toggle(self):
        on = wrecked(self.MOMENT, enable_chat_awareness=True)
        off = wrecked(self.MOMENT, enable_chat_awareness=False)
        said_on = on.build_injection("42", is_group=True)
        said_off = off.build_injection("42", is_group=True)
        self.assertIn("周围还有人看着", said_on, said_on)
        self.assertNotIn("周围还有人看着", said_off, said_off)
        self.assertIn("群聊", said_off, "关掉这一层也不能把「这是群聊」一并抹掉")

    def test_private_chat_is_untouched_by_the_toggle(self):
        off = wrecked(self.MOMENT, enable_chat_awareness=False)
        text = off.build_injection("42", is_group=False)
        self.assertIn("私聊", text)


class TokenBudgetTest(unittest.TestCase):
    """`inject_token_budget` 以前根本没被 from_raw 读：面板改多少都是 3500。"""

    MOMENT = datetime(2026, 9, 25, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    def _finish(self, budget: int, sents):
        from types import SimpleNamespace

        from humanoid.prompt_builder import PromptBuilder

        builder = PromptBuilder.__new__(PromptBuilder)
        builder._core = SimpleNamespace(role_id="bot1")
        cfg = HumanoidConfig.from_raw({"inject_token_budget": budget})
        return builder._finish(sents, cfg)

    def test_budget_reaches_the_builder_and_drops_low_salience_first(self):
        long_day = "她今天" + "、".join(f"上午在赶第{i}份方案稿" for i in range(1, 41))
        sents = [
            ("私聊，只有你和TA，现在是15:00", 10.0),
            ("你管TA叫小鱼", 10.0),
            ("她困得不行", 8.0),
            (long_day, 4.0),
        ]
        tight = self._finish(200, sents)
        loose = self._finish(20000, sents)
        self.assertIn(long_day[:6], loose, "预算宽裕时长句本该留下")
        self.assertNotIn(long_day[:6], tight, "预算调小后低显著度的句子没被丢：预算没用上")
        for must_keep in ("私聊", "15:00", "小鱼", "她困得不行"):
            self.assertIn(must_keep, tight, f"必需品被丢了：{tight}")

    def test_smaller_budget_never_grows_the_block(self):
        big = wrecked(self.MOMENT, inject_token_budget=3500)
        small = wrecked(self.MOMENT, inject_token_budget=200)
        self.assertLessEqual(
            len(small.build_injection("42", is_group=False)),
            len(big.build_injection("42", is_group=False)),
        )


class SchemaEffectiveTest(unittest.TestCase):
    """面板上每一项都得能真正改到运行时的配置对象。"""

    def test_every_schema_key_reaches_config(self):
        for key in SCHEMA:
            with self.subTest(key=key):
                probe = probe_value(key)
                expected = getattr(HumanoidConfig.from_raw({key: probe}), key)
                default = getattr(DEFAULTS, key)
                self.assertNotEqual(
                    expected, default,
                    f"面板改了 {key} 也不生效：传进去 {probe!r}，配置里还是默认 {default!r}",
                )

    def test_inject_activity_context_accepts_the_three_documented_modes(self):
        for mode in ("medium", "full", "mood_only"):
            with self.subTest(mode=mode):
                self.assertEqual(
                    HumanoidConfig.from_raw({"inject_activity_context": mode}).inject_activity_context,
                    mode,
                )

    def test_legacy_low_mode_normalizes_to_medium(self):
        """老配置文件里可能还写着 low：它早就是 medium 的同义词，别再让它原样留着。"""
        self.assertEqual(
            HumanoidConfig.from_raw({"inject_activity_context": "low"}).inject_activity_context,
            "medium",
        )

    def test_token_budget_is_clamped_not_dropped(self):
        self.assertEqual(HumanoidConfig.from_raw({"inject_token_budget": 1}).inject_token_budget, 200)
        self.assertEqual(HumanoidConfig.from_raw({"inject_token_budget": 900}).inject_token_budget, 900)


class WeatherHonestyTest(unittest.TestCase):
    """没配天气就不许报天气：编一句「晴朗 ☀️」会让主人以为插件在正常干活。"""

    def _service(self, city: str, api_key: str = "", enabled: bool = True) -> WeatherService:
        store = ScopeStore(Path(tempfile.mkdtemp()) / "state.json")
        store.load("2026-09-25", 28)
        config = HumanoidConfig.from_raw(
            {"timezone_city": city, "weather_api_key": api_key, "weather_enabled": enabled}
        )
        return WeatherService(
            store.scope,
            lambda: config,
            FrozenClock(datetime(2026, 9, 25, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")), city=city),
            None,
            RecordingLogger(),
        )

    def test_missing_api_key_invents_no_weather(self):
        service = self._service("Osaka", "")
        snap = service.snapshot()
        self.assertEqual(snap["weather"], "", f"没配 Key 却报出天气：{snap}")
        self.assertIn("未填 API Key", snap["env"])

    def test_disabled_weather_invents_no_weather(self):
        snap = self._service("Osaka", enabled=False).snapshot()
        self.assertEqual(snap["weather"], "")
        self.assertIn("未开启", snap["env"])

    def test_unresolved_location_invents_no_weather(self):
        snap = self._service("北京", "").snapshot()  # 中文名 OpenWeather 认不了
        self.assertEqual(snap["weather"], "")

    def test_notice_words_are_recognized(self):
        for text in ("当前城市 [x]（未填 API Key）", "天气未开启", "当前城市 [x]（获取中）", "没配天气城市：…"):
            with self.subTest(text=text):
                self.assertTrue(is_notice(text))
        self.assertFalse(is_notice("当前城市 [Osaka] 天气：晴，气温 21℃"))


class SleepSentenceTest(unittest.TestCase):
    """睡着时那句话说两遍主语：「她这会儿她在睡」是句病话，模型会照抄。"""

    def test_no_doubled_subject_when_asleep(self):
        import asyncio

        from humanoid.core_instance import HumanoidCoreInstance

        moment = datetime(2026, 9, 25, 3, 5, tzinfo=ZoneInfo("Asia/Shanghai"))
        core = make_core(moment, use_llm_schedule=False)
        core.clock = FrozenClock(moment)
        # 「她在睡」得有一份此刻写着睡的日程撑着（直写，绕开夜间窗口重切）
        core.schedule._scope.update_self(
            today_date=moment.strftime("%Y-%m-%d"),
            daily_schedule=[{
                "start": "00:00", "end": "24:00", "event": "睡眠休息", "location": "卧室",
                "emotion": "平静", "energy_rate": 0.1,
            }],
        )
        core.soma.data["asleep_since"] = core.soma.now - 3600.0
        text = core.build_injection("42", is_group=False)
        self.assertIn("她这会儿在睡", text, f"睡着没说、或又说两遍主语：{text}")
        self.assertNotIn("她她", text, f"病句：{text}")
        self.assertNotIn("她这会儿她在睡", text, f"主语重复：{text}")


@unittest.skipIf(main_module is None, "缺少 astrbot 桩，跑不了入口层")
class MainSurfaceTest(unittest.IsolatedAsyncioTestCase):
    """给人看的那几句：管理员列表别抛 tuple 原文，天气行别留空尾巴。"""

    async def asyncSetUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        main_module.get_astrbot_data_path = lambda: str(self.tmp)
        self.raw = {
            "timezone_city": "America/Los_Angeles",
            "admin_qq": ["10001", "10002"],
            "state_flush_interval_seconds": 1,
        }
        self.star = main_module.HumanoidCore(FakeContext(), self.raw)
        await self.star.initialize()

    async def asyncTearDown(self) -> None:
        await self.star.terminate()

    async def test_settings_summary_lists_admins_readably(self):
        core = self.star._core(FakeEvent("/拟人设置"))
        text = self.star._settings_summary(core)
        self.assertIn("10001、10002", text, text)
        self.assertNotIn("('10001',", text, "把 tuple 的 repr 直接端给用户了")

    async def test_status_tells_why_there_is_no_weather(self):
        out = (await collect(self.star.cmd_status(FakeEvent("/你的状态"))))[0]
        self.assertNotIn("晴朗", out, "没配 Key 也报晴朗，这是编的")
        self.assertIn("未填 API Key", out, "该说清为什么没有天气，而不是留个空行")
        self.assertNotIn("- 天气：\n", out, "留一行空的「天气：」最没用")

    async def test_contract_does_not_carry_setup_manual(self):
        core = self.star._core(FakeEvent("/x"))
        contract = build_contract(core)
        self.assertEqual(contract["weather"], "", contract["weather"])

    async def test_batch_affection_counts_malformed_entries(self):
        out = (await collect(
            self.star.cmd_batch_affection(FakeEvent("/批量好感度 10001:70,10002:abc,10003:150"))
        ))[0]
        self.assertIn("已批量设置 1 个", out)
        self.assertIn("跳过 2 个", out, f"abc 与 150 两条都该报出来：{out}")

    def test_parse_affection_batch_returns_bad_count(self):
        engine = HumanoidEngine.__new__(HumanoidEngine)
        pairs, bad = engine.parse_affection_batch("10001:70,10002:abc,10003:150 没冒号的一条")
        self.assertEqual([uid for uid, _ in pairs], ["10001", "10003"])
        self.assertEqual(bad, 2)


class SwitchEffectsTest(unittest.TestCase):
    """开关得真的改行为：这两项以前是「值存进去了，表现一点不变」。"""

    NIGHT = datetime(2026, 9, 25, 2, 30, tzinfo=ZoneInfo("Asia/Shanghai"))

    def _sleeping(self, **conf):
        moment = self.NIGHT
        core = make_core(moment, night_start_hour=22, night_end_hour=8,
                         schedule_follow_night_window=True, **conf)
        core.clock = FrozenClock(moment)
        # 整夜都在睡：soma 会把她算成睡着
        core.schedule._scope.update_self(
            today_date=moment.strftime("%Y-%m-%d"),
            daily_schedule=[{
                "start": "22:00", "end": "08:00", "event": "睡眠休息", "location": "卧室",
                "emotion": "平静", "energy_rate": 0.1,
            }],
        )
        core.soma.data["asleep_since"] = core.soma.now - 3600.0
        return core

    def test_show_sleep_window_also_hides_being_asleep(self):
        on = self._sleeping(show_sleep_window=True).build_injection("42", is_group=False)
        off = self._sleeping(show_sleep_window=False).build_injection("42", is_group=False)
        self.assertIn("在睡", on, on)
        self.assertNotIn("在睡", off, "关掉睡眠提示后，换句话仍在说她睡着")
        self.assertNotIn("睡眠时段", off, off)

    def test_social_energy_off_hides_the_line(self):
        off = make_core(self.NIGHT, social_energy_enabled=False)
        on = make_core(self.NIGHT, social_energy_enabled=True)
        off_lines = "\n".join(off.status_lines("42"))
        on_lines = "\n".join(on.status_lines("42"))
        self.assertIn("社交能量", on_lines, on_lines)
        self.assertNotIn("社交能量", off_lines, "关掉系统还在报这一行数值")

    def test_soma_off_removes_body_sensations(self):
        """关掉躯体层，注入里就不要再出现「她很困」「她有些饿」。"""

        noon = datetime(2026, 9, 25, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        on = wrecked(noon, soma_enabled=True).build_injection("42", is_group=False)
        off = wrecked(noon, soma_enabled=False).build_injection("42", is_group=False)
        self.assertIn("困", on, on)
        self.assertNotIn("困", off, off)

    def test_last_interaction_mode_changes_whether_the_quote_appears(self):
        """with_last_msg 会把 TA 离开前那句原话递上去；simple 只说隔了多久。"""

        noon = datetime(2026, 9, 25, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        quote = "我明天上午面试你能不能陪我说说话"
        cores = {}
        for mode in ("simple", "with_last_msg"):
            core = wrecked(noon, last_interaction_mode=mode)
            core.behavior.add_event("42", {
                "type": "long_gap", "timestamp": core.now_epoch(), "importance": 1.0,
                "data": {"gap_bucket": "long", "gap_seconds": 5400.0, "previous_message": quote},
            })
            cores[mode] = core.build_injection("42", is_group=False, text="我回来了")
        self.assertNotEqual(cores["simple"], cores["with_last_msg"], "两档给的话一模一样")
        self.assertIn(quote[:10], cores["with_last_msg"], cores["with_last_msg"])
        self.assertNotIn(quote[:10], cores["simple"], cores["simple"])

    # night_mode_enabled 的开关效果走 `Clock.is_night()`（关掉直接返回 False），
    # 开/关两档由 tests/test_expression.NightFactsTest 覆盖。

    def test_last_interaction_threshold_below_the_segment_floor_is_honest(self):
        """「隔了多久」的句子来自分段表，表从 5 分钟起：阈值配得更小也只能按 5 分钟生效。"""

        from humanoid.services.behavior import GAP_SEGMENTS

        floor = min(low for low, _high, *_rest in GAP_SEGMENTS)
        self.assertEqual(
            floor, 300.0,
            "间隔分段表的下限变了：schema 里「低于 5 分钟按 5 分钟算」的说明要同步",
        )

    def test_custom_holiday_reaches_the_clock(self):
        """holidays 里写她自己的日子，她的钟就得认得这天（/时间 不报节日，节日进的是上下文）。"""

        from humanoid.clock import Clock
        from humanoid.data.holidays import resolve_holiday

        cfg = HumanoidConfig.from_raw(
            {"holidays": [{"date": "2026-09-25", "name": "我们的纪念日"}]}
        )
        moment = datetime(2026, 9, 25, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(resolve_holiday(moment, cfg.holidays), "我们的纪念日")
        clock = Clock(lambda: cfg)
        self.assertEqual(clock.holiday(moment), "我们的纪念日")
        self.assertEqual(resolve_holiday(moment, []), "")


class NicknameGateTest(unittest.TestCase):
    """CHANGELOG 宣称「测试」这类占位词不认：那「测试号」也不能认。"""

    def test_placeholder_derivatives_are_rejected(self):
        for name in ("测试", "测试号", "测试账号", "管理员", "已注销用户", "游客"):
            with self.subTest(name=name):
                self.assertEqual(validate_auto_nickname(name), "")

    def test_ordinary_names_still_pass(self):
        for name in ("小明", "阿哲", "老王", "欧阳娜娜", "Lily", "李雷-韩梅"):
            with self.subTest(name=name):
                self.assertEqual(validate_auto_nickname(name), name)
