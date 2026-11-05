"""v3.0.10：角色专属素材（按人设生成的那一批）。

内置的 49 条是「人可能做的事」，不是「她会做的事」——人设是兽医的人，池子里也不该
出现给猫量体温以外的东西，反过来一个没有宠物的人不该天天在喂猫。所以人设第一次出现
（或人设改了）时按她生成一批、按人设指纹缓存；人设没变就一次模型都不再调。

用例只走公开入口与脚本化替身，不复制实现里的字面量。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from humanoid.llm import LLMResult, OUTCOME_OK
from humanoid.persona import Persona
from humanoid.services import role_materials
from humanoid.services.life_material import DEFAULT_COUNT, materials_for
from humanoid.services.schedule import ScheduleService

from .fakes import FrozenClock, RecordingLogger, ScopeStore
from .test_schedule import cfg

TZ = ZoneInfo("Asia/Shanghai")
MOMENT = datetime(2026, 8, 22, 13, 0, tzinfo=TZ)
SEED = ["role-1", "2026-08-22"]
PERSONA = "我是个上班族，家里养了一只橘猫，喜欢看书和画画。"
PLAIN = "我是个学生，平时一个人住。"


def _scope(moment=MOMENT):
    conf = cfg()
    store = ScopeStore(Path(tempfile.mkdtemp()) / "state.json")
    store.load(moment.strftime("%Y-%m-%d"), conf.cycle_length)
    return store.scope


def _entry(event, **extra):
    row = {
        "event": event,
        "location": "家里",
        "minutes": [10, 20],
        "slots": ["下午"],
        "band": "life",
        "needs": [],
        "interest": "",
        "topic": "日常",
    }
    row.update(extra)
    return row


def _reply(count=10):
    return json.dumps(
        [_entry("在客厅做第 " + str(i) + " 件事") for i in range(count)],
        ensure_ascii=False,
    )


class _Gateway:
    """脚本化替身：按顺序吐文本，调用次数与最后一次 prompt 都留得下来。"""

    def __init__(self, texts):
        self.texts = list(texts)
        self.prompts: list[str] = []

    async def generate(self, **kwargs):
        self.prompts.append(str(kwargs.get("prompt") or ""))
        text = self.texts[min(len(self.prompts) - 1, len(self.texts) - 1)]
        return LLMResult(
            ok=True, text=text, outcome=OUTCOME_OK, label="fake", provider_id="p1"
        )


def _service(moment=MOMENT, persona_text=PERSONA, **overrides):
    conf = cfg(**overrides)
    store = ScopeStore(Path(tempfile.mkdtemp()) / "state.json")
    store.load(moment.strftime("%Y-%m-%d"), conf.cycle_length)
    holder = {"text": persona_text}

    async def persona():
        # 真实的人格源是异步的：写成普通函数会被 _resolve_persona 的 await 判成异常，
        # 于是人设根本没进来，测试也就测不到「换人设」这条路径。
        return Persona(name="她", prompt=holder["text"], source="test")
    service = ScheduleService(
        store.scope,
        lambda: conf,
        FrozenClock(moment),
        logger=RecordingLogger(),
        persona_provider=persona,
        monotonic=lambda: 10_000.0,
    )
    return service, conf, holder


class FingerprintTest(unittest.TestCase):
    def test_same_persona_same_fingerprint(self):
        self.assertEqual(
            role_materials.persona_fingerprint(PERSONA),
            role_materials.persona_fingerprint(PERSONA),
        )

    def test_whitespace_does_not_change_it(self):
        self.assertEqual(
            role_materials.persona_fingerprint(PERSONA),
            role_materials.persona_fingerprint("  " + PERSONA + "\n\t  "),
        )

    def test_changed_persona_changes_it(self):
        self.assertNotEqual(
            role_materials.persona_fingerprint(PERSONA),
            role_materials.persona_fingerprint(PLAIN),
        )

    def test_empty_persona_has_no_fingerprint(self):
        self.assertEqual(role_materials.persona_fingerprint("   "), "")


class ParseMaterialsTest(unittest.TestCase):
    def test_parses_a_plain_array(self):
        items = role_materials.parse_materials(_reply(3))
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["event"], "在客厅做第 0 件事")
        self.assertEqual(items[0]["location"], "家里")

    def test_tolerates_fences_and_chatter(self):
        raw = "好的，这是素材：\n\n" + chr(96) * 3 + "json\n" + _reply(2) + "\n" + chr(96) * 3 + "\n希望有用。"
        items = role_materials.parse_materials(raw)
        self.assertEqual(len(items), 2)

    def test_wrapper_object_is_unwrapped(self):
        raw = json.dumps({"materials": [_entry("在厨房煮面")]}, ensure_ascii=False)
        self.assertEqual(len(role_materials.parse_materials(raw)), 1)

    def test_one_bad_entry_does_not_drop_the_batch(self):
        raw = json.dumps(
            [{"event": ""}, "不是对象", _entry("在阳台浇花")], ensure_ascii=False
        )
        items = role_materials.parse_materials(raw)
        self.assertEqual([item["event"] for item in items], ["在阳台浇花"])

    def test_minutes_are_clamped(self):
        items = role_materials.parse_materials(
            json.dumps([_entry("做很久的事", minutes=[1, 900])], ensure_ascii=False)
        )
        self.assertEqual(items[0]["minutes"], (5, 120))

    def test_broken_minutes_fall_back_to_a_short_default(self):
        items = role_materials.parse_materials(
            json.dumps([_entry("短事", minutes="很久")], ensure_ascii=False)
        )
        lo, hi = items[0]["minutes"]
        self.assertLessEqual(lo, hi)
        self.assertGreaterEqual(lo, 5)

    def test_unknown_band_falls_back_to_life(self):
        items = role_materials.parse_materials(
            json.dumps([_entry("发呆", band="???")], ensure_ascii=False)
        )
        self.assertEqual(items[0]["band"], "life")

    def test_invalid_slots_fall_back_to_daytime(self):
        items = role_materials.parse_materials(
            json.dumps([_entry("晒太阳", slots=["半夜"])], ensure_ascii=False)
        )
        self.assertNotIn("凌晨", items[0]["slots"])
        self.assertIn("下午", items[0]["slots"])

    def test_duplicate_events_collapse(self):
        raw = json.dumps([_entry("在阳台浇花"), _entry("在阳台浇花")], ensure_ascii=False)
        self.assertEqual(len(role_materials.parse_materials(raw)), 1)

    def test_count_is_a_ceiling(self):
        self.assertEqual(len(role_materials.parse_materials(_reply(30), 5)), 5)

    def test_garbage_returns_empty(self):
        self.assertEqual(role_materials.parse_materials("模型今天不想给 JSON"), [])


class AcceptedTest(unittest.TestCase):
    def test_pet_item_is_dropped_without_a_pet(self):
        items = [_entry("给猫添粮", needs=["pet"])]
        self.assertEqual(role_materials.accepted(items, PLAIN), [])

    def test_pet_item_is_kept_with_a_pet(self):
        items = [_entry("给猫添粮", needs=["pet"])]
        self.assertEqual(len(role_materials.accepted(items, PERSONA)), 1)

    def test_interest_item_is_dropped_without_that_interest(self):
        items = [_entry("画速写", interest="draw")]
        self.assertEqual(role_materials.accepted(items, PLAIN), [])
        self.assertEqual(len(role_materials.accepted(items, PERSONA)), 1)

    def test_item_without_conditions_always_survives(self):
        items = [_entry("在窗边发呆")]
        self.assertEqual(len(role_materials.accepted(items, PLAIN)), 1)


class StoreTest(unittest.TestCase):
    def test_roundtrip(self):
        scope = _scope()
        items = role_materials.parse_materials(_reply(2))
        role_materials.store(scope, "fp-1", items)
        loaded = role_materials.load(scope)
        self.assertEqual([item["event"] for item in loaded], [item["event"] for item in items])
        self.assertEqual(role_materials.stored_fingerprint(scope), "fp-1")

    def test_garbage_on_disk_reads_as_empty(self):
        scope = _scope()
        scope.update_self(**{role_materials.ROLE_MATERIALS_KEY: "坏了"})
        self.assertEqual(role_materials.load(scope), [])

    def test_bad_entries_are_dropped_on_read(self):
        scope = _scope()
        scope.update_self(
            **{role_materials.ROLE_MATERIALS_KEY: [{"event": ""}, _entry("在厨房煮面")]}
        )
        self.assertEqual([item["event"] for item in role_materials.load(scope)], ["在厨房煮面"])


class NeedsBuildTest(unittest.TestCase):
    def test_no_persona_means_no_build(self):
        self.assertFalse(role_materials.needs_build(_scope(), "", 1000.0))

    def test_empty_cache_means_build(self):
        self.assertTrue(role_materials.needs_build(_scope(), "fp-1", 1000.0))

    def test_same_fingerprint_means_no_build(self):
        scope = _scope()
        role_materials.store(scope, "fp-1", role_materials.parse_materials(_reply(2)))
        self.assertFalse(role_materials.needs_build(scope, "fp-1", 1000.0))

    def test_changed_persona_rebuilds(self):
        scope = _scope()
        role_materials.store(scope, "fp-1", role_materials.parse_materials(_reply(2)))
        self.assertTrue(role_materials.needs_build(scope, "fp-2", 1000.0))

    def test_changed_persona_does_not_wait_for_the_backoff(self):
        # 上一版人设是刚刚建成功的，换成新人设必须立刻重建——原来这里会拿
        # 「上次尝试时间」当退避，白等 30 分钟，人设换了素材还是旧的。
        scope = _scope()
        role_materials.store(scope, "fp-1", role_materials.parse_materials(_reply(2)))
        role_materials.mark_try(scope, 1000.0, "fp-1")
        self.assertTrue(
            role_materials.needs_build(scope, "fp-2", 1010.0, retry_seconds=1800.0)
        )

    def test_failed_attempt_waits_for_the_backoff(self):
        scope = _scope()
        role_materials.store(scope, "fp-1", role_materials.parse_materials(_reply(2)))
        role_materials.mark_try(scope, 1000.0, "fp-2")
        self.assertFalse(
            role_materials.needs_build(scope, "fp-2", 1010.0, retry_seconds=1800.0)
        )
        self.assertTrue(
            role_materials.needs_build(scope, "fp-2", 3000.0, retry_seconds=1800.0)
        )


class MaterialsPoolTest(unittest.TestCase):
    def test_role_items_reach_the_pool(self):
        extra = [
            {
                "event": "在阳台给多肉换盆",
                "location": "阳台",
                "minutes": (20, 40),
                "slots": ("下午",),
                "topic": "换盆",
                "band": "rare",
                "needs": (),
                "interest": "",
                "wants": False,
            }
        ]
        items = materials_for(SEED, PERSONA, 15 * 60, (), DEFAULT_COUNT, extra)
        self.assertIn("在阳台给多肉换盆", [item["event"] for item in items])

    def test_pool_without_extra_still_works(self):
        items = materials_for(SEED, PERSONA, 15 * 60, (), DEFAULT_COUNT)
        self.assertTrue(items)


class ServiceBuildTest(unittest.IsolatedAsyncioTestCase):
    async def _run_once(self, service):
        self.assertTrue(service.ensure_role_materials(), "应当排入一次素材生成")
        await service._role_task

    async def test_builds_once_and_caches(self):
        service, _conf, _holder = _service()
        gateway = _Gateway([_reply(10)])
        service.gateway = gateway
        await self._run_once(service)
        self.assertEqual(len(role_materials.load(service._scope)), 10)
        self.assertEqual(len(gateway.prompts), 1)
        self.assertIn("只输出 JSON 数组", gateway.prompts[0])

    async def test_unchanged_persona_never_asks_again(self):
        service, _conf, _holder = _service()
        gateway = _Gateway([_reply(10)])
        service.gateway = gateway
        await self._run_once(service)
        service._role_checked_at = None
        await self._run_once(service)
        self.assertEqual(len(gateway.prompts), 1, "人设没变不该再问一次模型")

    async def test_changed_persona_rebuilds(self):
        service, _conf, holder = _service()
        gateway = _Gateway([_reply(10), _reply(12)])
        service.gateway = gateway
        await self._run_once(service)
        holder["text"] = PLAIN
        service._role_checked_at = None
        await self._run_once(service)
        self.assertEqual(len(gateway.prompts), 2)
        self.assertEqual(len(role_materials.load(service._scope)), 12)

    async def test_too_few_items_keep_the_old_batch(self):
        service, _conf, holder = _service()
        gateway = _Gateway([_reply(10), _reply(3)])
        service.gateway = gateway
        await self._run_once(service)
        holder["text"] = PLAIN
        service._role_checked_at = None
        await self._run_once(service)
        self.assertEqual(len(role_materials.load(service._scope)), 10, "太少的一批不该顶掉旧的")

    async def test_invented_identities_are_filtered_out(self):
        # 人设里没有猫，模型却写了「给猫添粮」：这条必须被扔掉，其余九条照常留下。
        items = [_entry("给猫添粮", needs=["pet"])] + [_entry("在阳台做第 %d 件事" % i) for i in range(9)]
        raw = json.dumps(items, ensure_ascii=False)
        service, _conf, _holder = _service(persona_text=PLAIN)
        service.gateway = _Gateway([raw])
        await self._run_once(service)
        stored = [item["event"] for item in role_materials.load(service._scope)]
        self.assertEqual(len(stored), 9)
        self.assertNotIn("给猫添粮", stored)

    async def test_checks_are_throttled(self):
        service, _conf, _holder = _service()
        service.gateway = _Gateway([_reply(10)])
        self.assertTrue(service.ensure_role_materials())
        await service._role_task
        self.assertFalse(service.ensure_role_materials(), "退避期内不该再检查")

    async def test_without_llm_schedule_it_does_nothing(self):
        service, _conf, _holder = _service(use_llm_schedule=False)
        service.gateway = _Gateway([_reply(10)])
        self.assertFalse(service.ensure_role_materials())
        self.assertEqual(role_materials.load(service._scope), [])

    async def test_no_gateway_does_nothing(self):
        service, _conf, _holder = _service()
        self.assertFalse(service.ensure_role_materials())
