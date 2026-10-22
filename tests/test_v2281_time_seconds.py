"""v2.28.1：注入里的绝对时间精确到秒，但模型不该把它念出来。

用户原话：「把时间每次注入上下文的变成 精确到秒的 但是记住不要让模型会念出来
把这个时间注入让模型知道是几点但是可以知道 不要让他每次都念出来」。

## 为什么是秒

「此刻的她」那段是**每轮重发的临时块**（`mark_as_temp()`，不落会话历史），
两条消息之间常常只隔几十秒。只给「15:20」时，她分不清对方是 15:20:05 还是
15:20:55 说的——「刚说完」和「快半小时了」在她的时间里是同一时刻。

## 为什么不用担心她念出来

「要不要提时间」这件事**不由这一行管**，而由 system_prompt 的框架句管：
`build_framing()` 里已经写着「它们是背景，不是要拿出来讲的内容；对方没问起时，
它们只影响你怎么说话」。秒只是把钟点的刻度改细了，没有把任何东西提升成
「要讲的内容」——就像给人一块表，不是命令她逢人就报时。

真正会被念出来的是另一类词：「上午/下午/晚上」这种时段词（无论几点都回一句
「下午好呀」）。那条禁令保持不变，本文件也盯着它。
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from humanoid.config import HumanoidConfig
from humanoid.core_instance import HumanoidCoreInstance
from humanoid.prompt_builder import FRAMING_TEXT
from humanoid.state import StateStore

from .fakes import FakeContext, FrozenClock, RecordingLogger

TZ = ZoneInfo("Asia/Shanghai")
# 秒取 33：非零、非整十，免得测试被「写死的 :00」骗过去。
MOMENT = datetime(2026, 8, 22, 15, 20, 33, tzinfo=TZ)


def core_with(moment: datetime = MOMENT, mode: str = "low", **conf):
    """一台钟被冻在 `moment` 的 Core。

    时钟是构造完之后换的（与 test_v216_facts 同一套写法）：构造函数不收 clock。
    """
    store = StateStore(Path(tempfile.mkdtemp()) / "state.json", lambda: 0.01)
    store.load(moment.strftime("%Y-%m-%d"), 28)
    config = HumanoidConfig.from_raw(
        {"timezone_city": "北京", "inject_activity_context": mode, **conf}
    )
    core = HumanoidCoreInstance(
        role_id="bot1",
        state_store=store,
        config_provider=lambda: config,
        logger=RecordingLogger(),
        stop_event=asyncio.Event(),
        resolver=FakeContext(),
        gateway=None,
    )
    core.clock = FrozenClock(moment)
    return core


def world_text(core) -> str:
    """`background_note()` 本身就返回拼好的一段（内部已按「；」 join），直接拿来断言。"""
    return core.prompt_builder.background_note()


class TimeToSecondsTest(unittest.TestCase):
    """注入里的绝对钟点带秒。"""

    def test_clock_line_has_seconds(self) -> None:
        text = world_text(core_with())
        self.assertIn("现在是15:20:33", text, f"注入里的钟点没有秒：{text}")

    def test_seconds_track_the_clock(self) -> None:
        """换个非零秒，确认不是把 :00 或 :33 写死的。"""
        text = world_text(core_with(datetime(2026, 8, 22, 9, 7, 41, tzinfo=TZ)))
        self.assertIn("现在是09:07:41", text, f"秒没有跟着钟走：{text}")

    def test_minute_precision_still_holds(self) -> None:
        """「15:20」这个前缀仍然成立：只加秒，不改钟点本身。

        既有测试（test_v2151_freedom / test_v216_facts）断言的是「15:20」，
        这条钉住它们不会因为这档改动变红。
        """
        text = world_text(core_with())
        self.assertIn("15:20", text)

    def test_seconds_reach_the_state_note(self) -> None:
        """这段要真的进上下文，不只是 background_note() 里有。

        走向是：background_note() -> main._state_note() -> build_state_note() ->
        一个 mark_as_temp() 的临时内容块。它**不进** build_injection()（那是事实块，
        时间在那儿会被当成要回应的内容）——所以这里断言的是 state_note 这条路。
        """
        from humanoid.prompt_builder import build_state_note

        core = core_with()
        note = build_state_note(world_line=core.prompt_builder.background_note())
        self.assertIn("15:20:33", note)

    def test_seconds_stay_out_of_the_fact_block(self) -> None:
        """时间不许溜进事实块：在那儿它会被当成「要回应的内容」而不是背景。"""
        core = core_with()
        core.mood.set_nickname("42", "小鱼")
        text = core.build_injection("42", is_group=False)
        self.assertNotIn("15:20:33", text)
        self.assertNotIn("15:20", text)

    def test_date_and_weekday_unchanged(self) -> None:
        """只改钟点，日期与周几不动。"""
        text = world_text(core_with())
        self.assertIn("今天是2026-08-22", text)
        self.assertIn("今天周六", text)


class NotRecitedTest(unittest.TestCase):
    """秒不许被变成「要念出来」的内容。"""

    def test_framing_still_calls_it_background(self) -> None:
        """授权口径不变：框架句仍然把它定为背景。"""
        self.assertIn("背景", FRAMING_TEXT)
        self.assertIn("不是要拿出来讲的内容", FRAMING_TEXT)
        self.assertIn("对方没问起", FRAMING_TEXT)

    def test_framing_mentions_no_clock_at_all(self) -> None:
        """框架句里不许出现任何跟「报时」有关的字眼。

        test_v2151_freedom 已全局盯着「不要/必须/别/控制」这类词；这里补一条更具体的：
        不许因为加了秒就多出一句「请报出准确时间」之类的话。
        """
        for phrase in ("报出", "准确时间", "精确到", "念出", "说出时间", "告知时间",
                       "几点", "钟点", "秒"):
            self.assertNotIn(phrase, FRAMING_TEXT,
                             f"框架句出现了报时相关措辞：{phrase}")

    def test_time_line_is_a_bare_fact(self) -> None:
        """时间那一行只是事实，不带任何语气词或指令。"""
        text = world_text(core_with())
        line = [l for l in text.split("；") if "现在" in l]
        self.assertTrue(line, f"没找到时间行：{text}")
        body = line[0]
        for word in ("应该", "必须", "请", "记得", "别忘了", "要告诉", "可以提"):
            self.assertNotIn(word, body, f"时间行里出现了指令：{word}")

    def test_no_period_words(self) -> None:
        """上午/下午/晚上这类时段词仍然不出现——它们才会被当成打招呼由头。"""
        for hour in (7, 12, 18, 23):
            core = core_with(datetime(2026, 8, 22, hour, 5, 9, tzinfo=TZ))
            text = world_text(core)
            for word in ("上午", "下午", "晚上", "凌晨", "清晨", "傍晚", "中午"):
                self.assertNotIn(word, text, f"{hour} 点出现了时段词：{word}")

    def test_no_technical_time_format(self) -> None:
        """不许退化成 UTC 偏移或 ISO 形式。"""
        text = world_text(core_with())
        for bad in ("UTC+", "UTC-", "+08:00", "T15:", "2026-08-22T"):
            self.assertNotIn(bad, text, f"时间行出现技术形式：{bad}")


if __name__ == "__main__":
    unittest.main()