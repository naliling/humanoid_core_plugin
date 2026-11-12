"""v3.1.37：按身体类型写细节——人、四蹄、爪兽各用各的句子。

v3.1.33 加的物种硬闸只认「人形专有名词」（鞋、袖子、手指…），可素材池和感官句池
里还写着一批**只属于人的身体动作**：「袖口蹭到胳膊，有点痒」「手腕有点发酸」
「胳膊举过头顶有点酸」——词表里连「胳膊」「手腕」都没有，于是小马照样收到。
补词只能拦住，拦完那一批素材就空了；真正要的是**按身体类型换一套句子**：

* 四蹄（pony / 半人马 / 带四蹄的兽形）：蹄、鬃毛、皮毛、后腿、鼻尖，不穿衣服；
* 爪兽（beast）：爪、鼻头、后腿，不写蹄；
* 鳞甲（龙 / 龙马 / 麒麟 / 人鱼）：爪和蹄都可能有，但盖的是鳞片，不是皮毛；
* 人形（human / machine / 半兽人形 / 魅魔一类）：一个字不动，手指、领口照旧。

这个文件钉四件事：
    1. `species.body_kind()` 的判定表；
    2. 三句漏网句子现在拦得住，而「腰」这种四足也有的部位照样放行；
    3. 四个小马人设的素材（event / sensory）与感官句里没有人形部位；
    4. 兽人不出「手 / 蹄」，人类不出「蹄」——分池不能拿别人的句子去补。
"""

from __future__ import annotations

import asyncio
import re
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from humanoid import species
from humanoid.config import HumanoidConfig
from humanoid.core_instance import HumanoidCoreInstance
from humanoid.persona import PersonaSource
from humanoid.services import life_material as lm
from humanoid.services.schedule_content import HUMAN_ONLY_TERMS, species_issues
from humanoid.state import StateStore
from tests.fakes import FrozenClock, RecordingLogger

UID = "u-v3137"
BASE = datetime(2026, 9, 26, 0, 0)
SLOTS = (8, 13, 21, 3)

PONY_PERSONAS = {
    "独角兽": "你是小马利亚，纯白双翼的独角小马。你住在大树图书馆，喜欢看书。",
    "陆马": "你是苹果嘉儿，一匹橙色陆马，四蹄结实，鬃毛是金色的。你在甜苹果园种苹果。",
    "飞马": "你是一只淡蓝色的小马，背上有翅膀，喜欢在云上睡觉。",
    "半人马": "你是半人马，上半身像人，下半身是四蹄，尾巴会扫到草。",
}
BEAST_PERSONA = "你是一只灰狼，住在森林里，用四条腿走路，尾巴很蓬。"
HUMAN_PERSONA = "她总穿一条白色连衣裙，喜欢看书，手指很长。"

HUMAN_TERMS = re.compile(r"手指|指尖|手掌|手背|手心|小臂|胳膊|袖子|领口|手腕")
HOOF_TERMS = re.compile(r"蹄|爪")


def _core(persona: str, moment: datetime = BASE + timedelta(hours=13)) -> HumanoidCoreInstance:
    conf = HumanoidConfig.from_raw({"timezone_city": "北京"})
    store = StateStore(Path(tempfile.mkdtemp()) / "s.json", 0.01)
    store.load(moment.strftime("%Y-%m-%d"), 28)

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

    resolver = _C()
    source = PersonaSource(resolver, lambda: RecordingLogger())
    core = HumanoidCoreInstance(
        role_id="bot1", state_store=store, config_provider=lambda: conf,
        logger=RecordingLogger(), stop_event=asyncio.Event(),
        resolver=resolver, gateway=None, persona_source=source,
    )
    asyncio.run(source.persona("bot1"))
    core.clock = FrozenClock(moment)
    for name in ("schedule", "soma", "energy", "social", "process", "mood"):
        service = getattr(core, name, None)
        if service is not None and hasattr(service, "_clock"):
            service._clock = core.clock
    return core


def _sensory_lines(persona: str, days: int = 3) -> set[str]:
    lines: set[str] = set()
    for day in range(days):
        for hour in SLOTS:
            core = _core(persona, BASE + timedelta(days=day, hours=hour))
            builder = core.prompt_builder
            soma = core.snapshot(refresh=False).get("soma") or {}
            line = builder._sensory_line(
                core.clock.now(), soma, builder._outfit_text(), builder._seed(UID)
            )
            if line:
                lines.add(line)
    return lines


class BodyKindTest(unittest.TestCase):
    """身体类型分四种：人形、四蹄、爪兽、鳞甲。判不出来按人写。"""

    def test_kind_table(self):
        self.assertEqual(species.body_kind("pony"), species.BODY_HOOF)
        self.assertEqual(species.body_kind("pony", ["角", "翅膀"]), species.BODY_HOOF)
        self.assertEqual(species.body_kind("beast", ["尾巴", "四蹄"]), species.BODY_HOOF)
        self.assertEqual(species.body_kind("beast", ["尾巴"]), species.BODY_PAW)
        self.assertEqual(species.body_kind("beast", ["角", "鳞片"]), species.BODY_SCALE)
        # 四蹄优先于鳞片：龙马、麒麟既有鳞又有蹄，说「蹄」不说「爪」。
        self.assertEqual(
            species.body_kind("beast", ["角", "鳞片", "四蹄"]), species.BODY_HOOF
        )
        for kind in ("human", "machine", "beast_ear", "spirit", ""):
            self.assertEqual(species.body_kind(kind), species.BODY_HUMAN, kind)

    def test_profile_body_tolerates_garbage(self):
        self.assertEqual(species.profile_body(None), species.BODY_HUMAN)
        self.assertEqual(species.profile_body({"kind": "pony"}), species.BODY_HOOF)
        self.assertEqual(
            species.profile_body({"kind": "pony", "traits": ["四蹄"]}), species.BODY_HOOF
        )


class HumanOnlyTermsTest(unittest.TestCase):
    """词表补齐：胳膊、手腕、小臂、领口现在拦得住；「腰」不许拦。"""

    def test_new_terms_are_registered(self):
        for term in ("小臂", "胳膊", "手腕", "领口", "脚踝"):
            self.assertIn(term, HUMAN_ONLY_TERMS)

    def test_reported_sentences_are_blocked(self):
        for text in ("袖口蹭到胳膊，有点痒", "手腕有点发酸", "胳膊举过头顶有点酸"):
            for kind in ("pony", "beast"):
                self.assertTrue(species_issues(text, kind), f"{kind} 漏了 {text}")

    def test_quadruped_parts_are_allowed(self):
        for text in ("坐久了腰有点酸", "腿有点沉", "背上有点痒"):
            self.assertEqual(species_issues(text, "pony"), [], text)


class PonyMaterialsTest(unittest.TestCase):
    """四个小马人设：素材的 event 与 sensory 都得是她身上说得出的话。"""

    def test_personas_read_as_hoof(self):
        for label, persona in PONY_PERSONAS.items():
            self.assertEqual(species.profile_body(species.detect(persona)), species.BODY_HOOF, label)

    def test_events_and_sensory_are_clean(self):
        for label, persona in PONY_PERSONAS.items():
            for minute in (480, 780, 1260, 180):
                for item in lm.materials_for(
                    [UID, label, minute], persona_text=persona,
                    now_minute=minute, count=6, species="pony",
                ):
                    for key in ("event", "sensory"):
                        value = str(item.get(key) or "")
                        self.assertEqual(
                            species_issues(value, "pony"), [], f"{label} {key}={value!r}"
                        )
                    self.assertFalse(
                        HUMAN_TERMS.search(lm.body_sensory(item, "pony")),
                        f"{label} 换句后仍有人形部位：{lm.body_sensory(item, 'pony')}",
                    )

    def test_hoof_body_does_not_wear_clothes(self):
        for label, persona in PONY_PERSONAS.items():
            self.assertEqual(_core(persona).outfit_text(), "", label)

    def test_human_body_keeps_its_clothes(self):
        self.assertEqual(_core(HUMAN_PERSONA).outfit_text(), "一条白色连衣裙")

    def test_beast_body_is_unclothed(self):
        """兽形不穿衣服：别让穿搭层判不出兽形就给她套「日常便服」。"""
        self.assertEqual(_core(BEAST_PERSONA).outfit_text(), "没穿衣服")


class SensoryLinePerBodyTest(unittest.TestCase):
    """连渲染三天四时段：各身体类型只拿到自己的句子。"""

    def test_hoof_lines_have_no_human_parts(self):
        lines = _sensory_lines(PONY_PERSONAS["陆马"])
        self.assertTrue(lines)
        for line in lines:
            self.assertFalse(HUMAN_TERMS.search(line), line)

    def test_paw_lines_have_neither_hands_nor_hooves(self):
        lines = _sensory_lines(BEAST_PERSONA)
        self.assertTrue(lines)
        for line in lines:
            self.assertFalse(HUMAN_TERMS.search(line), line)
            self.assertFalse(HOOF_TERMS.search(line), line)

    def test_human_lines_keep_their_hands(self):
        lines = _sensory_lines(HUMAN_PERSONA)
        self.assertTrue(lines)
        for line in lines:
            self.assertFalse(HOOF_TERMS.search(line), line)


class ScaleBodyTest(unittest.TestCase):
    """龙 / 龙马 / 麒麟：判出物种、按蹄或鳞写，不出「皮毛」。"""

    def test_dragon_is_scale(self):
        profile = species.detect("你是一条龙，身上盖着深红的鳞片，背上有一对翅膀，性子很傲。")
        self.assertEqual(profile["kind"], "beast")
        self.assertEqual(profile["label"], "龙")
        self.assertEqual(species.profile_body(profile), species.BODY_SCALE)

    def test_dragon_horse_is_not_swallowed_by_pony_branch(self):
        """「四蹄 + 鬃毛」会把龙马拖进小马分支，标签写成独角兽——明确物种要优先。"""
        for text in (
            "你是龙马，四蹄踏在地上，背上有一对龙翼，鬃毛像火。",
            "你是一匹龙马，鬃毛像火焰，跑起来地面都在震。",
        ):
            profile = species.detect(text)
            self.assertEqual(profile["label"], "龙马", text)
            self.assertEqual(species.profile_body(profile), species.BODY_HOOF, text)

    def test_qilin_stays_a_pony(self):
        profile = species.detect("她是麒麟，头上有角，身上有鳞。")
        self.assertEqual(profile["kind"], "pony")
        self.assertEqual(profile["label"], "麒麟")
        self.assertEqual(species.profile_body(profile), species.BODY_HOOF)

    def test_dragon_materials_say_scales_not_fur(self):
        persona = "你是一条龙，身上盖着深红的鳞片，背上有一对翅膀，性子很傲。"
        profile = species.detect(persona)
        for minute in (480, 780, 1260, 180):
            for item in lm.materials_for(
                [UID, "龙", minute], persona_text=persona,
                now_minute=minute, count=6, species=profile,
            ):
                for key in ("event", "sensory"):
                    value = str(item.get(key) or "")
                    self.assertNotIn("皮毛", value, f"{key}={value!r}")
                    self.assertNotIn("蹄", value, f"{key}={value!r}")
                    self.assertEqual(species_issues(value, "beast"), [], value)

    def test_dragon_sensory_lines_say_scales(self):
        persona = "你是一条龙，身上盖着深红的鳞片，背上有一对翅膀，性子很傲。"
        lines = _sensory_lines(persona)
        self.assertTrue(lines)
        for line in lines:
            self.assertFalse(HUMAN_TERMS.search(line), line)
            self.assertNotIn("皮毛", line)
            self.assertNotIn("蹄", line)

    def test_scale_swaps_fur_for_scales(self):
        item = {"sensory": "皮毛底下有点闷", "band": "life"}
        self.assertEqual(lm.body_sensory(item, {"kind": "beast", "traits": ["鳞片"]}),
                         "鳞片底下有点闷")
        self.assertEqual(lm.body_sensory(item, {"kind": "beast", "traits": ["四蹄"]}),
                         "皮毛底下有点闷")


if __name__ == "__main__":
    unittest.main()
