"""v2.28.0：契约与门面把梦、醒来状态导出给社交层（C4）。

用户原话：「白天/凌晨我觉得要加上梦境」——梦生成出来如果只留在 Core 里，社交层
拿不到，主动消息就还是只有天气。这里钉住三件事：

* `link.build_contract` 的 `day.dreams` / 顶层 `wake` 与能力清单；
* `api.CoreFacade.brief` 的 `day.dreams` / `wake`；
* 兼容：旧 state 里那份没有新字段的契约、没有 dreams 的旧日程，都必须安全降级，
  `CONTRACT_VERSION` / `API_VERSION` 保持 2（只加不改）。
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from humanoid.api import API_VERSION, CAPABILITIES, CoreFacade
from humanoid.core_instance import HumanoidCoreInstance
from humanoid.link import CONTRACT_CAPABILITIES, CONTRACT_VERSION, build_contract
from humanoid.state import StateStore

from .fakes import FakeContext, FrozenClock, RecordingLogger

TZ = ZoneInfo("Asia/Shanghai")
MOMENT = datetime(2026, 8, 22, 7, 10, tzinfo=TZ)
TODAY = "2026-08-22"


def cfg(**kw):
    from humanoid.config import HumanoidConfig

    return HumanoidConfig.from_raw({"timezone_city": "北京", **kw})


def make_core(**conf) -> HumanoidCoreInstance:
    store = StateStore(Path(tempfile.mkdtemp()) / "s.json", lambda: 0.01)
    store.load(TODAY, 28)
    config = cfg(**conf)
    core = HumanoidCoreInstance(
        role_id="bot1", state_store=store, config_provider=lambda: config,
        logger=RecordingLogger(), stop_event=asyncio.Event(),
        resolver=FakeContext(), gateway=None,
    )
    clock = FrozenClock(MOMENT)
    core.clock = clock
    for name in ("schedule", "soma", "energy", "social", "process", "mood"):
        service = getattr(core, name, None)
        if service is not None and hasattr(service, "_clock"):
            service._clock = clock
    core.scope.update_self(
        today_date=TODAY,
        daily_schedule=[
            {"start": "00:00", "end": "07:00", "event": "睡眠",
             "dreams": ["一片海", "老朋友"], "energy_rate": 0.15},
            {"start": "07:00", "end": "24:00", "event": "工作", "energy_rate": -0.05},
        ],
    )
    core.soma.data.update({
        "wake_at": MOMENT.timestamp() - 300.0,
        "last_wake_at": MOMENT.timestamp() - 300.0,
        "wake_inertia_until": MOMENT.timestamp() + 300.0,
        "wake_disturbed": True,
        "last_sleep_hours": 7.0,
    })
    return core


class ContractDreamsAndWakeTest(unittest.TestCase):
    def test_day_dreams_and_wake_are_exported(self):
        core = make_core()
        contract = build_contract(core)
        self.assertEqual(contract["day"]["dreams"], ["一片海", "老朋友"])
        wake = contract["wake"]
        self.assertEqual(
            set(wake),
            {"asleep", "wake_at", "inertia_until", "inertia_minutes_left",
             "disturbed", "last_sleep_hours"},
        )
        self.assertFalse(wake["asleep"])
        self.assertTrue(wake["disturbed"])
        self.assertAlmostEqual(wake["last_sleep_hours"], 7.0)
        self.assertEqual(wake["inertia_minutes_left"], 5)

    def test_capabilities_only_added(self):
        self.assertEqual(CONTRACT_VERSION, 2)
        for legacy in ("body", "feelings", "form", "activity", "day", "ongoing", "routine"):
            self.assertIn(legacy, CONTRACT_CAPABILITIES)
        self.assertIn("dreams", CONTRACT_CAPABILITIES)
        self.assertIn("wake", CONTRACT_CAPABILITIES)
        self.assertEqual(API_VERSION, 2)
        for legacy in ("brief", "clock", "events", "fingerprint"):
            self.assertIn(legacy, CAPABILITIES)
        self.assertIn("dreams", CAPABILITIES)
        self.assertIn("wake", CAPABILITIES)

    def test_fields_hash_is_stable_and_covers_new_structure(self):
        from humanoid.link import _fields_hash

        core = make_core()
        first = build_contract(core)
        second = build_contract(core)
        self.assertTrue(str(first["fields_hash"]).startswith("sha256:"))
        self.assertEqual(first["fields_hash"], second["fields_hash"])
        # 结构指纹只覆盖键名：多一个 day.dreams / wake 就应该变。
        self.assertNotEqual(
            _fields_hash({"day": {"doing": "工作"}}),
            _fields_hash({"day": {"doing": "工作", "dreams": []}}),
            "结构指纹没覆盖新增字段",
        )

    def test_old_schedule_without_dreams_exports_empty_list(self):
        core = make_core()
        core.scope.update_self(
            today_date=TODAY,
            daily_schedule=[
                {"start": "00:00", "end": "07:00", "event": "睡眠", "energy_rate": 0.15},
                {"start": "07:00", "end": "24:00", "event": "工作", "energy_rate": -0.05},
            ],
        )
        contract = build_contract(core)
        self.assertEqual(contract["day"]["dreams"], [])

    def test_broken_soma_exports_empty_wake(self):
        from humanoid.link import _wake_block

        self.assertEqual(_wake_block(SimpleNamespace()), {})
        self.assertEqual(
            _wake_block(SimpleNamespace(soma=SimpleNamespace(
                wake_state=lambda: (_ for _ in ()).throw(RuntimeError("boom")))),
            ),
            {},
        )
        self.assertEqual(_wake_block(SimpleNamespace(soma=SimpleNamespace(wake_state=lambda: "坏"))), {})


class FacadeDreamsAndWakeTest(unittest.TestCase):
    def setUp(self):
        self.core = make_core()
        self.roles = _Roles([self.core])
        self.facade = CoreFacade(self.roles)

    def test_brief_carries_dreams_and_wake(self):
        # 门面读的是**落盘的那份契约**：先刷新一次，模拟正常插件 tick。
        self.core.refresh_contract()
        brief = self.facade.brief("bot1", "10001", task="proactive")
        self.assertEqual(brief["day"]["dreams"], ["一片海", "老朋友"])
        self.assertTrue(brief["wake"]["disturbed"])
        self.assertIn("wake", brief)

    def test_old_contract_without_new_fields_is_safe(self):
        # 模拟旧 Core 写下的契约：没有 day.dreams、没有 wake。
        self.core.scope.set_self("contract", {
            "v": 2,
            "body": {"energy": 60.0, "energy_text": "状态挺好"},
            "day": {"doing": "工作", "done": [], "next": []},
        })
        brief = self.facade.brief("bot1", "10001")
        self.assertEqual(brief["day"]["dreams"], [])
        # wake 可以现算（门面的意义），拿不到时才是空 dict；这里 soma 还活着，必须有值。
        self.assertIn("inertia_until", brief["wake"])

    def test_unknown_role_still_returns_none(self):
        self.assertIsNone(self.facade.brief("不存在的角色", "10001"))

    def test_dreams_change_the_brief_fingerprint(self):
        base = self.facade._fingerprint(self.core, {"day": {"dreams": []}})
        changed = self.facade._fingerprint(self.core, {"day": {"dreams": ["梦到一片海"]}})
        self.assertNotEqual(base, changed, "梦变了，缓存指纹该跟着变")


class _Roles:
    """只给门面用的最小 RoleManager。"""

    def __init__(self, cores):
        self._cores = cores

    def get_all(self):
        return list(self._cores)


if __name__ == "__main__":
    unittest.main()
