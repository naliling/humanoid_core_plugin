"""状态与坏值硬化（这一批盯的是「坏数据把好数据一起带走」）。

四类问题，都是**静默**的：

1. `state.json` 里一个 NaN 就够让冷静期永远不到期（`now >= nan` 恒假）；
2. 一个孤立代理项字符（`\\ud800`，平台昵称/消息里能带进来）能让**此后每一次落盘**都
   抛 UnicodeEncodeError——插件表面照常跑，实则再也不写盘；
3. 一个没有 JSON 表示的叶子（`set`、自定义对象）原来会让 `_serialize` 退回**空骨架**，
   等于一个坏值抹掉所有人的好感度；
4. `min(1.0, nan)` 返回 1.0，所以读到坏值的在意度会**写回盘上**并钉在满分。

每条都断言正向结果（某个值真的写进去了 / 某个东西真的被删了），不只断言不抛异常。
"""

from __future__ import annotations

import asyncio
import json
import math
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from humanoid.config import HumanoidConfig
from humanoid.emotion import EmotionLayer
from humanoid.llm import LLMGateway, ProviderResolver
from humanoid.role_scope import RoleScope
from humanoid.services import mood as M
from humanoid.services.behavior import BehaviorService
from humanoid.services.mood import MoodService
from humanoid.state import StateStore, _serialize

from .fakes import FakeContext, FakeProvider, RecordingLogger, ScopeStore

TZ = timezone.utc


def cfg(**overrides) -> HumanoidConfig:
    return HumanoidConfig.from_raw({"timezone_city": "北京", **overrides})


# ----------------------------------------------------------------------
# 1. 读盘：NaN/Infinity 不许活着进内存
# ----------------------------------------------------------------------

class NonFiniteNumbersAreScrubbedOnLoad(unittest.TestCase):
    """`json.loads` **接受** `NaN` / `Infinity` 字面量，而写盘侧早就不写它们了。

    所以「盘上有 NaN」是别人（手改、旧版本、第三方工具）留下的，读进来不清的话：
    `cool_until=NaN` → `now >= until` 恒假 → 她**永远**出不了冷静期。
    """

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "state.json"
        self.log = RecordingLogger()

    def _write(self, payload: dict) -> None:
        # 与真实的坏文件同形：JSON 里出现裸的非有限字面量（json.dumps 默认就写得出）。
        text = json.dumps(payload, ensure_ascii=False)
        self.assertTrue(
            "NaN" in text or "Infinity" in text,
            "样本里必须真的有裸的 NaN/Infinity，否则这条测试毫无意义",
        )
        self.path.write_text(text, encoding="utf-8")

    def _load(self) -> StateStore:
        store = StateStore(self.path, lambda: 1.0, self.log)
        store.load("2026-01-01")
        return store

    def test_nan_does_not_survive_load_and_good_data_does(self):
        self._write({
            "_state_version": 3,
            "roles": {"default": {"self": {}, "users": {"42": {
                "nickname": "阿哲",
                "mood": {
                    "affection": float("nan"),
                    "base_affection": 46.0,
                    "cool_until": float("nan"),
                    "cool_reason": "上次那件事",
                },
            }}}},
        })
        store = self._load()
        mood = store.data["roles"]["default"]["users"]["42"]["mood"]
        self.assertIsNone(mood["cool_until"], "NaN 活着进了内存 → 冷静期永不到期")
        self.assertIsNone(mood["affection"])
        self.assertEqual(mood["cool_reason"], "上次那件事", "清洗不该动同一条记录里的正常字段")
        self.assertEqual(
            store.data["roles"]["default"]["users"]["42"]["nickname"], "阿哲"
        )
        self.assertIn("非有限", self.log.text("warning"), "清掉了坏值却没留一条日志")

    def test_flush_after_load_writes_null_not_nan(self):
        self._write({
            "_state_version": 3,
            "roles": {"default": {"self": {"energy": float("inf")}, "users": {}}},
        })
        store = self._load()
        # 读盘本身不写盘（清洗只是把内存里那份修干净，文件留给下一次真的改动去改）。
        store.mark_dirty()
        self.assertTrue(store.flush_sync(), "清洗过的状态必须能落盘")
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn("NaN", text)
        self.assertNotIn("Infinity", text)
        self.assertEqual(json.loads(text)["roles"]["default"]["users"], {})
        self.assertTrue(
            math.isfinite(store.data["roles"]["default"]["self"]["energy"]),
            "inf 精力没有被修成有限数",
        )


# ----------------------------------------------------------------------
# 2. 落盘：孤立代理项不许把持久化钉死
# ----------------------------------------------------------------------

class UnencodableCharacterDoesNotStopPersistence(unittest.TestCase):
    """`'\\ud800'.encode('utf-8')` 抛 UnicodeEncodeError，而状态里可能就有它。

    原来这个异常会从 `_atomic_write` 一路冒到 `flush()`：此后**每一次**落盘都在同一个
    字符上失败，插件照常回复但状态再也不落盘，重启丢光。宁可把那个字符写成 `?`。
    """

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "state.json"
        self.log = RecordingLogger()
        self.store = StateStore(self.path, lambda: 1.0, self.log)
        self.store.load("2026-01-01")

    def test_flush_writes_instead_of_raising(self):
        self.store.data["roles"].setdefault("default", {"self": {}, "users": {}})
        users = self.store.data["roles"]["default"].setdefault("users", {})
        users["42"] = {"nickname": "阿哲", "mood": {"affection": 66.0}}
        users["43"] = {"nickname": "小\ud800明"}
        self.assertTrue(self.store.flush_sync(), "坏字符把整次落盘打回去了")
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["roles"]["default"]["users"]["42"]["nickname"], "阿哲")
        self.assertEqual(
            saved["roles"]["default"]["users"]["42"]["mood"]["affection"], 66.0,
            "一个坏字符连带把别人的数据一起丢了",
        )
        self.assertIn("孤立代理项", self.log.text("warning"))

    def test_later_flushes_still_work(self):
        """修好这一份之后，后续的改动照样落得下去（不是「只成功这一次」）。"""
        self.store.set("bad", "\ud800")
        self.assertTrue(self.store.flush_sync())
        self.store.data["roles"]["default"] = {
            "self": {"energy": 12.0}, "users": {}
        }
        self.store.mark_dirty()
        self.assertTrue(self.store.flush_sync())
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["roles"]["default"]["self"]["energy"], 12.0)
        warnings = [
            line for line in self.log.text("warning").splitlines() if "孤立代理项" in line
        ]
        self.assertEqual(
            len(warnings), 1,
            "坏字符会一直留在内存里，每次落盘都重报一遍会把日志刷成每 5 秒一行",
        )


# ----------------------------------------------------------------------
# 3. 序列化：没有 JSON 表示的叶子不许换掉整棵树
# ----------------------------------------------------------------------

class UnserializableLeafDoesNotWipeState(unittest.TestCase):
    """坏叶子 → 整份状态换成空骨架，是「一个坏值抹掉所有人的好感度」。

    `_serialize` 的两条失败路径原来一条漏接（TypeError）、一条会把整棵树换成
    `default_state()`，于是既可能「再也写不出去」也可能「写出空文件」。
    """

    GOOD = {"_state_version": 3, "roles": {"default": {"self": {"energy": 71.0}, "users": {
        "42": {"nickname": "阿哲", "mood": {"affection": 77.0}},
    }}}}

    def test_bad_leaf_becomes_null_and_the_rest_survives(self):
        state = json.loads(json.dumps(self.GOOD))
        state["roles"]["default"]["users"]["42"]["mood"]["bad"] = {1, 2}
        text = _serialize(state)
        saved = json.loads(text)
        self.assertIsNone(saved["roles"]["default"]["users"]["42"]["mood"]["bad"])
        self.assertEqual(saved["roles"]["default"]["users"]["42"]["mood"]["affection"], 77.0)
        self.assertEqual(saved["roles"]["default"]["self"]["energy"], 71.0)

    def test_nan_plus_bad_leaf_still_writes_the_roles(self):
        """原来的组合场景：先 ValueError（NaN）→ 清洗后 TypeError（set）→ 退回空骨架。"""
        state = json.loads(json.dumps(self.GOOD))
        bad = state["roles"]["default"]["users"]["42"]["mood"]
        bad["weight"] = float("nan")          # 顺序在前 → 第一次 dumps 抛 ValueError
        bad["objects"] = {1, 2}               # 清洗后仍在 → 第二次 dumps 抛 TypeError
        text = _serialize(state)
        saved = json.loads(text)
        self.assertIsNone(saved["roles"]["default"]["users"]["42"]["mood"]["weight"])
        self.assertIsNone(saved["roles"]["default"]["users"]["42"]["mood"]["objects"])
        self.assertEqual(saved["roles"]["default"]["users"]["42"]["nickname"], "阿哲")
        self.assertEqual(saved["roles"]["default"]["users"]["42"]["mood"]["affection"], 77.0)

    def test_flush_sync_writes_the_roles_instead_of_an_empty_skeleton(self):
        store = StateStore(Path(tempfile.mkdtemp()) / "state.json", lambda: 1.0)
        store.load("2026-01-01")
        store.data["roles"] = json.loads(json.dumps(self.GOOD))["roles"]
        store.data["roles"]["default"]["users"]["42"]["bad"] = {1, 2}
        store.mark_dirty()
        self.assertTrue(store.flush_sync())
        saved = json.loads(store.path.read_text(encoding="utf-8"))
        self.assertIn("42", saved["roles"]["default"]["users"], "整份状态被换成空骨架了")


# ----------------------------------------------------------------------
# 4. 坏值不许变成「满分」/「永不过期」
# ----------------------------------------------------------------------

class _CoolingCore:
    """`_cooling_line` 只用到 `mood.profile` 与 `now_epoch`。"""

    def __init__(self, record: dict, now: float = 1_000_000.0) -> None:
        self._record = record
        self._now = now

    class _Clock:
        @staticmethod
        def today_str() -> str:
            return "2026-01-01"

    clock = _Clock()
    role_id = "bot1"

    def now_epoch(self) -> float:
        return self._now

    def mood_profile(self):  # pragma: no cover - 占位，见下面的 mood 属性
        return self._record


class NonFiniteCoolUntilDoesNotFreezeHerMood(unittest.TestCase):
    def _layer(self, record: dict) -> EmotionLayer:
        core = _CoolingCore(record)
        core.mood = type("M", (), {"profile": staticmethod(lambda uid: record)})()
        return EmotionLayer(core)

    def test_nan_until_is_treated_as_no_cooling(self):
        layer = self._layer({"affection": 46.0, "cool_until": float("nan"), "cool_reason": "上次"})
        self.assertIsNone(
            layer._cooling_line("42", 0), "NaN 的 cool_until 让她永远冷着、盖掉所有别的情绪"
        )

    def test_valid_until_still_cools(self):
        """反向控制：真的在冷静期里必须照样返回那一条。"""
        layer = self._layer({"affection": 46.0, "cool_until": 1_000_600.0, "cool_reason": "上次"})
        got = layer._cooling_line("42", 0)
        self.assertIsNotNone(got)
        self.assertEqual(got[0], 0.95)
        self.assertIn("上次", got[1])

    def test_num_rejects_non_finite_and_keeps_numbers(self):
        from humanoid.emotion import _num

        self.assertEqual(_num(float("nan"), 46.0), 46.0)
        self.assertEqual(_num(float("inf"), 46.0), 46.0)
        self.assertEqual(_num(float("-inf"), 46.0), 46.0)
        self.assertEqual(_num("不是数字", 46.0), 46.0)
        self.assertEqual(_num("12.5", 46.0), 12.5)
        self.assertEqual(_num(None, 46.0), 46.0)
        self.assertEqual(_num(0, 46.0), 0.0)


class NonFiniteCoolingNoteIsIgnored(unittest.TestCase):
    def _service(self, record: dict):
        conf = cfg()
        store = ScopeStore(Path(tempfile.mkdtemp()) / "s.json")
        store.load("2026-01-01", conf.cycle_length)
        store.scope.set_user("42", "mood", record)
        svc = MoodService(store.scope, lambda: conf, None, time_source=lambda: 1_000_000.0)
        return svc

    def test_nan_until_yields_no_note(self):
        svc = self._service({"affection": 46.0, "cool_until": float("nan"), "cool_reason": "上次"})
        self.assertEqual(svc.cooling_note("42"), "", "NaN 的 cool_until 让冷静期永不到期")

    def test_valid_until_still_produces_a_note(self):
        svc = self._service({"affection": 46.0, "cool_until": 1_000_600.0, "cool_reason": "上次"})
        note = svc.cooling_note("42")
        self.assertIn("气头上", note)
        self.assertIn("上次", note, "理由丢了，她只会说一句无来由的「别来吵」")


class LlmDeltaRejectsNonFiniteNumbers(unittest.IsolatedAsyncioTestCase):
    """模型吐出 `NaN`/`Infinity`（都是合法 JSON 字面量）时不许被夹成**正向满分**。

    `json.loads` 照收 `NaN`，而 `Delta.capped()` 用 `min/max`——`min(10.0, nan)` 返回
    10.0，于是三根轴一起拿到最大正向 delta，比不分析还糟。
    """

    def build(self, reply: str, **over):
        conf = cfg(
            mood_use_llm_for_delta=True,
            mood_provider_name="p",
            mood_llm_interval_messages=1,
            **over,
        )
        store = ScopeStore(Path(tempfile.mkdtemp()) / "s.json")
        store.load("2026-01-01", conf.cycle_length)
        log = RecordingLogger()
        provider = FakeProvider("p", reply=reply)
        ctx = FakeContext(chat_providers=[provider])
        gateway = LLMGateway(ProviderResolver(ctx, log), lambda: conf, log)
        svc = MoodService(
            store.scope, lambda: conf, None, gateway=gateway, logger=log,
            time_source=lambda: 1_000_000.0,
        )
        return svc, log, provider

    async def test_nan_reply_is_dropped_not_maxed(self):
        svc, log, provider = self.build(
            '{"affection_delta": NaN, "libido_delta": NaN, "aggression_delta": NaN}'
        )
        svc.profile("42")
        delta = await svc._llm_delta("42", ["今天天气不错"])
        self.assertEqual(provider.calls, 1, "模型根本没被调用，这条测试就没盯到夹取那一步")
        self.assertIsNone(delta, "NaN 被当成数值用了")
        self.assertIn("非有限", log.text("warning"))

    async def test_infinity_reply_is_dropped(self):
        svc, _log, provider = self.build(
            '{"affection_delta": Infinity, "libido_delta": 0, "aggression_delta": 0}'
        )
        self.assertIsNone(await svc._llm_delta("42", ["你好"]))
        self.assertEqual(provider.calls, 1)

    async def test_valid_reply_still_applies(self):
        """反向控制：正常数值那条路必须照旧生效。"""
        svc, _log, provider = self.build(
            '{"affection_delta": 5, "libido_delta": 1, "aggression_delta": -2}'
        )
        delta = await svc._llm_delta("42", ["你好"])
        self.assertEqual(provider.calls, 1)
        self.assertIsNotNone(delta)
        self.assertAlmostEqual(delta.affection, 5.0)
        self.assertAlmostEqual(delta.aggression, -2.0)

    async def test_full_chain_does_not_get_the_max_positive_delta(self):
        svc, _log, _provider = self.build(
            '{"affection_delta": NaN, "libido_delta": NaN, "aggression_delta": NaN}'
        )
        before = svc.profile("42")
        start_aff, start_lib = before["affection"], before["libido"]
        delta = await svc.update_from_message_async("42", "今天天气不错")
        self.assertEqual(delta.affection, 0.0, "NaN 变成了正向满分 delta")
        self.assertEqual(delta.libido, 0.0)
        self.assertAlmostEqual(svc.profile("42")["affection"], start_aff, places=4)
        self.assertAlmostEqual(svc.profile("42")["libido"], start_lib, places=4)


class CareIgnoresNonFiniteHistory(unittest.TestCase):
    """`min(1.0, nan)` 返回 1.0：坏值算出来的在意度会被**写回盘上**并钉在满分。"""

    class _Mood:
        def profile(self, user_id):
            return {"affection": 50.0, "aggression": 10.0}

    def _core(self, scope):
        core = type("C", (), {})()
        core.role_id = "bot1"
        core.mood = self._Mood()
        core.config = cfg()
        core.scope = scope
        return core

    def _scope(self, attention: dict) -> RoleScope:
        return RoleScope(
            {"_state_version": 3, "roles": {"bot1": {"self": {}, "users": {
                "42": {"attention": attention},
            }}}},
            "bot1",
            lambda: None,
        )

    def test_nan_care_is_not_written_back_as_full_marks(self):
        scope = self._scope({"care": float("nan"), "care_at": 1_000_000.0})
        service = BehaviorService(self._core(scope))
        value = service.care("42", 2_000_000.0)
        self.assertTrue(math.isfinite(value))
        self.assertLess(value, 0.9, f"NaN 被夹成了 {value}（满分在意度）")
        written = scope.user_state("42")["attention"]
        self.assertLess(written["care"], 0.9, "坏值被原样写回盘上，此后 72 小时都降不下来")
        self.assertTrue(math.isfinite(written["care"]))

    def test_valid_history_still_integrates(self):
        """反向控制：正常的注意力记录照样参与积分。"""
        scope = self._scope({"care": 0.05, "care_at": 1_000_000.0})
        service = BehaviorService(self._core(scope))
        value = service.care("42", 1_100_000.0)
        self.assertGreater(value, 0.05, "正常的在意度没有被往目标值推")
        self.assertEqual(scope.user_state("42")["attention"]["care"], value)

    def test_non_finite_affection_falls_back_to_neutral_target(self):
        service = BehaviorService(self._core(self._scope({"care": 0.5, "care_at": 1.0})))
        broken = service._care_target("42", {"affection": float("nan"), "aggression": float("inf")})
        neutral = service._care_target("42", {"affection": 50.0, "aggression": 15.0})
        self.assertTrue(math.isfinite(broken))
        self.assertAlmostEqual(broken, neutral, places=6, msg="坏值被当成了满分好感/零脾气")


class ShellWithOtherStateIsKept(unittest.TestCase):
    """`spoke_log` 不许让用户条目永远清不掉。"""

    def _service(self):
        conf = cfg()
        store = ScopeStore(Path(tempfile.mkdtemp()) / "s.json")
        store.load("2026-01-01", conf.cycle_length)
        return MoodService(store.scope, lambda: conf, None, time_source=lambda: 1_000_000.0)

    def test_spoke_log_leaves_with_the_mood_profile(self):
        svc = self._service()
        now = 2_000_000_000.0
        svc.profile("42")
        svc.note_spoke("42", "你先忙吧", now=now - 200 * 86400.0, pending=True)
        svc._scope.set_user("42", "last_interaction", now - 200 * 86400.0)
        svc._scope.user_state("42")["mood"]["last_interaction"] = now - 200 * 86400.0
        self.assertEqual(svc.prune_expired(now=now), 1, "情绪档案该被清掉")
        state = svc._scope.user_state("42")
        self.assertNotIn("spoke_log", state, "spoke_log 留着 → 这个条目连 90 天那条路都进不去")
        self.assertEqual(svc._spoke_lines("42"), [], "几个月前的「对方还没回应」还在往外递")
        self.assertEqual(
            svc.prune_expired(now=now + 200 * 86400.0), 1,
            "spoke_log 不在 _STALE_SHELL_FIELDS 里，条目就永远删不掉",
        )
        self.assertNotIn("42", svc._scope.all_user_ids())

    def test_readers_do_not_create_entries_for_said_and_spoke(self):
        svc = self._service()
        before = svc._scope.all_user_ids()
        self.assertEqual(svc.said("stranger"), [])
        self.assertEqual(svc.spoke("stranger"), [])
        self.assertEqual(svc.recall_lines("stranger"), [])
        self.assertEqual(
            svc._scope.all_user_ids(), before, "读一眼她说过什么就给陌生人留了个永久空壳"
        )


class BrokenAxisValueResetsToTheConfiguredInitial(unittest.TestCase):
    """坏掉的轴值不许按「0 分」补齐。

    `_serialize` 把内存里的 NaN 写成 `null`，所以「算术里混进过一次 NaN」的档案下次
    启动读回来就是 `None`。原来 `_repair` 拿 `bounds[0]`（= 0）当兜底，于是表现是
    「好感度一夜之间变成 0」——最低档，而且一个字都不记。
    """

    def _service(self, **over):
        conf = cfg(mood_initial_affection=60, mood_initial_libido=20, **over)
        store = ScopeStore(Path(tempfile.mkdtemp()) / "s.json")
        store.load("2026-01-01", conf.cycle_length)
        svc = MoodService(store.scope, lambda: conf, None, time_source=lambda: 1_000_000.0)
        return svc, store

    def test_broken_axes_fall_back_to_the_configured_initials(self):
        svc, store = self._service()
        record = svc.profile("42")
        record["affection"] = None          # NaN 写盘后的样子
        record["base_affection"] = "坏了"
        record["libido"] = float("nan")
        repaired = svc.profile("42")
        self.assertEqual(repaired["affection"], 60.0, "坏值被补成了最低档")
        self.assertEqual(repaired["base_affection"], 60.0)
        self.assertEqual(repaired["libido"], 20.0)

    def test_a_real_zero_is_left_alone(self):
        """反向控制：用户真的把好感度设成 0 时不许被改成初始值。"""
        svc, _store = self._service()
        record = svc.profile("42")
        record["affection"] = 0.0
        record["base_affection"] = 0.0
        repaired = svc.profile("42")
        self.assertEqual(repaired["affection"], 0.0)
        self.assertEqual(repaired["base_affection"], 0.0)

    def test_out_of_range_still_clamps_to_the_bound(self):
        svc, _store = self._service()
        record = svc.profile("42")
        record["affection"] = 999.0         # 数值本身是好的，只是越界 → 夹到上界
        self.assertEqual(svc.profile("42")["affection"], 100.0)


class EmptyUserShellIsPruned(unittest.TestCase):
    """纯空壳 `{}` 永远清不掉：旧版本 `get_user` 在群里给每个陌生人留下的就是它。"""

    def _service(self):
        conf = cfg()
        store = ScopeStore(Path(tempfile.mkdtemp()) / "s.json")
        store.load("2026-01-01", conf.cycle_length)
        return MoodService(store.scope, lambda: conf, None, time_source=lambda: 1_000_000.0)

    def test_empty_shell_is_deleted_while_real_records_stay(self):
        svc = self._service()
        svc._scope.user_state("ghost")            # 只有 {}，不能有任何东西可丢
        svc._scope.user_state("ghost2")
        svc.profile("42")                          # 有情绪档案的人
        svc._scope.set_user("43", "nickname", "阿哲")
        pruned = svc.prune_expired()
        self.assertEqual(pruned, 2, "空壳没有被清掉")
        self.assertNotIn("ghost", svc._scope.all_user_ids())
        self.assertNotIn("ghost2", svc._scope.all_user_ids())
        self.assertIn("42", svc._scope.all_user_ids())
        self.assertIn("43", svc._scope.all_user_ids(), "有称呼的条目不该被当成空壳")
        self.assertIsInstance(
            svc._scope.peek_user("42", "mood"), dict, "真档案被连带删了"
        )

    def test_shell_without_timestamp_but_with_data_is_kept(self):
        """反向控制：有数据但判不出时间的条目仍然保留（宁可不清也不误删）。"""
        svc = self._service()
        svc._scope.set_user("43", "nickname", "阿哲")
        self.assertEqual(svc.prune_expired(), 0)
        self.assertIn("43", svc._scope.all_user_ids())


if __name__ == "__main__":
    unittest.main()
