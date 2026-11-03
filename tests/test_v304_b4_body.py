"""v3.0.4 B4（#131）：mood.py 的四道日闸共用一把日期尺。

反馈的根因是「同一个今天，两把尺子」：`neg_day`（同一天同类递减）自己读
`clock.today_str()`，而 `loss_day`（每日净损封顶）走 `_day_key()`（账本时间
`_time()` + 角色时区）。跨午夜那一瞬、时钟被换掉、角色换城市时，两者给出不同的
日期字符串：递减计数记在 A 日、每日亏损记在 B 日，各自按自己的「今天」翻篇。

本文件钉住三件事：
1. `neg_day` 与 `loss_day` 的 date 必须永远相等，且一起在午夜翻篇；
2. 判不出日期时不写空日期（空串永远等于空串，计数一辈子不翻篇）；
3. `log_day` 与 mood_tag 的种子日期也走同一把尺子。
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from humanoid.config import HumanoidConfig
from humanoid.data.mood_map import generate_mood_tag
from humanoid.services.mood import Delta, MoodService

from .fakes import RecordingLogger, ScopeStore

TZ = timezone(timedelta(hours=8))


class _LegacyClock:
    """只有 now()/today_str() 的旧式替身钟（仓库里大多数测试用的就是这种）。"""

    def __init__(self, dt):
        self.dt = dt

    def now(self):
        return self.dt

    def today_str(self) -> str:
        return self.dt.strftime("%Y-%m-%d")

    def advance(self, **kwargs) -> None:
        self.dt = self.dt + timedelta(**kwargs)


class _DayKeyClock:
    """实现了 day_key() 的替身钟：`_day_key()` 的首选路径。"""

    def __init__(self, key: str) -> None:
        self.key = key
        self.calls: list = []

    def day_key(self, ts=None) -> str:
        self.calls.append(ts)
        return self.key

    def now(self):
        return datetime(2026, 8, 22, 9, 0, tzinfo=TZ)


def _svc(clock, time_source=None, **over):
    conf = HumanoidConfig.from_raw(
        {"timezone_city": "北京", "mood_use_llm_for_delta": False, **over}
    )
    store = ScopeStore(Path(tempfile.mkdtemp()) / "s.json")
    store.load("2026-08-22", conf.cycle_length)
    if time_source is None:
        time_source = lambda: clock.now().timestamp()  # noqa: E731
    svc = MoodService(
        store.scope, lambda: conf, clock, time_source=time_source,
        logger=RecordingLogger(),
    )
    return svc


def _record() -> dict:
    return {
        "affection": 60.0,
        "libido": 20.0,
        "aggression": 20.0,
        "base_affection": 60.0,
    }


class OneDayRuler(unittest.TestCase):
    """neg_day 与 loss_day 必须永远说同一天。"""

    def test_both_day_gates_carry_the_same_date(self):
        clock = _LegacyClock(datetime(2026, 8, 22, 23, 59, tzinfo=TZ))
        svc = _svc(clock)
        record = _record()
        svc._throttle_negative(Delta(-0.5, 0.0, 0.0), record, "neg:傻", severe=True)
        neg = record.get("neg_day") or {}
        loss = record.get("loss_day") or {}
        self.assertEqual(neg.get("date"), "2026-08-22")
        self.assertEqual(
            loss.get("date"), neg.get("date"),
            f"同一次扣分写进了两天：递减={neg.get('date')!r} 亏损={loss.get('date')!r}",
        )

    def test_both_gates_roll_over_together_at_midnight(self):
        clock = _LegacyClock(datetime(2026, 8, 22, 23, 59, tzinfo=TZ))
        svc = _svc(clock)
        record = _record()
        svc._throttle_negative(Delta(-0.5, 0.0, 0.0), record, "neg:傻", severe=True)
        self.assertEqual(record["neg_day"]["date"], "2026-08-22")
        clock.advance(minutes=2)  # 00:01 次日
        svc._throttle_negative(Delta(-0.5, 0.0, 0.0), record, "neg:傻", severe=True)
        self.assertEqual(record["neg_day"]["date"], "2026-08-23")
        self.assertEqual(record["loss_day"]["date"], "2026-08-23")
        self.assertEqual(
            record["neg_day"]["counts"].get("neg:傻"), 1,
            "跨日了同类递减计数没从 1 重新数",
        )

    def test_repeat_decay_still_works_inside_one_day(self):
        clock = _LegacyClock(datetime(2026, 8, 22, 9, 0, tzinfo=TZ))
        svc = _svc(clock)
        record = _record()
        first = svc._throttle_negative(Delta(-0.5, 0.0, 0.0), record, "neg:傻", severe=True)
        second = svc._throttle_negative(Delta(-0.5, 0.0, 0.0), record, "neg:傻", severe=True)
        # 递减 = 同一句话越说越不值钱（惩罚幅度变小），不是扣得更多。
        self.assertGreater(second.affection, first.affection, "同一天第二次该递减")
        self.assertEqual(record["neg_day"]["counts"].get("neg:傻"), 2)

    def test_the_clock_day_key_is_the_preferred_ruler(self):
        """真 Clock / 实现了 day_key() 的替身钟：直接用它的日键。"""
        clock = _DayKeyClock("2026-01-02")
        svc = _svc(clock)
        record = _record()
        svc._throttle_negative(Delta(-0.5, 0.0, 0.0), record, "neg:笨", severe=True)
        self.assertEqual(record["neg_day"]["date"], "2026-01-02")
        self.assertEqual(record["loss_day"]["date"], "2026-01-02")
        self.assertTrue(clock.calls, "没走 clock.day_key()，日闸又自己算了一遍日期")


class NoEmptyDate(unittest.TestCase):
    """判不出「今天」时不许写空日期：空串永远等于空串，计数永不翻篇。"""

    def test_no_clock_never_writes_an_empty_day(self):
        svc = _svc(None, time_source=lambda: None)
        record = _record()
        svc._throttle_negative(Delta(-0.5, 0.0, 0.0), record, "neg:傻", severe=True)
        self.assertNotIn("neg_day", record, "空日期被写进了 neg_day：递减永久停在 ×0.2")
        self.assertNotIn("loss_day", record)
        svc._log_event("1", record, Delta(4.0, 0.0, 0.0))
        self.assertNotIn("log_day", record, "空日期被写进了 log_day：日志累加器不再翻篇")


class LogDayAndTag(unittest.TestCase):
    """log_day 与 mood_tag 种子也走同一把尺子。"""

    def test_log_day_rolls_over_with_the_same_ruler(self):
        clock = _LegacyClock(datetime(2026, 8, 22, 23, 59, tzinfo=TZ))
        svc = _svc(clock)
        svc.profile("1")
        record = svc.profile("1")
        svc._log_event("1", record, Delta(4.0, 0.0, 0.0))
        self.assertEqual(record["log_day"]["date"], "2026-08-22")
        clock.advance(minutes=2)
        svc._log_event("1", record, Delta(4.0, 0.0, 0.0))
        self.assertEqual(
            record["log_day"]["date"], "2026-08-23",
            "跨日了日志累加器还记在前一天：两天的事件被折进同一条「今日」",
        )

    def test_mood_tag_seed_uses_the_day_key(self):
        clock = _LegacyClock(datetime(2026, 8, 22, 9, 0, tzinfo=TZ))
        svc = _svc(clock)
        record = svc.profile("1")
        svc._day_key = lambda: "2026-03-04"  # 只钉种子来源，不依赖真实时钟
        svc._refresh_tag("1", record)
        expected = generate_mood_tag(
            float(record["affection"]),
            float(record["libido"]),
            float(record["aggression"]),
            float(svc._scope.get_self("energy", 80.0)),
            seed=(svc._scope.role_id, "2026-03-04"),
        )
        self.assertEqual(svc._scope.peek_user("1", "mood_tag"), expected)
