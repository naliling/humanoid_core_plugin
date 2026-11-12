"""v3.1.38：判定别那么脆、天气别那么爱说、面板能兜底。

三件事，对应三条用户反馈：

1. **判定太依赖关键词**。用户原话「对某个角色的判定太直接太少太依赖了」。这一版
   补了「半龙人 / 人鱼 / 兽人 / 半兽人 / 凤凰 / 海妖 / 僵尸…」这批以前漏掉的写法，
   并把「有手的非人」单独钉成人形身体——它们不是人类，但确实有手指。
2. **角色太爱说天气**。判据从「变了 + 4 小时」收紧成「**变天**（雨雪风雾或真的
   冷热）+ 8 小时」，温度漂移不算变天；平常天气（晴/多云/阴）根本不给模型看见。
3. **面板兜底**。`ccb_body` 手填身体类型，压过自动判定——「她是兽人但有手」这种
   靠词表永远猜不准的，用户填一格就定了。
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from humanoid import species
from humanoid.config import HumanoidConfig
from humanoid.core_instance import HumanoidCoreInstance
from humanoid.persona import PersonaSource
from humanoid.services import life_material as lm
from humanoid.services import role_materials as rm
from humanoid.services.weather import WEATHER_REPEAT_SECONDS, notable_weather
from humanoid.state import StateStore
from tests.fakes import FrozenClock, RecordingLogger

MOMENT = datetime(2026, 9, 26, 13, 0)

PONY_PERSONA = "你是小马利亚，纯白双翼的独角小马。你住在大树图书馆，喜欢看书。"


def _core(persona: str, raw: dict | None = None) -> HumanoidCoreInstance:
    conf = HumanoidConfig.from_raw({"timezone_city": "北京", **(raw or {})})
    store = StateStore(Path(tempfile.mkdtemp()) / "s.json", 0.01)
    store.load(MOMENT.strftime("%Y-%m-%d"), 28)

    class _M:
        async def resolve_selected_persona(self, **_k):
            return ("p", {"name": "", "prompt": persona, "begin_dialogs": []}, None, False)

        async def get_default_persona_v3(self):
            return {"name": "", "prompt": persona, "begin_dialogs": []}

    class _C:
        persona_manager = _M()
        conversation_manager = None

        def get_config(self, *_a, **_k):
            return None

    source = PersonaSource(_C(), lambda: RecordingLogger())
    core = HumanoidCoreInstance(
        role_id="bot1", state_store=store, config_provider=lambda: conf,
        logger=RecordingLogger(), stop_event=asyncio.Event(),
        resolver=_C(), gateway=None, persona_source=source,
    )
    asyncio.run(source.persona("bot1"))
    core.clock = FrozenClock(MOMENT)
    for name in ("schedule", "soma", "energy", "social", "process", "mood"):
        service = getattr(core, name, None)
        if service is not None and hasattr(service, "_clock"):
            service._clock = core.clock
    return core


class DetectRobustTest(unittest.TestCase):
    """以前判不出来的写法，现在要有结论；结论要落到身体类型上。"""

    def test_half_dragon_is_humanoid(self):
        profile = species.detect("你是半龙人，头上有一对龙角，鳞片从脖颈往下长。")
        self.assertEqual(profile["label"], "半龙人")
        self.assertEqual(species.profile_body(profile), species.BODY_HUMAN)

    def test_mermaid_has_hands(self):
        profile = species.detect("她是人鱼，下半身是鱼尾，头发很长。")
        self.assertEqual(profile["label"], "人鱼")
        self.assertEqual(species.profile_body(profile), species.BODY_HUMAN)

    def test_beastfolk_is_half_animal_person(self):
        for text in ("你是兽人，有兽耳和尾巴。", "你是半兽人，力气很大。", "你是一个狼人。"):
            profile = species.detect(text)
            self.assertEqual(profile["kind"], species.KIND_BEAST_EAR, text)
            self.assertEqual(species.profile_body(profile), species.BODY_HUMAN, text)

    def test_new_species_are_recognized(self):
        self.assertEqual(species.detect("你是一只凤凰，翅膀是金色的。")["label"], "凤凰")
        self.assertEqual(species.detect("你是朱雀。")["label"], "朱雀")
        self.assertEqual(species.detect("你是海妖，尾巴是银色的。")["label"], "海妖")
        self.assertEqual(species.detect("你是一个僵尸。")["label"], "僵尸")
        self.assertEqual(species.detect("你是亡灵。")["label"], "亡灵")

    def test_bird_body_is_paw_not_feather(self):
        profile = species.detect("你是一只凤凰，翅膀是金色的。")
        self.assertEqual(species.profile_body(profile), species.BODY_PAW)

    def test_pony_still_wins(self):
        profile = species.detect(PONY_PERSONA)
        self.assertEqual(profile["kind"], species.KIND_PONY)
        self.assertEqual(species.profile_body(profile), species.BODY_HOOF)

    def test_profile_body_prefers_explicit_body(self):
        self.assertEqual(
            species.profile_body({"kind": "beast", "traits": ["鳞片"], "body": "human"}),
            species.BODY_HUMAN,
        )
        # 脏值不认，退回按 kind + traits 推
        self.assertEqual(
            species.profile_body({"kind": "pony", "body": "bird"}), species.BODY_HOOF
        )


class WeatherChattinessTest(unittest.TestCase):
    """天气：值得说的只有变天；平常天气不给模型看见。"""

    def test_notable_only_for_real_weather(self):
        for text in ("小雨，气温 9℃", "大雪，气温 -2℃", "大风", "雾", "霾", "沙尘"):
            self.assertTrue(notable_weather(text), text)
        for text in ("多云，气温 22℃", "晴，气温 25℃", "阴，气温 18℃", ""):
            self.assertFalse(notable_weather(text), text)

    def test_repeat_window_is_shared_with_the_gate(self):
        """类属性和模块常量必须是同一个值——两处各写一份，改了一处等于没改。"""
        from humanoid.prompt_builder import PromptBuilder

        self.assertEqual(PromptBuilder.WEATHER_REPEAT_SECONDS, WEATHER_REPEAT_SECONDS)
        self.assertGreaterEqual(WEATHER_REPEAT_SECONDS, 8 * 3600.0)

    def test_contract_drops_plain_weather(self):
        from humanoid.link import _contract_weather

        self.assertEqual(
            _contract_weather({"weather": "多云", "env": "天气：多云，气温 12℃", "notice": False}),
            "",
        )
        self.assertEqual(
            _contract_weather({"weather": "小雨", "env": "天气：小雨，气温 12℃", "notice": False}),
            "天气：小雨，气温 12℃",
        )


class RoleMaterialsPromptTest(unittest.TestCase):
    """素材生成时就把身体条件说清楚，而不是事后过滤。"""

    def test_human_prompt_is_unchanged(self):
        prompt = rm.build_prompt("她是一个图书管理员。")
        self.assertNotIn("【她的身体】", prompt)
        self.assertIn("只输出 JSON 数组", prompt)

    def test_hoof_prompt_says_no_hands(self):
        prompt = rm.build_prompt("你是小马。", species="pony")
        self.assertIn("【她的身体】", prompt)
        self.assertIn("没有手", prompt)
        self.assertIn("不穿衣服", prompt)

    def test_paw_and_scale_prompts_differ(self):
        paw = rm.build_prompt("你是一只狼。", species="beast")
        scale = rm.build_prompt("你是一条龙。", species={"kind": "beast", "traits": ["鳞片"]})
        self.assertIn("有爪没有手", paw)
        self.assertIn("鳞片不是皮毛", scale)
        self.assertNotEqual(paw, scale)

    def test_fingerprint_includes_body(self):
        text = "她是一个安静的图书管理员。"
        self.assertNotEqual(
            rm.persona_fingerprint(text, "pony"),
            rm.persona_fingerprint(text, "beast"),
        )
        self.assertEqual(
            rm.persona_fingerprint(text, "pony"),
            rm.persona_fingerprint(text, {"kind": "pony"}),
        )
        self.assertEqual(rm.persona_fingerprint("   "), "")


class BodyOverridePanelTest(unittest.TestCase):
    """面板 ccb_body：用户一句话压过自动判定。"""

    def test_config_parses_body(self):
        conf = HumanoidConfig.from_raw({"ccb_body": "hoof"})
        self.assertEqual(conf.ccb_body, "hoof")

    def test_body_override_beats_detection(self):
        core = _core(PONY_PERSONA, {"ccb_body": "human"})
        profile = core.species_profile()
        self.assertEqual(profile["kind"], species.KIND_PONY)
        self.assertEqual(species.profile_body(profile), species.BODY_HUMAN)

    def test_invalid_body_override_is_ignored(self):
        core = _core(PONY_PERSONA, {"ccb_body": "bird"})
        profile = core.species_profile()
        self.assertEqual(species.profile_body(profile), species.BODY_HOOF)

    def test_life_material_reads_explicit_body(self):
        self.assertEqual(
            lm._species_body({"kind": "beast", "traits": ["鳞片"], "body": "human"}),
            species.BODY_HUMAN,
        )


    def test_plain_weather_is_never_worth_saying(self):
        """去重门之前先过「变天」判据：多云隔多久都不该被写进上下文。"""
        builder = _core("你是一个温柔的女生，喜欢看书。").prompt_builder
        self.assertFalse(builder._weather_worth_saying("多云，气温 12℃"))
        self.assertFalse(builder._weather_worth_saying("晴，气温 20℃"))
        self.assertTrue(builder._weather_worth_saying("小雨，气温 8℃"))


class OutfitByBodyTest(unittest.TestCase):
    """穿着跟着身体走：四蹄不穿、兽形与鳞甲「没穿衣服」、人形兽类不硬套。"""

    def test_half_dragon_is_not_declared_unclothed(self):
        """半龙人是人形有手，穿搭层判成兽形也不该说「没穿衣服」。"""
        core = _core("你是半龙人，头上有一对龙角，身上有鳞片，但你是人形，有手。")
        self.assertEqual(core.outfit_text(), "")

    def test_scale_body_is_unclothed(self):
        core = _core("你是一条龙，身上盖着深红的鳞片，背上有一对翅膀，性子很傲。")
        self.assertEqual(core.outfit_text(), "没穿衣服")

    def test_paw_body_is_unclothed(self):
        core = _core("你是一只灰狼，住在森林里，用四条腿走路，尾巴很蓬。")
        self.assertEqual(core.outfit_text(), "没穿衣服")

    def test_body_override_reaches_outfit(self):
        core = _core("她总穿一条白色连衣裙，喜欢看书。", {"ccb_body": "scale"})
        self.assertEqual(core.outfit_text(), "没穿衣服")

    def test_human_keeps_her_clothes(self):
        core = _core("她总穿一条白色连衣裙，喜欢看书。")
        self.assertEqual(core.outfit_text(), "一条白色连衣裙")


if __name__ == "__main__":
    unittest.main()
