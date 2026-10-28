"""情绪基线自愈与亲近欲涨幅（v2.28.14）。

反馈一：「好感 100/100，但攻击性 37.5/50 从来不掉，亲近欲加得难，ccb 每次差一点」。
根因有两层：

1. 老档案的 base_libido / base_aggression 是**当时的轴值**而不是面板初始值
   （线上见到 base_libido=41.8、base_aggression=38.1，甚至 50）。base 是「落点」，
   被钉高之后攻击性掉不回正常水平、亲近欲的「比基线涨 4」永远差一点。
   这里把「没有打过标记的档案按当前面板值重锚一次」钉住。
2. 亲近欲一天只有 4.5 点额度、单句 1.2，攒一次门槛要四天。v2.28.14 抬到
   单句 1.5 / 每日 6，ccb 的「可达上限」同步抬到 6。

只重锚 libido / aggression：好感的 base 是棘轮抬起来的关系下限，不是初始值。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from humanoid import ccb
from humanoid.config import HumanoidConfig
from humanoid.services import mood as M
from humanoid.services.mood import MoodService

from .fakes import ScopeStore

FEMALE = "你是小满，24岁。她说话很短，喜欢安静。"
UID = "100000003"

# 线上那条真实的旧档案（灰凝）：好感被 override 钉在 100，两条 base 是旧版轴值。
LEGACY = {
    "affection": 100.0,
    "libido": 43.9,
    "aggression": 37.5,
    "base_affection": 100.0,
    "base_libido": 41.8,
    "base_aggression": 38.1,
    "last_interaction": 999_000.0,
    "last_decay": 999_000.0,
    "turn_count": 1357,
    "messages_since_llm": 0,
    "first_met": 1.0,
}


def cfg(**overrides) -> HumanoidConfig:
    return HumanoidConfig.from_raw({"timezone_city": "北京", **overrides})


class BaseSyncTest(unittest.TestCase):
    def build(self, record: dict | None = None, **over):
        conf = cfg(**over)
        store = ScopeStore(Path(tempfile.mkdtemp()) / "s.json")
        store.load("2026-01-01", conf.cycle_length)
        if record is not None:
            store.scope.set_user(UID, "mood", dict(record))
        svc = MoodService(store.scope, lambda: conf, None, time_source=lambda: 1_000_000.0)
        return svc, store, conf

    def test_legacy_bases_are_reanchored_to_panel_initials(self):
        svc, _, conf = self.build(dict(LEGACY))
        record = svc.profile(UID)
        self.assertEqual(record["base_libido"], float(conf.mood_initial_libido))
        self.assertEqual(record["base_aggression"], float(conf.mood_initial_aggression))
        self.assertEqual(record["base_sync_v"], M.BASE_SYNC_VERSION)
        # 好感基线是棘轮抬出来的关系下限，不跟着重锚
        self.assertEqual(record["base_affection"], 100.0)
        # 当前值不能动：base 是落点，不是瞬时值
        self.assertEqual(record["libido"], 43.9)
        self.assertEqual(record["aggression"], 37.5)

    def test_reanchor_uses_the_current_panel_value(self):
        svc, _, _ = self.build(dict(LEGACY), mood_initial_libido=22, mood_initial_aggression=17)
        record = svc.profile(UID)
        self.assertEqual(record["base_libido"], 22.0)
        self.assertEqual(record["base_aggression"], 17.0)

    def test_reanchor_is_one_shot(self):
        svc, _, _ = self.build(dict(LEGACY))
        record = svc.profile(UID)
        record["base_libido"] = 41.8  # 之后的改动（比如手工修）不再被覆盖
        again = svc.profile(UID)
        self.assertEqual(again["base_libido"], 41.8, "打上标记后不该再按面板值重锚")

    def test_new_profile_gets_marker_and_initials(self):
        svc, _, conf = self.build(None)
        record = svc.profile("fresh")
        self.assertEqual(record["base_libido"], float(conf.mood_initial_libido))
        self.assertEqual(record["base_aggression"], float(conf.mood_initial_aggression))
        self.assertEqual(record["base_sync_v"], M.BASE_SYNC_VERSION)

    def test_legacy_user_can_enter_the_ccb_scene_after_reanchor(self):
        svc, _, _ = self.build(dict(LEGACY))
        record = svc.profile(UID)
        kw = dict(
            enabled=True,
            is_group=False,
            persona_prompt=FEMALE,
            affection=record["affection"],
            base_affection=record["base_affection"],
            libido=record["libido"],
            base_libido=record["base_libido"],
            energy=90.0,
            last_at=0.0,
            now=1_000_000.0,
        )
        self.assertIsNotNone(
            ccb.evaluate(**kw), "重锚之后涨幅 13.9 点，不该再被「涨得不够」挡住"
        )
        stale = dict(kw, base_libido=41.8)
        self.assertIsNone(ccb.evaluate(**stale), "旧 base 下涨幅只有 2.1，本来就该挡住")


class LibidoGainTest(unittest.TestCase):
    def build(self, **over):
        conf = cfg(**over)
        store = ScopeStore(Path(tempfile.mkdtemp()) / "s.json")
        store.load("2026-01-01", conf.cycle_length)
        svc = MoodService(store.scope, lambda: conf, None, time_source=lambda: 1_000_000.0)
        return svc, store, conf

    def test_single_message_libido_cap_is_raised_but_finite(self):
        svc, _, _ = self.build()
        record = svc.profile("42")
        out = svc._throttle_positive(M.Delta(0.0, 9.0, 0.0), record, "42", "")
        self.assertAlmostEqual(out.libido, M.LIBIDO_MESSAGE_CAP)
        self.assertGreater(M.LIBIDO_MESSAGE_CAP, 1.2, "亲近欲单句上限没抬")

    def test_daily_libido_budget_is_raised_but_finite(self):
        svc, _, _ = self.build()
        record = svc.profile("42")
        total = 0.0
        for _ in range(20):
            out = svc._throttle_positive(M.Delta(0.0, 9.0, 0.0), record, "42", "")
            total += out.libido
        self.assertAlmostEqual(total, M.DAILY_LIBIDO_GAIN_CAP)
        self.assertGreater(M.DAILY_LIBIDO_GAIN_CAP, 4.5, "亲近欲日额度没抬")

    def test_ccb_reach_mirrors_the_daily_budget(self):
        self.assertEqual(ccb.LIBIDO_DAILY_REACH, M.DAILY_LIBIDO_GAIN_CAP)
        self.assertAlmostEqual(ccb.reachable_libido_min(38.0, 30.0), 36.0)


if __name__ == "__main__":
    unittest.main()
