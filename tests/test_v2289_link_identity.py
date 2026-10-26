"""v2.28.9：联动出口翻新——名片名/称呼供给 + 契约与门面 brief 扩充。

## 为什么

用户原话：「两个插件联动主动消息从 core 拿到的消息不全，而且 core 可以给它的信息
却很少，这是事实；而且主动消息从 core 那拿不到名称，以至于根本不会叫人」「要加的
得加，顺带翻新一下」。

社交层（自主拟人社交）跑主动消息时，原来只能从 Core 拿到「她自己的状态」：
不知道她是谁（参照名/人设）、不知道她穿什么，也不知道对面这个人叫什么——
`nickname` 只在 `/叫我` 或自动认定成功时才有，而自动认定门槛很高（含数字/emoji/
装饰的名片一律不认），于是大多数用户 `nickname` 是空，主动消息**从来不叫名字**。

这一版补三块，全部**只加不改**（契约仍 v2，API 仍 v2）：

1. `mood` 新增 `display_name`（消息自带名片名，原样清洗后存 user 级）；
2. 契约新增 `identity` / `outfit` / `paths.user_display_name` 与对应能力；
3. 门面 `brief()` 所有 task 都带 `her` / `outfit` / `weather` / `holiday` /
   `routine`，且带 user_id 时带 `user` 块（称呼回退链 / 情绪 / 注意力 / 原话 /
   亲密档案）。

## 本文件盯的

* 名片名写入、清洗、**绝不覆盖 nickname**、相同值不写盘、prune 后仍在；
* `call_name` 回退链 nickname → display_name → ""（**绝不编名字**）；
* 契约 identity/outfit/paths/capabilities，fields_hash 变了但 schema 版本不动；
* brief 三个 task 的公共块、user 块字段齐全、said 顺序与上限、attention 兜底、
  intimacy 的 `want_line` 仍受 `ccb_proactive` 控制；
* 没名字的老数据：`call_name` 是空串，不会出现 role_id / 用户 id 被当成名字。
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
from humanoid.config import HumanoidConfig
from humanoid.core_instance import HumanoidCoreInstance
from humanoid.link import (
    CONTRACT_CAPABILITIES,
    CONTRACT_SCHEMA,
    CONTRACT_VERSION,
    _fields_hash,
    _identity_block,
    _outfit_block,
    build_contract,
)
from humanoid.role_scope import RoleScope
from humanoid.services.mood import (
    DISPLAY_NAME_MAX_CHARS,
    _STALE_SHELL_FIELDS,
    MoodService,
    clean_display_name,
    pick_display_name,
)
from humanoid.state import StateStore

from .fakes import FakeContext, FrozenClock, RecordingLogger

TZ = ZoneInfo("Asia/Shanghai")
MOMENT = datetime(2026, 8, 22, 7, 10, tzinfo=TZ)
TODAY = "2026-08-22"
#: `user` 块的固定字段（只加不改：加字段时这里一起改）。
USER_KEYS = {
    "user_id", "nickname", "nickname_source", "display_name",
    "call_name", "name_source",
    "affection", "base_affection", "affection_delta",
    "label", "mood_tag", "aggression", "libido",
    "attention", "first_met", "known_days", "last_interaction",
    "said", "intimacy",
}


# ────────────────────────── 构造工具 ──────────────────────────


def cfg(**kw) -> HumanoidConfig:
    return HumanoidConfig.from_raw({"timezone_city": "北京", **kw})


def make_core(**conf) -> HumanoidCoreInstance:
    """一份可运行的最小 Core（照 tests/test_v2280_contract.py 的口径）。"""
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
    for name in ("schedule", "soma", "energy", "social", "process", "mood", "weather"):
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
    return core


class _Roles:
    """只给门面用的最小 RoleManager。"""

    def __init__(self, cores):
        self._cores = list(cores)

    def get_all(self):
        return list(self._cores)


class _MutableTime:
    """可推进的 epoch 时间源（名片名时间戳/合并规则要看得出来）。"""

    def __init__(self, value: float = 1_000_000.0) -> None:
        self.value = float(value)

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += float(seconds)


def make_mood():
    """一个只依赖 RoleScope 的 MoodService：名片名/称呼/清理都在这里测。"""
    root: dict = {}
    scope = RoleScope(root, "default")
    marks: list[int] = []
    scope.set_mark_dirty(lambda: marks.append(1))
    clock = _MutableTime()
    service = MoodService(scope, lambda: HumanoidConfig(), time_source=clock)
    return service, root, scope, clock, marks


def seed_user(core: HumanoidCoreInstance, uid: str = "10001") -> None:
    """一个「什么都有」的用户：称呼、名片名、情绪、状态标签、原话、认识时间。"""
    core.mood.set_nickname(uid, "小灵", "user")
    core.mood.note_display_name(uid, "阿\n哲")
    record = core.mood.profile(uid)
    now = core.now_epoch()
    record.update({
        "affection": 72.5,
        "base_affection": 46.0,
        "libido": 42.0,
        "aggression": 18.0,
        "last_interaction": now - 3600.0,
    })
    core.scope.update_user(
        uid,
        first_met=now - 10 * 86400.0,
        last_interaction=now - 3600.0,
        mood_tag="想被陪",
        said=[
            {"said": f"第{i}句原话", "at": now - i * 100.0} for i in range(6)
        ],
    )


# ────────────────────── 名片名：清洗与写入 ──────────────────────


class DisplayNameCleanTest(unittest.TestCase):
    def test_strips_and_collapses_control_chars(self):
        self.assertEqual(clean_display_name("  小雨  ", "1"), "小雨")
        self.assertEqual(clean_display_name("小\n雨", "1"), "小 雨")
        self.assertEqual(clean_display_name("小\t\t雨", "1"), "小 雨")
        self.assertEqual(clean_display_name("小 \n\t 雨", "1"), "小 雨")
        self.assertEqual(clean_display_name("小\x00雨", "1"), "小 雨")

    def test_truncates_to_the_limit(self):
        name = "阿" * (DISPLAY_NAME_MAX_CHARS + 20)
        self.assertEqual(len(clean_display_name(name, "1")), DISPLAY_NAME_MAX_CHARS)

    def test_empty_and_symbol_only_are_ignored(self):
        for raw in ("", "   ", "\n\t", "★★★", "❤️", "（ ）"):
            self.assertEqual(clean_display_name(raw, "1"), "", repr(raw))

    def test_qq_number_is_not_a_name(self):
        # 没设昵称时 get_sender_name 返回的就是 QQ 号：等于 user_id 的纯数字不算名字。
        self.assertEqual(clean_display_name("3632823490", "3632823490"), "")
        self.assertEqual(clean_display_name(" 3632823490 ", "3632823490"), "")
        # 不含字母/汉字的纯数字串即使不是 user_id 也留着（名片原样，交给社交层判断），
        # 但至少别把 QQ 号本身当名字。
        self.assertEqual(clean_display_name("2024", "10001"), "2024")

    def test_decorated_card_names_are_kept(self):
        # 名片原样：装饰符号是用户自己的选择，清洗只负责脏字符。
        self.assertEqual(clean_display_name("★小鱼★", "1"), "★小鱼★")
        self.assertEqual(clean_display_name("阿哲 2 号", "1"), "阿哲 2 号")


class DisplayNameServiceTest(unittest.TestCase):
    def setUp(self):
        self.service, self.root, self.scope, self.clock, self.marks = make_mood()
        self.users = self.root["roles"]["default"]["users"]

    def test_write_and_timestamp(self):
        self.assertEqual(self.service.display_name("7"), "", "没写过就是空串")
        self.assertEqual(self.service.note_display_name("7", "  阿\n哲  "), "阿 哲")
        self.assertEqual(self.service.display_name("7"), "阿 哲")
        self.assertEqual(self.service.display_name_at("7"), self.clock.value)
        self.assertEqual(len(self.marks), 1, "写入该标脏一次")

    def test_same_value_does_not_write(self):
        self.service.note_display_name("7", "阿哲")
        before = len(self.marks)
        self.service.note_display_name("7", "  阿哲  ")
        self.assertEqual(len(self.marks), before, "洗净后相同就不该再写盘")
        self.service.note_display_name("7", "")
        self.assertEqual(len(self.marks), before, "空串更不该覆盖已有的名字")

    def test_never_overwrites_nickname(self):
        self.service.set_nickname("7", "小灵", "user")
        self.service.note_display_name("7", "阿哲")
        self.assertEqual(self.service.nickname("7"), "小灵", "nickname 是「她认定的称呼」")
        self.assertEqual(self.service.display_name("7"), "阿哲", "display_name 是「名片原样」")
        self.assertEqual(self.service.call_name("7"), "小灵")

    def test_reads_do_not_create_entries(self):
        for call in (self.service.display_name, self.service.call_name,
                     self.service.name_source, self.service.display_name_at):
            call("42")
        self.assertEqual(self.users, {}, "纯查询不该建用户条目")

    def test_call_name_fallback_chain(self):
        self.assertEqual(self.service.call_name("7"), "")
        self.assertEqual(self.service.name_source("7"), "")
        self.service.note_display_name("7", "阿哲")
        self.assertEqual(self.service.call_name("7"), "阿哲")
        self.assertEqual(self.service.name_source("7"), "display_name")
        self.service.set_nickname("7", "小灵", "user")
        self.assertEqual(self.service.call_name("7"), "小灵", "昵称优先")
        self.assertEqual(self.service.name_source("7"), "nickname")

    def test_junk_stored_value_is_not_a_name(self):
        # state.json 被手改/旧版本写坏：对象不是名字，读出空串，绝不念一个 dict 出来。
        self.scope.set_user("7", "display_name", {"坏": 1})
        self.assertEqual(self.service.display_name("7"), "")
        self.assertEqual(self.service.call_name("7"), "")
        self.assertEqual(self.service.name_source("7"), "")


class DisplayNameMergeTest(unittest.TestCase):
    """群聊/私聊各记一份时的合并：「取最长/最新」，空串绝不覆盖已有名字。"""

    def test_pick_rules(self):
        self.assertEqual(pick_display_name("", 0.0, "阿哲", 5.0), "阿哲")
        self.assertEqual(pick_display_name("阿哲", 10.0, "", 20.0), "阿哲", "空串不能覆盖")
        self.assertEqual(pick_display_name("旧名字", 100.0, "新", 200.0), "新", "新的优先")
        self.assertEqual(pick_display_name("旧名字", 200.0, "新", 100.0), "旧名字")
        self.assertEqual(pick_display_name("短", 0.0, "长的名字", 0.0), "长的名字",
                         "时间戳不可信时取信息更全的那份")
        self.assertEqual(pick_display_name("一样", 0.0, "一样", 0.0), "一样")

    def test_new_observation_wins_over_old(self):
        service, _root, _scope, clock, _marks = make_mood()
        service.note_display_name("7", "阿哲")
        first_at = service.display_name_at("7")
        clock.advance(100.0)
        service.note_display_name("7", "阿哲哲")
        self.assertEqual(service.display_name("7"), "阿哲哲")
        self.assertGreater(service.display_name_at("7"), first_at)

    def test_a_newer_record_from_a_merge_is_not_downgraded(self):
        # 合并进来的另一份「更新」记录（时间戳在将来）：旧的观察不能把它盖掉。
        service, _root, scope, clock, _marks = make_mood()
        scope.update_user("7", display_name="未来的名字",
                          display_name_at=clock.value + 10_000.0)
        service.note_display_name("7", "现在的名字")
        self.assertEqual(service.display_name("7"), "未来的名字")

    def test_display_fields_survive_prune(self):
        self.assertIn("display_name", _STALE_SHELL_FIELDS)
        self.assertIn("display_name_at", _STALE_SHELL_FIELDS)
        service, root, _scope, _clock, _marks = make_mood()
        service.note_display_name("7", "阿哲")
        service.set_nickname("7", "小灵")
        service.profile("7")
        users = root["roles"]["default"]["users"]
        users["7"]["last_interaction"] = 0.0  # 远古时间 → 情绪档案过期
        self.assertEqual(service.prune_expired(now=1_000_000.0), 1)
        self.assertEqual(users["7"].get("display_name"), "阿哲", "过期清理不能把名字丢掉")
        self.assertGreater(users["7"].get("display_name_at", 0.0), 0.0)
        self.assertEqual(users["7"].get("nickname"), "小灵")
        self.assertNotIn("mood", users["7"], "情绪档案该清回初始值")

    def test_stale_shell_keeps_display_name_but_not_forever(self):
        """只剩名片名的条目与只剩 nickname 的条目同一条保留策略。"""
        service, root, _scope, _clock, _marks = make_mood()
        service.note_display_name("ghost", "小明")
        users = root["roles"]["default"]["users"]
        # 判不出时间戳：宁可不清也不误删（与 nickname 同侧）。
        self.assertEqual(service.prune_expired(now=1_000_000.0), 0)
        self.assertIn("ghost", users)
        # 90 天不活跃的静默期（配置默认 30 天）到了，整条删除。
        users["ghost"]["last_interaction"] = 1_000_000.0 - 100 * 86400.0
        self.assertEqual(service.prune_expired(now=1_000_000.0), 1)
        self.assertNotIn("ghost", users)


# ────────────────────── 契约：identity / outfit ──────────────────────


class ContractIdentityOutfitTest(unittest.TestCase):
    def setUp(self):
        self.core = make_core()
        self.core.scope.set_self("char_name_override", "小满")
        self.core.scope.set_self("schedule_persona", "温柔的夜猫子")
        self.core.scope.set_self("outfit_override", "白色蕾丝连衣裙")
        self.contract = build_contract(self.core)

    def test_identity_block(self):
        self.assertEqual(
            self.contract["identity"],
            {"char_name": "小满", "persona_label": "温柔的夜猫子"},
        )

    def test_outfit_block_shape(self):
        outfit = self.contract["outfit"]
        self.assertEqual(set(outfit), {"enabled", "text", "source"})
        self.assertIsInstance(outfit["enabled"], bool)
        self.assertIsInstance(outfit["text"], str)
        self.assertIsInstance(outfit["source"], str)
        self.assertIn("白色蕾丝连衣裙", outfit["text"])

    def test_outfit_off_is_still_a_block(self):
        contract = build_contract(make_core(outfit_enabled=False))
        self.assertEqual(set(contract["outfit"]), {"enabled", "text", "source"})
        self.assertFalse(contract["outfit"]["enabled"])
        self.assertEqual(contract["outfit"]["text"], "")

    def test_paths_and_capabilities_only_added(self):
        paths = self.contract["paths"]
        self.assertEqual(
            paths["user_display_name"], "roles.<bid>.users.<uid>.display_name"
        )
        for legacy in ("user_affection", "user_nickname",
                       "user_last_interaction", "user_said"):
            self.assertIn(legacy, paths, f"旧路径不能丢：{legacy}")
        for old in ("body", "feelings", "form", "activity", "day", "ongoing", "routine",
                    "dreams", "wake", "event"):
            self.assertIn(old, CONTRACT_CAPABILITIES)
        self.assertIn("identity", CONTRACT_CAPABILITIES)
        self.assertIn("outfit", CONTRACT_CAPABILITIES)
        self.assertIn("identity", self.contract["capabilities"])
        self.assertIn("outfit", self.contract["capabilities"])

    def test_schema_version_unchanged_but_fields_hash_moved(self):
        self.assertEqual(CONTRACT_VERSION, 2)
        self.assertEqual(CONTRACT_SCHEMA, "humanoid.contract/2")
        self.assertEqual(self.contract["v"], 2)
        self.assertEqual(self.contract["schema"], CONTRACT_SCHEMA)
        # 新块进了结构指纹：去掉 identity/outfit 后哈希必然不同（只加不改也得让
        # 对面的结构校验看得到变化）。
        baseline = {k: v for k, v in self.contract.items()
                    if k not in ("identity", "outfit")}
        self.assertNotEqual(_fields_hash(baseline), self.contract["fields_hash"])
        self.assertTrue(str(self.contract["fields_hash"]).startswith("sha256:"))
        # 同一份状态两次生成必须同指纹（取值在变、结构不变）。
        self.assertEqual(build_contract(self.core)["fields_hash"],
                         self.contract["fields_hash"])

    def test_broken_core_still_builds_a_contract(self):
        """拿不到 char_name/outfit 时契约照常生成，新块兜底为空——绝不因此失败。"""
        core = make_core()

        def boom(*_args, **_kwargs):
            raise RuntimeError("boom")

        core.char_name = boom
        core.persona_label = boom
        core.outfit_info = boom
        contract = build_contract(core)
        self.assertEqual(contract["identity"], {"char_name": "", "persona_label": ""})
        self.assertEqual(contract["outfit"], {})
        self.assertEqual(contract["v"], 2)

    def test_block_helpers_degrade(self):
        self.assertEqual(_identity_block(SimpleNamespace()),
                         {"char_name": "", "persona_label": ""})
        self.assertEqual(_outfit_block(SimpleNamespace()), {})
        self.assertEqual(
            _outfit_block(SimpleNamespace(
                outfit_info=lambda: (_ for _ in ()).throw(RuntimeError("boom")))),
            {},
        )
        self.assertEqual(_outfit_block(SimpleNamespace(outfit_info=lambda: "坏")), {})
        cleaned = _outfit_block(SimpleNamespace(outfit_info=lambda: {
            "enabled": True, "kind": "女", "text": "白衬衫", "source": "手写",
            "没见过的键": 1,
        }))
        self.assertEqual(cleaned, {"enabled": True, "text": "白衬衫", "source": "手写"})


# ────────────────────── 门面：brief 公共块 ──────────────────────


class FacadeBriefSharedBlocksTest(unittest.TestCase):
    def setUp(self):
        self.core = make_core()
        self.core.scope.set_self("char_name_override", "小满")
        self.core.scope.set_self("schedule_persona", "温柔的夜猫子")
        self.core.scope.set_self("outfit_override", "白色蕾丝连衣裙")
        self.core.refresh_contract()
        self.facade = CoreFacade(_Roles([self.core]))
        self.contract = self.core.scope.get_self("contract")

    def test_all_tasks_have_her_outfit_weather_holiday_routine(self):
        for task in ("chat", "proactive", "status"):
            brief = self.facade.brief("bot1", "10001", task=task)
            self.assertIsNotNone(brief, task)
            for key in ("her", "outfit", "weather", "holiday", "routine"):
                self.assertIn(key, brief, f"{task} 缺 {key}")
            self.assertEqual(brief["her"], self.contract["identity"], task)
            self.assertEqual(set(brief["outfit"]), {"enabled", "text", "source"})
            self.assertIn("白色蕾丝连衣裙", brief["outfit"]["text"])
            self.assertEqual(brief["weather"], self.contract["weather"], task)
            self.assertEqual(brief["holiday"], self.contract["holiday"], task)
            self.assertEqual(brief["routine"], self.contract["routine"], task)

    def test_events_behavior_unchanged(self):
        chat = self.facade.brief("bot1", "10001", task="chat")
        proactive = self.facade.brief("bot1", "10001", task="proactive")
        status = self.facade.brief("bot1", "10001", task="status")
        self.assertNotIn("events", chat, "chat 任务不该多带事件，省预算")
        self.assertIn("events", proactive)
        self.assertIn("events", status)
        # status 仍保留旧字段（只加不改，老社交层还在读它）。
        self.assertIn("relationship", status)
        self.assertNotIn("relationship", proactive)

    def test_her_falls_back_to_live_core(self):
        # 模拟旧 Core 落盘的契约：没有 identity 块。
        self.core.scope.set_self("contract", {"v": 2, "body": {}})
        brief = self.facade.brief("bot1", "10001")
        self.assertEqual(brief["her"]["char_name"], "小满")
        self.assertEqual(brief["her"]["persona_label"], "温柔的夜猫子")
        self.assertEqual(brief["routine"], {}, "契约里没有作息就给空 dict")
        self.assertEqual(brief["weather"], "")

    def test_no_user_id_has_no_user_block(self):
        brief = self.facade.brief("bot1", task="chat")
        self.assertNotIn("user", brief)

    def test_capabilities_and_api_version(self):
        self.assertEqual(API_VERSION, 2)
        for key in ("identity", "user"):
            self.assertIn(key, CAPABILITIES)
        self.assertEqual(list(self.facade.capabilities()), list(CAPABILITIES))


# ────────────────────── 门面：brief 的 user 块 ──────────────────────


class FacadeUserBlockTest(unittest.TestCase):
    def setUp(self):
        self.core = make_core()
        self.core.scope.set_self("char_name_override", "小满")
        self.core.scope.set_self("schedule_persona", "温柔的夜猫子")
        self.core.refresh_contract()
        self.facade = CoreFacade(_Roles([self.core]))
        self.now = self.core.now_epoch()
        seed_user(self.core, "10001")

    def _user(self, task: str = "proactive", uid: str = "10001"):
        return self.facade.brief("bot1", uid, task=task)["user"]

    def test_user_block_fields_complete(self):
        user = self._user()
        self.assertEqual(set(user), USER_KEYS)
        self.assertEqual(user["user_id"], "10001")
        self.assertEqual(user["nickname"], "小灵")
        self.assertEqual(user["nickname_source"], "user")
        self.assertEqual(user["display_name"], "阿 哲")
        self.assertEqual(user["call_name"], "小灵")
        self.assertEqual(user["name_source"], "nickname")
        self.assertEqual(user["affection"], 72.5)
        self.assertEqual(user["base_affection"], 46.0)
        self.assertEqual(user["affection_delta"], 26.5)
        self.assertIsInstance(user["label"], str)
        self.assertTrue(user["label"], "情绪标签不该是空串")
        self.assertEqual(user["mood_tag"], "想被陪")
        self.assertEqual(user["aggression"], 18.0)
        self.assertEqual(user["libido"], 42.0)
        self.assertIsInstance(user["attention"], dict)
        self.assertEqual(set(user["attention"]), {"care", "focus", "spare"})
        for axis in ("care", "focus", "spare"):
            self.assertGreaterEqual(user["attention"][axis], 0.0)
            self.assertLessEqual(user["attention"][axis], 1.0)
        self.assertAlmostEqual(user["first_met"], self.now - 10 * 86400.0, places=1)
        self.assertEqual(user["known_days"], 10)
        self.assertAlmostEqual(user["last_interaction"], self.now - 3600.0, places=1)
        self.assertIsInstance(user["intimacy"], dict)

    def test_user_block_present_for_every_task(self):
        for task in ("chat", "proactive", "status"):
            brief = self.facade.brief("bot1", "10001", task=task)
            self.assertIn("user", brief, task)
            self.assertEqual(brief["user"]["user_id"], "10001", task)

    def test_task_status_relationship_reuses_the_same_user_data(self):
        brief = self.facade.brief("bot1", "10001", task="status")
        self.assertEqual(brief["relationship"]["affection"], brief["user"]["affection"])
        self.assertEqual(brief["relationship"]["base_affection"],
                         brief["user"]["base_affection"])
        self.assertEqual(brief["relationship"]["nickname"], brief["user"]["nickname"])
        self.assertEqual(brief["relationship"]["last_interaction"],
                         brief["user"]["last_interaction"])

    def test_call_name_falls_back_to_display_name(self):
        # 有名片名、但还没有「认定称呼」的人：这正是大多数用户的样子。
        self.core.mood.note_display_name("20002", "阿哲")
        user = self._user(uid="20002")
        self.assertEqual(user["nickname"], "")
        self.assertEqual(user["display_name"], "阿哲")
        self.assertEqual(user["call_name"], "阿哲")
        self.assertEqual(user["name_source"], "display_name")

    def test_said_newest_first_and_capped(self):
        user = self._user()
        said = user["said"]
        self.assertEqual(len(said), 5, "最多 5 条")
        self.assertEqual([row["text"] for row in said],
                         [f"第{i}句原话" for i in range(5)])
        self.assertEqual([row["at"] for row in said],
                         sorted((row["at"] for row in said), reverse=True))

    def test_said_tolerates_junk(self):
        self.core.scope.update_user("30003", said=[
            {"said": "有时间的", "at": self.now},
            {"said": "没时间的", "at": "坏"},
            {"text": "只有 text 键的", "at": self.now - 5.0},
            "字符串", None, {"said": "   "},
        ])
        said = self._user(uid="30003")["said"]
        self.assertEqual([row["text"] for row in said],
                         ["有时间的", "只有 text 键的", "没时间的"])

    def test_attention_is_empty_for_users_without_a_profile(self):
        user = self._user(uid="40004")
        self.assertEqual(user["attention"], {}, "没情绪档案的人不该被算注意力")

    def test_attention_degrades_on_broken_behavior(self):
        original = self.core.behavior
        try:
            self.core.behavior = object()  # 缺 care/_focus/_spare
            user = self._user()
        finally:
            self.core.behavior = original
        self.assertEqual(user["attention"], {}, "注意力拿不到就给空 dict")

    def test_brief_does_not_create_user_entries(self):
        core = make_core()
        facade = CoreFacade(_Roles([core]))
        before = set(core.scope.all_user_ids())
        facade.brief("bot1", "50005", task="chat")
        facade.brief("bot1", "50005", task="proactive")
        self.assertEqual(set(core.scope.all_user_ids()), before,
                         "门面读一眼不该给陌生人建条目")

    def test_unknown_role_and_old_core_are_still_safe(self):
        self.assertIsNone(self.facade.brief("不存在的角色", "10001"))

        class Broken:
            role_id = "bot1"

            def __getattr__(self, name):
                raise RuntimeError("boom")

        facade = CoreFacade(_Roles([Broken()]))
        self.assertIsNone(facade.brief("bot1", "10001"))


class FacadeIntimacyAuthorizationTest(unittest.TestCase):
    """`user.intimacy` 直接复用 `facade.intimacy()`：want_line 的授权逻辑原样不动。"""

    def _core(self, proactive: bool) -> HumanoidCoreInstance:
        core = make_core(ccb=True, ccb_proactive=proactive)
        from humanoid import ccb

        core.mood.profile("10001")
        now = core.now_epoch()
        core.scope.user_state("10001").update({
            "ccb_done_count": 2.0,
            "ccb_satisfy": 70.0,
            "ccb_last_at": now,
            "ccb_stage": 1.0,
            "ccb_day": float(ccb._day_index(now)),
        })
        return core

    def test_want_line_off_by_default(self):
        core = self._core(proactive=False)
        facade = CoreFacade(_Roles([core]))
        user = facade.brief("bot1", "10001", task="proactive")["user"]
        self.assertEqual(user["intimacy"]["want_line"], "",
                         "ccb_proactive 没开就不该给「她这会儿想不想」")
        self.assertEqual(user["intimacy"]["done_count"], 2.0)

    def test_want_line_only_when_authorized(self):
        core = self._core(proactive=True)
        facade = CoreFacade(_Roles([core]))
        user = facade.brief("bot1", "10001", task="proactive")["user"]
        self.assertTrue(user["intimacy"]["want_line"])
        # 直接复用同一个出口：两边逐字段一致（门面不同接口不能各说各话）。
        self.assertEqual(user["intimacy"], facade.intimacy("bot1", "10001"))


# ────────────────────── 老数据：没有名字就绝不编 ──────────────────────


class LegacyNoNameTest(unittest.TestCase):
    def test_old_data_without_names_has_empty_call_name(self):
        core = make_core()
        facade = CoreFacade(_Roles([core]))
        core.mood.profile("60006")  # 老用户：有情绪档案，但没 nickname/display_name
        user = facade.brief("bot1", "60006", task="proactive")["user"]
        self.assertEqual(user["nickname"], "")
        self.assertEqual(user["display_name"], "")
        self.assertEqual(user["call_name"], "", "没有名字就必须是空串，绝不编")
        self.assertEqual(user["name_source"], "")
        for value in (user["call_name"], user["display_name"], user["nickname"]):
            self.assertNotIn("60006", value, "用户 id 不是名字")
            self.assertNotEqual(value, "bot1", "role_id 不是名字")

    def test_legacy_nickname_only_is_enough(self):
        core = make_core()
        facade = CoreFacade(_Roles([core]))
        core.mood.set_nickname("60007", "老数据称呼", "user")
        user = facade.brief("bot1", "60007", task="chat")["user"]
        self.assertEqual(user["call_name"], "老数据称呼")
        self.assertEqual(user["name_source"], "nickname")
        self.assertEqual(user["display_name"], "", "昵称与名片名是两回事")


if __name__ == "__main__":
    unittest.main()
