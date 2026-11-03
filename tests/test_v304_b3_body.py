"""v3.0.4 / B3（R1 单一真源）身体与情绪侧回归（task-32）。

覆盖四条审核发现：
* F4  `soma.slot_at` 跨午夜段：22:00→07:00 在凌晨要判成「在睡」，且与
  `slots.sleep_window_minutes` 口径一致（同一份日程，两处不再各算一遍）。
* F5  `energy._compute_delta` 跨午夜段：以前 `if slot_end <= slot_start: continue`
  把整段丢掉，于是「23:00→02:00 熬夜 -0.5/分钟」落进 gap 只给恢复——熬夜回血。
* F10 `emotion._memory_note` 读取侧有效期：7 天 TTL 以前只写在写入侧剪枝里，
  于是 8 天前的记仇照旧说「前几天那件事我还记着」。
* F13 精力档位只有一份：面板 mood_tag 与注入体感句必须同源（75 不该是「精力充沛」）。
* #45 关系阶段峰值 `turn_count_peak`：清理/衰减把 turn_count 归零时阶段不倒退。
* #34 另一半：`mood_data_retention_days=0` 时纯空壳 `{}` 仍要被清（旧实现在
  「整条删除」循环前就 return 了）。
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from humanoid.config import HumanoidConfig
from humanoid.emotion import EmotionLayer
from humanoid.services.energy import ENERGY_WORDS, EnergyService, describe_energy
from humanoid.services.mood import MoodService
from humanoid.services.soma import SomaService

from .fakes import ScopeStore
from .test_services import FrozenClock, build_store, cfg

TZ = timezone(timedelta(hours=8))


def _store(conf: HumanoidConfig) -> ScopeStore:
    store = ScopeStore(Path(tempfile.mkdtemp()) / "s.json")
    store.load("2026-11-03", conf.cycle_length)
    return store


class _Clock:
    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def now(self) -> datetime:
        return self.moment

    def today_str(self) -> str:
        return self.moment.strftime("%Y-%m-%d")

    def timestamp(self) -> float:
        return self.moment.timestamp()


# ──────────────────────────────────────────────────────────────────────
# F4：slot_at 跨午夜
# ──────────────────────────────────────────────────────────────────────


class CrossMidnightSlotAt(unittest.TestCase):
    def build(self, slots):
        conf = cfg()
        store = _store(conf)
        clock = _Clock(datetime(2026, 11, 3, 12, 0, tzinfo=TZ))
        service = SomaService(
            store.scope, lambda: conf, clock, lambda: slots, lambda: {}, lambda: clock.timestamp()
        )
        return service, conf

    def test_after_midnight_is_inside_the_sleep_slot(self):
        """22:00→07:00 的睡段，凌晨 01:00 必须判成在睡。

        旧写法把跨午夜段折成 `end = 24:00`（e=1440），只有 22:00-24:00 落在里面，
        01:00 落到空档 → `slot_at` 返回 {}、`is_sleep_time` 返回 False。同一份日程，
        `slots.sleep_window_minutes` 却把这段算成 540 分钟。
        """
        service, _ = self.build(
            [{"start": "22:00", "end": "07:00", "event": "睡眠", "energy_rate": 0.1}]
        )
        slot = service.slot_at(datetime(2026, 11, 3, 1, 0, tzinfo=TZ))
        self.assertEqual(slot.get("event"), "睡眠", "凌晨一点不在睡段里")
        self.assertTrue(service.is_sleep_time(datetime(2026, 11, 3, 1, 0, tzinfo=TZ)))

    def test_window_minutes_agree_with_slot_at(self):
        """`sleep_window_minutes` 与 `slot_at` 必须对同一份日程给出同一个答案。"""
        from humanoid.slots import sleep_window_minutes

        slots = [{"start": "22:00", "end": "07:00", "event": "睡眠", "energy_rate": 0.1}]
        service, conf = self.build(slots)
        # 跨午夜时返回 (起始, 结束) 且 end < start：22:00 → 07:00
        window = sleep_window_minutes(slots)
        self.assertEqual(window, (22 * 60, 7 * 60))
        self.assertEqual((window[1] - window[0]) % 1440, 540)
        for hour in (22, 23, 0, 3, 6):
            moment = datetime(2026, 11, 3, hour, 30, tzinfo=TZ)
            self.assertEqual(
                service.slot_at(moment).get("event"), "睡眠", f"{hour}:30 与 540 分钟窗口不一致"
            )
        for hour in (7, 12, 20):
            moment = datetime(2026, 11, 3, hour, 30, tzinfo=TZ)
            self.assertEqual(service.slot_at(moment), {}, f"{hour}:30 不该落在睡段里")


# ──────────────────────────────────────────────────────────────────────
# F5：跨午夜段的精力消耗
# ──────────────────────────────────────────────────────────────────────


class CrossMidnightEnergy(unittest.TestCase):
    def build(self, slots, moment):
        conf = cfg()
        store = build_store(conf)
        clock = FrozenClock(moment)
        service = EnergyService(store.scope, lambda: conf, clock, lambda: slots)
        return service, store

    def _advance(self, slots):
        service, store = self.build(slots, datetime(2026, 11, 4, 1, 0, tzinfo=TZ))
        store.data["energy"] = 50.0
        store.data["last_update"] = "2026-11-03 23:30:00"
        return service.advance()

    def test_late_night_crossing_slot_consumes(self):
        """23:00→02:00、-0.5/分钟的熬夜段，跨过午夜后仍要扣精力。

        旧写法在 `_compute_delta` 里 `if slot_end <= slot_start: continue`：整段被丢掉，
        23:30→01:00 落进「没有日程」的 gap，只拿到自然恢复——熬夜反而回血。
        """
        late = [{"start": "23:00", "end": "02:00", "event": "熬夜", "energy_rate": -0.5}]
        energy = self._advance(late)
        self.assertLess(energy, 49.0, f"熬夜 90 分钟精力没掉（{energy}）")

    def test_crossing_slot_equals_split_slots(self):
        """跨午夜段与「拆成两段」是同一件事，数值必须一样。"""
        crossing = [{"start": "23:00", "end": "02:00", "event": "熬夜", "energy_rate": -0.5}]
        split = [
            {"start": "23:00", "end": "24:00", "event": "熬夜", "energy_rate": -0.5},
            {"start": "00:00", "end": "02:00", "event": "熬夜", "energy_rate": -0.5},
        ]
        self.assertAlmostEqual(self._advance(crossing), self._advance(split), places=4)


# ──────────────────────────────────────────────────────────────────────
# F10：情绪记忆读取侧有效期
# ──────────────────────────────────────────────────────────────────────


class _MoodStub:
    def __init__(self, record):
        self._record = record

    def profile(self, user_id):
        return self._record


class _CoreStub:
    def __init__(self, record, now):
        self.mood = _MoodStub(record)
        self._now = now

    def now_epoch(self):
        return self._now


class EmotionMemoryTtl(unittest.TestCase):
    NOW = 1_800_000_000.0

    def _note(self, days_ago: float, kind: str = "grudge", level: str = "heavy"):
        record = {
            "emotion_memory": [
                {"t": self.NOW - days_ago * 86400.0, "k": kind, "lvl": level}
            ]
        }
        layer = EmotionLayer(_CoreStub(record, self.NOW))
        return layer._memory_note("1", 0)

    def test_fresh_memory_still_speaks(self):
        weight, line = self._note(3)
        self.assertGreater(weight, 0.0)
        self.assertTrue(line)

    def test_expired_memory_is_silent(self):
        """8 天前的记仇不该再说「前几天那件事我还记着」。"""
        weight, line = self._note(8)
        self.assertEqual((weight, line), (0.0, ""))

    def test_boundary_seven_days(self):
        self.assertGreater(self._note(6.9)[0], 0.0)
        self.assertEqual(self._note(7.1)[0], 0.0)

    def test_bad_timestamp_is_silent(self):
        record = {"emotion_memory": [{"t": "坏了", "k": "grudge", "lvl": "heavy"}]}
        layer = EmotionLayer(_CoreStub(record, self.NOW))
        self.assertEqual(layer._memory_note("1", 0), (0.0, ""))


# ──────────────────────────────────────────────────────────────────────
# F13：精力档位单一真源
# ──────────────────────────────────────────────────────────────────────


class EnergyWordSingleSource(unittest.TestCase):
    def test_mood_tag_uses_the_same_ladder_as_injection(self):
        from humanoid.data.mood_map import generate_mood_tag
        from humanoid.wording import band_index

        ladder = sorted(ENERGY_WORDS, key=lambda item: item[0])
        for energy in (95.0, 85.0, 75.0, 55.0, 30.0, 10.0):
            tag = generate_mood_tag(50.0, 20.0, 10.0, energy, seed=("b", "2026-11-03"))
            words = ladder[band_index(energy, ENERGY_WORDS)][1]
            self.assertTrue(
                any(word in tag for word in words),
                f"精力 {energy} 的标签「{tag}」不在 ENERGY_WORDS 该档 {words} 里",
            )

    def test_75_is_not_the_top_band(self):
        """75 在注入里是「状态挺好」，面板不能说是「精力充沛」。"""
        from humanoid.data.mood_map import generate_mood_tag
        from humanoid.wording import band_index

        ladder = sorted(ENERGY_WORDS, key=lambda item: item[0])
        top = ladder[-1][1]
        words = ladder[band_index(75.0, ENERGY_WORDS)][1]
        tag = generate_mood_tag(50.0, 20.0, 10.0, 75.0, seed=("b", "2026-11-03"))
        self.assertFalse(any(word in tag for word in top), tag)
        self.assertIn(describe_energy(75.0), words, "注入句用的词不在 75 这一档里")
        self.assertTrue(any(word in tag for word in words), f"面板与注入不同档：{tag}")


# ──────────────────────────────────────────────────────────────────────
# #45：关系阶段峰值
# ──────────────────────────────────────────────────────────────────────


def _svc(**over):
    conf = HumanoidConfig.from_raw(
        {"timezone_city": "北京", "mood_use_llm_for_delta": False, **over}
    )
    store = _store(conf)
    clock = _Clock(datetime(2026, 11, 3, 9, 0, tzinfo=TZ))
    svc = MoodService(
        store.scope, lambda: conf, clock,
        time_source=lambda: clock.now().timestamp(),
        logger=None,
    )
    return svc, store, clock


class TurnCountPeak(unittest.TestCase):
    def test_commit_raises_peak(self):
        svc, store, _ = _svc()
        record = svc.profile("1")
        for _ in range(3):
            asyncio.run(svc.update_from_message("1", "你好棒", 80.0, 8))
        record = svc.profile("1")
        self.assertEqual(int(record["turn_count"]), 3)
        self.assertGreaterEqual(int(record["turn_count_peak"]), 3)

    def test_old_record_backfills_peak_from_turn_count(self):
        """老档没有 turn_count_peak：按 turn_count 兜底，不能写 0。"""
        svc, store, _ = _svc()
        user_state = store.scope.user_state("1")
        user_state["mood"] = {
            "affection": 60.0, "libido": 30.0, "aggression": 20.0,
            "base_affection": 60.0, "base_libido": 30.0, "base_aggression": 20.0,
            "turn_count": 120, "first_met": 1_700_000_000.0,
            "last_interaction": 1_700_000_000.0, "last_decay": 1_700_000_000.0,
        }
        record = svc.profile("1")
        self.assertEqual(int(record["turn_count_peak"]), 120)

    def test_peak_survives_decay_zeroing_turn_count(self):
        svc, store, clock = _svc()
        user_state = store.scope.user_state("1")
        user_state["mood"] = {
            "affection": 60.0, "libido": 30.0, "aggression": 20.0,
            "base_affection": 60.0, "base_libido": 30.0, "base_aggression": 20.0,
            "turn_count": 0, "turn_count_peak": 200,
            "first_met": clock.now().timestamp(),
            "last_interaction": clock.now().timestamp(),
            "last_decay": clock.now().timestamp(),
        }
        record = svc.profile("1")
        self.assertEqual(int(record["turn_count_peak"]), 200, "峰值被衰减一起抹掉了")

    def test_reset_clears_peak(self):
        svc, store, _ = _svc()
        record = svc.profile("1")
        record["turn_count"] = 50
        record["turn_count_peak"] = 50
        svc.reset("1")
        record = svc.profile("1")
        self.assertEqual(int(record["turn_count_peak"]), 0)


# ──────────────────────────────────────────────────────────────────────
# #34 的另一半：retention=0 时纯空壳仍要清
# ──────────────────────────────────────────────────────────────────────


class RetentionZeroStillPrunesShells(unittest.TestCase):
    """`mood_data_retention_days=0`（永不清理情绪字段）不等于「整段什么都不做」。

    旧实现 `if days <= 0: ... return 0` 在「整条删除」循环之前早退，于是群聊里
    每个说过话的陌生人留下的纯空壳 `{}` 永远清不掉，state.json 随群规模涨。
    现在字段级清理被 `do_field_cleanup` 单独关掉，空壳清理照跑。
    """

    def test_empty_shell_is_pruned_even_when_retention_is_zero(self):
        svc, store, _ = _svc(mood_data_retention_days=0)
        store.scope.user_state("ghost")  # 只有 {} 的陌生人
        svc.profile("42")               # 有情绪档案的人
        store.scope.set_user("43", "nickname", "阿哲")
        pruned = svc.prune_expired()
        self.assertEqual(pruned, 1, "retention=0 时纯空壳仍应被清掉")
        self.assertNotIn("ghost", store.scope.all_user_ids())
        self.assertIn("42", store.scope.all_user_ids(), "真档案不该被连带删")
        self.assertIn("43", store.scope.all_user_ids())

    def test_zero_retention_keeps_the_emotion_record(self):
        """字段级清理被关掉：过期用户只丢空壳，情绪档案原样留着。"""
        svc, store, clock = _svc(mood_data_retention_days=0)
        record = svc.profile("7")
        record["last_interaction"] = clock.now().timestamp() - 30 * 86400.0
        store.scope.set_user("7", "last_interaction", clock.now().timestamp() - 30 * 86400.0)
        svc.prune_expired()
        self.assertIsInstance(store.scope.peek_user("7", "mood"), dict, "retention=0 却清了档案")


if __name__ == "__main__":
    unittest.main()
