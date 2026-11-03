"""B4 / R5：天气新鲜度与社交能量恢复的时间口径（审核 时钟与天气 4 / 10）。

钉住的两件事：

* 天气「取到时刻」落盘一律 epoch 秒。旧存档的墙钟字符串只迁移一次；系统时钟回拨
  导致的未来时间戳按「刚取到」处理并把锚点改写成现在，不再永久判新鲜；
* 社交能量按**真实经过时间**结算（含跨重启），单次补的上限是 `MAX_SETTLE_SECONDS`，
  于是停机一周不等于满血复活，也不会因为循环醒来的次数少而少补。
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from humanoid.config import HumanoidConfig
from humanoid.services.social import (
    MAX_SETTLE_SECONDS,
    SETTLE_ANCHOR_KEY,
    SocialEnergyService,
    settle_seconds,
)
from humanoid.services.weather import (
    FETCHED_AT_KEY,
    LEGACY_FETCHED_AT_KEY,
    WeatherService,
)

from .fakes import FrozenClock, RecordingLogger, ScopeStore

TZ = ZoneInfo("Asia/Shanghai")
MOMENT = datetime(2026, 8, 22, 9, 0, tzinfo=TZ)
PAYLOAD = {
    "weather": [{"description": "多云"}],
    "main": {"temp": 26.5, "humidity": 70},
}


def cfg(**overrides) -> HumanoidConfig:
    return HumanoidConfig.from_raw({"timezone_city": "北京", **overrides})


def weather_cfg(**overrides) -> HumanoidConfig:
    base = {"weather_api_key": "0123456789abcdef", "weather_location": "Beijing,CN"}
    return cfg(**{**base, **overrides})


def build_store(conf: HumanoidConfig) -> ScopeStore:
    store = ScopeStore(Path(tempfile.mkdtemp()) / "state.json")
    store.load("2026-08-22", conf.cycle_length)
    return store


async def _ok_fetch(url: str, timeout: float) -> dict:
    return PAYLOAD


class WeatherFetchedAtTest(unittest.IsolatedAsyncioTestCase):
    """#18：取到时刻只落 epoch；旧串迁移一次；未来时间戳自愈而不是冻结。"""

    def build(self, conf=None):
        conf = conf or weather_cfg()
        self.conf = conf
        store = build_store(conf)
        clock = FrozenClock(MOMENT, city="北京")
        log = RecordingLogger()
        service = WeatherService(store.scope, lambda: self.conf, clock, _ok_fetch, log)
        return service, store, clock, log

    async def test_refresh_writes_epoch_seconds(self):
        service, store, clock, _ = self.build()
        self.assertTrue(await service.refresh_async())
        self.assertAlmostEqual(float(store.data[FETCHED_AT_KEY]), clock.timestamp())
        self.assertEqual(store.data.get(LEGACY_FETCHED_AT_KEY, ""), "", "新数据不再写墙钟串")
        snap = service.snapshot()
        self.assertFalse(snap["notice"], snap)

    async def test_refresh_clears_legacy_wallclock(self):
        service, store, clock, _ = self.build()
        store.data[LEGACY_FETCHED_AT_KEY] = "2026-08-20 20:00:00"
        self.assertTrue(await service.refresh_async())
        self.assertEqual(store.data.get(LEGACY_FETCHED_AT_KEY, ""), "")
        self.assertAlmostEqual(float(store.data[FETCHED_AT_KEY]), clock.timestamp())

    async def test_legacy_wallclock_is_migrated_and_rewritten(self):
        service, store, clock, _ = self.build()
        store.data["_cached_weather_obj"] = {"weather": "多云 🌡️ 27°C", "env": "天气：多云，气温 27℃"}
        store.data["_cached_location"] = "Beijing,CN"
        # 旧存档：无时区墙钟，1 分钟前取的。
        store.data[LEGACY_FETCHED_AT_KEY] = "2026-08-22 08:59:00"
        snap = service.snapshot()
        self.assertFalse(snap["notice"], snap)
        self.assertIn("多云", snap["weather"])
        expected = datetime(2026, 8, 22, 8, 59, tzinfo=TZ).timestamp()
        self.assertAlmostEqual(float(store.data[FETCHED_AT_KEY]), expected)
        self.assertEqual(store.data[LEGACY_FETCHED_AT_KEY], "", "迁移后清掉旧串，避免下次又按新时区反解")

    async def test_legacy_wallclock_far_past_is_expired(self):
        service, store, _, _ = self.build()
        store.data["_cached_weather_obj"] = {"weather": "多云 🌡️ 27°C", "env": "天气：多云，气温 27℃"}
        store.data["_cached_location"] = "Beijing,CN"
        store.data[LEGACY_FETCHED_AT_KEY] = "2026-08-20 20:00:00"
        self.assertEqual(service.snapshot()["env"], "天气数据过期了")

    async def test_future_timestamp_is_reanchored_not_frozen(self):
        service, store, clock, log = self.build()
        store.data["_cached_weather_obj"] = {"weather": "多云 🌡️ 27°C", "env": "天气：多云，气温 27℃"}
        store.data["_cached_location"] = "Beijing,CN"
        # 系统时钟回拨：盘上的取到时刻在一天之后。
        store.data[FETCHED_AT_KEY] = clock.timestamp() + 86400
        snap = service.snapshot()
        self.assertFalse(snap["notice"], "未来时间戳不该被当成新鲜缓存一直用")
        self.assertAlmostEqual(float(store.data[FETCHED_AT_KEY]), clock.timestamp(), msg="锚点必须自愈到现在")
        service.snapshot()
        self.assertEqual(log.text().count("回拨"), 1, "同一次回拨只告警一次")

    async def test_is_stale_compares_epoch_age(self):
        service, store, clock, _ = self.build()
        store.data["_cached_location"] = "Beijing,CN"
        store.data[FETCHED_AT_KEY] = clock.timestamp() - 30 * 60
        self.assertFalse(service.is_stale(), "30 分钟 < 默认 60 分钟刷新间隔")
        store.data[FETCHED_AT_KEY] = clock.timestamp() - 61 * 60
        self.assertTrue(service.is_stale())
        store.data[FETCHED_AT_KEY] = clock.timestamp() + 600
        self.assertFalse(service.is_stale(), "未来时间戳按刚取到处理，不当成过期")


class SocialSettleTest(unittest.IsolatedAsyncioTestCase):
    """#160：按真实经过时间结算，跨重启按锚点补，且单次封顶。"""

    def build(self, **overrides):
        conf = cfg(
            social_energy_recovery_per_minute=1.0,
            social_energy_recovery_interval_seconds=60,
            social_energy_reset_hour=-1,  # 关掉日重置，免得它把能量直接设满
            **overrides,
        )
        self.conf = conf
        store = build_store(conf)
        clock = FrozenClock(MOMENT, city="北京")
        service = SocialEnergyService(store.scope, lambda: self.conf, clock, RecordingLogger())
        return service, store, clock

    async def _run_one_round(self, service) -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(service.run_recovery_loop(stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)

    async def test_settle_seconds_clamps(self):
        self.assertEqual(settle_seconds(1000.0, 1600.0), 0.0, "时钟回拨不该倒扣能量")
        self.assertEqual(settle_seconds(1600.0, 1000.0), 600.0)
        self.assertEqual(settle_seconds(1000.0 + 7 * 86400, 1000.0), MAX_SETTLE_SECONDS)

    async def test_loop_settles_real_elapsed(self):
        service, store, clock = self.build()
        store.data["social_energy"] = 0.0
        # 上一轮结算在 10 分钟前（事件循环被卡住 / 进程刚重启）。
        store.data[SETTLE_ANCHOR_KEY] = clock.timestamp() - 600
        await self._run_one_round(service)
        self.assertAlmostEqual(service.value, 10.0, places=3, msg="600 秒 × 1.0/分钟")
        self.assertAlmostEqual(float(store.data[SETTLE_ANCHOR_KEY]), clock.timestamp())

    async def test_long_downtime_is_capped(self):
        service, store, clock = self.build()
        store.data["social_energy"] = 0.0
        store.data[SETTLE_ANCHOR_KEY] = clock.timestamp() - 7 * 86400
        await self._run_one_round(service)
        self.assertAlmostEqual(service.value, MAX_SETTLE_SECONDS / 60.0, places=3, msg="停机一周只补 1 小时，不等于满血")

    async def test_no_anchor_falls_back_to_one_interval(self):
        service, store, clock = self.build()
        store.data["social_energy"] = 0.0
        store.data.pop(SETTLE_ANCHOR_KEY, None)
        await self._run_one_round(service)
        self.assertAlmostEqual(service.value, 1.0, places=3, msg="没有锚点时按一个间隔算，不凭空补")

    async def test_anchor_written_even_when_energy_full(self):
        service, store, clock = self.build()
        store.data["social_energy"] = 100.0
        await self._run_one_round(service)
        self.assertAlmostEqual(float(store.data[SETTLE_ANCHOR_KEY]), clock.timestamp())
