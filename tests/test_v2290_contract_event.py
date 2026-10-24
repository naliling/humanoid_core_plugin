"""v2.29.0：事件进得了跨插件契约。

## 为什么

`schedule.note_event()` 让日程能被用户的一句话改写，但**它不出门就只是 Core 自己的事**。
社交插件（自主拟人社交）主动开口时读的是契约里的 `ongoing` / `wants` / `dreams`——
它完全不知道她刚被骂了，于是她挨了骂之后主动找你，说的还是天气和节日。

## 本文件盯的

1. 契约里有 `event` 块，且只在该有的时候有；
2. 导出是**只读**的——不能因为给社交层看一眼就把事件取走；
3. 旧 Core / 坏数据 → 空 dict，社交层按「这会儿没有这样一件事」处理；
4. `capabilities` 里有 `event`，社交层按能力判断，不靠版本号猜。
"""

from __future__ import annotations

import unittest

from humanoid.config import HumanoidConfig
from humanoid.services.schedule import (
    EVENT_COOLDOWN_SECONDS,
    EVENT_TTL_SECONDS,
    ScheduleService,
    event_phrase,
)

from .fakes import RecordingLogger


class _Scope:
    def __init__(self):
        self.self_state: dict = {}
        self.users: dict = {}

    def get_self(self, key, default=None):
        return self.self_state.get(key, default)

    def set_self(self, key, value):
        self.self_state[key] = value

    def update_self(self, **kw):
        self.self_state.update(kw)

    def mark_dirty(self):
        pass

    def get_user(self, uid, key, default=None):
        return self.users.setdefault(str(uid), {}).get(key, default)

    def peek_user(self, uid, key, default=None):
        entry = self.users.get(str(uid))
        return entry.get(key, default) if isinstance(entry, dict) else default


class _Clock:
    def __init__(self, t: float = 1_000_000.0):
        self._t = float(t)

    def now(self):
        import datetime, zoneinfo
        return datetime.datetime.fromtimestamp(self._t, zoneinfo.ZoneInfo("Asia/Shanghai"))

    def today_str(self):
        return self.now().strftime("%Y-%m-%d")

    def weekday(self):
        return "周六"

    def advance(self, seconds: float) -> None:
        self._t += float(seconds)


def make_service(**conf) -> ScheduleService:
    cfg = HumanoidConfig.from_raw({"timezone_city": "北京", **conf})
    clock = _Clock()
    return ScheduleService(
        scope=_Scope(),
        config_provider=lambda: cfg,
        clock=clock,
        logger=RecordingLogger(),
        monotonic=lambda: clock._t,
        random_source=lambda: 0.5,
        persona_provider=None,
        body_provider=lambda: {},
        world_provider=lambda: {},
    )


class PendingEventTest(unittest.TestCase):
    """`pending_event()` 是契约导出的数据源。"""

    def setUp(self):
        self.svc = make_service()

    def test_empty_when_nothing_happened(self):
        self.assertEqual(self.svc.pending_event(), {})

    def test_shape_when_present(self):
        self.svc.note_event(kind="很敷衍", seed=["bot1", "u1"])
        out = self.svc.pending_event()
        self.assertIn("phrase", out)
        self.assertIn("age_seconds", out)
        self.assertTrue(out["phrase"])
        self.assertGreaterEqual(out["age_seconds"], 0.0)

    def test_contract_block_has_no_age(self):
        """契约里**不带** age_seconds。

        它在事件存在的 30 分钟里每秒都在变，而契约挂在每分钟一次的身体推进上——
        带它等于那半小时里每分钟重写一遍全量 state.json（线上那份 2.4 MB）。
        由头有时效，那是 anchors 的 TTL 在管，不是契约的活。
        """
        from humanoid.link import _event_block

        class _Core:
            def __init__(self, schedule):
                self.schedule = schedule

        self.svc.note_event(kind="很敷衍", seed=["bot1", "u1"])
        block = _event_block(_Core(self.svc))
        self.assertEqual(block, {"phrase": block["phrase"]})
        self.assertNotIn("age_seconds", block)

    def test_read_does_not_consume(self):
        """给社交层看一眼不能把事件取走——取了这一轮决定就用不上了。"""
        self.svc.note_event(kind="很敷衍", seed=["bot1", "u1"])
        first = self.svc.pending_event()
        second = self.svc.pending_event()
        self.assertEqual(first, second)
        # 仍然可以被本轮的生成消费
        self.assertTrue(self.svc._consume_event())

    def test_age_advances(self):
        self.svc.note_event(kind="很敷衍", seed=["bot1", "u1"])
        before = self.svc.pending_event()["age_seconds"]
        self.svc._clock_test = getattr(self.svc, "_clock", None)
        self.svc._clock.advance(120.0)
        after = self.svc.pending_event()["age_seconds"]
        self.assertGreater(after, before)

    def test_empty_after_expiry(self):
        self.svc.note_event(kind="很敷衍", seed=["bot1", "u1"])
        self.svc._clock.advance(EVENT_TTL_SECONDS + 1)
        self.assertEqual(self.svc.pending_event(), {})

    def test_empty_after_consume(self):
        self.svc.note_event(kind="很敷衍", seed=["bot1", "u1"])
        self.svc._consume_event()
        self.assertEqual(self.svc.pending_event(), {})

    def test_unknown_kind_never_appears(self):
        self.assertFalse(self.svc.note_event(kind="乱写的", seed=["b"]))
        self.assertEqual(self.svc.pending_event(), {})


class ContractCapabilitiesTest(unittest.TestCase):
    def test_event_is_a_capability(self):
        from humanoid.link import CONTRACT_CAPABILITIES
        self.assertIn("event", CONTRACT_CAPABILITIES)

    def test_api_version_unchanged(self):
        """只加不改：新能力不动 API_VERSION。"""
        from humanoid.api import API_VERSION
        self.assertEqual(API_VERSION, 2)


class RefreshContractKeysTest(unittest.TestCase):
    """`refresh_contract` 的比较键里必须有 event。

    漏了它，装了新 Core 但契约里还留着旧结构时，新算出来的 event 键**永远写不进去**
    （其它键都没变 → 直接 return previous）。社交层于是永远拿不到
    「她刚经历了什么」，而且不报错——这是真机上跑出来才发现的一处。
    """

    def test_event_is_a_compared_key(self):
        import inspect
        from humanoid.core_instance import HumanoidCoreInstance
        src = inspect.getsource(HumanoidCoreInstance.refresh_contract)
        self.assertIn('"event"', src, "refresh_contract 的比较键里没有 event")

    def test_event_block_stable_while_event_pending(self):
        """事件还在保鲜期内，契约块不该变——否则契约每分钟重写一次。"""
        from humanoid.link import _event_block

        class _Core:
            def __init__(self, schedule):
                self.schedule = schedule

        svc = make_service()
        svc.note_event(kind="很敷衍", seed=["bot1", "u1"])
        core = _Core(svc)
        first = _event_block(core)
        svc._clock.advance(120.0)          # age 变了
        self.assertEqual(_event_block(core), first)
        svc._clock.advance(600.0)          # age 又变了
        self.assertEqual(_event_block(core), first)


if __name__ == "__main__":
    unittest.main()
