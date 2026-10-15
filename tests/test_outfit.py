"""穿搭：给她一件贴合的衣服；她知道自己在穿什么；判不准就不给。"""
import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from humanoid import outfit
from humanoid.config import HumanoidConfig
from humanoid.core_instance import HumanoidCoreInstance
from humanoid.persona import PersonaSource
from humanoid.state import StateStore
from tests.fakes import FrozenClock, RecordingLogger


class DetectKind(unittest.TestCase):
    def test_empty_persona_means_nothing(self):
        self.assertEqual(outfit.detect_kind("小雨", ""), "")
        self.assertEqual(outfit.detect_kind("", "   "), "")

    def test_plain_persona_is_human(self):
        self.assertEqual(
            outfit.detect_kind("小雨", "你是一个温柔的女生，喜欢看书。"), outfit.KIND_HUMAN
        )

    def test_beast_ear(self):
        self.assertEqual(
            outfit.detect_kind("阿狸", "你是一只猫娘，有猫耳和尾巴。"), outfit.KIND_BEAST_EAR
        )

    def test_beast(self):
        self.assertEqual(
            outfit.detect_kind("", "你会说话的小马，住在小马利亚。"), outfit.KIND_BEAST
        )

    def test_machine(self):
        self.assertEqual(outfit.detect_kind("", "你是一台仿生机器人。"), outfit.KIND_MACHINE)

    def test_spirit(self):
        self.assertEqual(outfit.detect_kind("", "你是一只精灵，住在森林里。"), outfit.KIND_SPIRIT)

    def test_machine_beats_beast(self):
        self.assertEqual(outfit.detect_kind("", "机械狐狸"), outfit.KIND_MACHINE)


class Resolve(unittest.TestCase):
    def test_preset_wins(self):
        self.assertEqual(
            outfit.resolve(preset="红色毛衣", name="小雨", persona="温柔女生"), "红色毛衣"
        )

    def test_builtin_by_kind(self):
        self.assertEqual(outfit.resolve(name="", persona="你会说话的小马"), "没穿衣服")

    def test_unknown_gives_nothing(self):
        self.assertEqual(outfit.resolve(name="小雨", persona=""), "")

    def test_scene_sleep(self):
        got = outfit.resolve(name="小雨", persona="温柔女生", scene="sleep")
        self.assertIn("睡衣", got)

    def test_scene_bath_is_bare(self):
        self.assertEqual(
            outfit.resolve(name="小雨", persona="温柔女生", scene="bath"), "没穿衣服"
        )

    def test_beast_ignores_scene(self):
        self.assertEqual(
            outfit.resolve(name="", persona="你会说话的小马", scene="sleep"), "没穿衣服"
        )

    def test_scene_variation_off(self):
        self.assertEqual(
            outfit.resolve(name="小雨", persona="温柔女生", scene="sleep", scene_variation=False),
            "日常便服",
        )

    def test_cold_adds_coat(self):
        self.assertIn("外套", outfit.resolve(name="小雨", persona="温柔女生", scene="cold"))


class SceneOf(unittest.TestCase):
    def test_sleep_by_event(self):
        self.assertEqual(outfit.scene_of(event="午睡"), "sleep")
        self.assertEqual(outfit.scene_of(asleep=True), "sleep")

    def test_bath(self):
        self.assertEqual(outfit.scene_of(event="洗澡"), "bath")

    def test_out(self):
        self.assertEqual(outfit.scene_of(event="出门买菜"), "out")

    def test_out_cold(self):
        self.assertEqual(outfit.scene_of(event="出门", temp_c=2.0), "cold")

    def test_unwell_by_cycle(self):
        self.assertEqual(outfit.scene_of(cycle_phase=0), "unwell")

    def test_home(self):
        self.assertEqual(outfit.scene_of(event="看书"), "home")


class Line(unittest.TestCase):
    def test_line(self):
        self.assertEqual(outfit.line("红色毛衣"), "她穿着：红色毛衣")

    def test_empty(self):
        self.assertEqual(outfit.line(""), "")


class EndToEnd(unittest.TestCase):
    """从 Core 实例走一遍：人设 → 识别 → 注入，接线对不对只有跑起来才知道。"""

    def _core(self, persona, conf_extra=None):
        # 关掉「场景变化」：这几个用例测的是基础穿着，场景（睡衣/家居服等）另有专门用例。
        raw = {"timezone_city": "北京", "outfit_scene_variation": False}
        raw.update(conf_extra or {})
        conf = HumanoidConfig.from_raw(raw)
        st = StateStore(Path(tempfile.mkdtemp()) / "s.json", 0.01)
        st.load("2026-09-26", 28)

        class M:
            async def resolve_selected_persona(self, **k):
                return ("p", {"name": "", "prompt": persona, "begin_dialogs": []}, None, False)

            async def get_default_persona_v3(self):
                return {"name": "", "prompt": persona, "begin_dialogs": []}

        class C:
            persona_manager = M()
            conversation_manager = None

            def get_config(self, *a, **k):
                return None

        c = C()
        src = PersonaSource(c, lambda: RecordingLogger())
        core = HumanoidCoreInstance(
            role_id="bot1", state_store=st, config_provider=lambda: conf,
            logger=RecordingLogger(), stop_event=asyncio.Event(),
            resolver=c, gateway=None, persona_source=src,
        )
        asyncio.run(src.persona("bot1"))
        core.clock = FrozenClock(datetime(2026, 9, 26, 23, 0))
        return core

    def test_human_persona_gets_outfit(self):
        core = self._core("你是一个温柔的女生，喜欢看书。")
        self.assertEqual(core.outfit_text(), "日常便服")
        self.assertIn("她穿着", core.outfit_line())
        self.assertEqual(core.outfit_info()["kind"], outfit.KIND_HUMAN)

    def test_beast_persona_is_unclothed(self):
        core = self._core("你会说话的小马，住在小马利亚。")
        self.assertEqual(core.outfit_text(), "没穿衣服")

    def test_no_persona_no_outfit(self):
        core = self._core("")
        self.assertEqual(core.outfit_text(), "")
        self.assertEqual(core.outfit_line(), "")
        self.assertEqual(core.outfit_info()["source"], "无（判不出她是什么，不注入）")

    def test_override_wins(self):
        core = self._core("你是一个温柔的女生。")
        core.set_outfit_override("白色连衣裙")
        self.assertEqual(core.outfit_text(), "白色连衣裙")
        core.set_outfit_override("自动")
        self.assertEqual(core.outfit_text(), "日常便服")

    def test_disabled_injects_nothing(self):
        core = self._core("你是一个温柔的女生。", {"outfit_enabled": False})
        self.assertEqual(core.outfit_text(), "")
        self.assertFalse(core.outfit_info()["enabled"])


class SpeciesDetectionThreeLayers(unittest.TestCase):
    """D3：物种判定三层——11 正例必须命中 beast，23 反例一个都不许命中。

    裸子串的代价是双向的：`喜欢恐龙`/`水龙头`/`尼龙外套` 被误判成 beast（于是「没穿衣服」），
    而用户真正想用的 `狼/马/龙/兽人` 反而判不出来。三层 = 先 mask 语素复合词、
    动物词必须落在身份位置、人形非人只扫身份区。
    """

    POSITIVE = (
        "你是一只狼", "你是一只小马", "你是一条龙", "你是兽人", "你是一只狐狸",
        "你是一只恐龙", "种族：狼", "你是一只猫娘", "我是一个狼人", "设定：半兽人",
        "你是龙娘",
    )
    NEGATIVE = (
        "喜欢恐龙", "家里水龙头坏了", "穿着尼龙外套", "喜欢喝乌龙茶", "爱吃龙虾",
        "喜欢火龙果", "龙眼很甜", "在剧组跑龙套", "喜欢玩狼人杀", "练出了人鱼线",
        "喜欢养仙人掌", "喜欢机械表", "喜欢看人偶剧", "小马哥很帅", "做过天使投资",
        "用神仙水护肤", "喜欢凤凰男那部剧", "马路上车很多", "我马上就来", "喜欢骑马",
        "看过恐龙园", "喜欢猫", "我家养了条狗",
    )

    def test_positives_are_beast(self):
        for text in self.POSITIVE:
            with self.subTest(text=text):
                self.assertEqual(
                    outfit.detect_kind("", text), outfit.KIND_BEAST,
                    f"「{text}」该判 beast（兽形、不穿衣服）",
                )

    def test_negatives_are_not_beast(self):
        for text in self.NEGATIVE:
            with self.subTest(text=text):
                self.assertNotEqual(
                    outfit.detect_kind("", text), outfit.KIND_BEAST,
                    f"「{text}」里的那个字是构词成分，不是物种声明",
                )

    def test_character_name_does_not_decide_the_species(self):
        """角色名不参与判定：名字叫「小龙女」不该让她「没穿衣服」。"""
        self.assertEqual(
            outfit.detect_kind("小龙女", "你是一个温柔的女生，喜欢看书。"), outfit.KIND_HUMAN
        )
        self.assertEqual(outfit.detect_kind("大灰狼", ""), "")

    def test_ear_and_tail_win_over_a_plain_animal(self):
        """有兽耳/尾巴的人形角色判 beast_ear——穿衣是安全侧。"""
        self.assertEqual(
            outfit.detect_kind("", "你是一只猫娘，有猫耳和尾巴。"), outfit.KIND_BEAST_EAR
        )


class SpiritMaskConsistency(unittest.TestCase):
    """裸「天使」「凤凰」**不** mask：留给层③ 在身份区判 spirit；误判源（天使投资 / 凤凰男）才 mask。

    这是交叉检查出来的坑：mask 表里同时有「天使」、`_SPIRIT_WORDS` 里也有「天使」，
    于是层① 先把词挖掉、层③ 永远看不到它 → 天使/凤凰人设永远判不出 spirit（退回 human 便服）。
    正确做法与 `_SPIRIT_WORDS` 里其余词（精灵 / 恶魔 / 女巫 …）口径一致：只 mask 复合词。
    """

    POSITIVE = ("你是一个天使", "你是凤凰", "种族：天使", "我是一个天使", "设定：凤凰")
    NEGATIVE = ("做过天使投资", "喜欢凤凰男那部剧", "用神仙水护肤", "天使投资很赚钱")

    def test_angel_and_phoenix_are_spirit(self):
        for text in self.POSITIVE:
            with self.subTest(text=text):
                self.assertEqual(
                    outfit.detect_kind("", text), outfit.KIND_SPIRIT,
                    f"「{text}」是在声明物种，该判 spirit",
                )

    def test_the_compound_words_are_still_masked(self):
        for text in self.NEGATIVE:
            with self.subTest(text=text):
                self.assertNotEqual(
                    outfit.detect_kind("", text), outfit.KIND_SPIRIT,
                    f"「{text}」里的「天使/凤凰」是构词成分，不是物种声明",
                )


class SleepWordsAndScenePriority(unittest.TestCase):
    def test_bare_meng_is_not_sleep(self):
        """裸「梦」被去掉：`聊梦想` 不该被塞进睡衣。"""
        self.assertNotEqual(outfit.scene_of(event="聊梦想"), "sleep")
        for text in ("做梦", "梦游", "噩梦", "午睡", "赖床"):
            with self.subTest(text=text):
                self.assertEqual(outfit.scene_of(event=text), "sleep")

    def test_going_out_beats_discomfort(self):
        """出门在外优先于不适：人在外面，生理期/不舒服也不该被换成家居服。"""
        self.assertEqual(outfit.scene_of(event="出门买菜", temp_c=2.0, discomfort=70.0), "cold")
        self.assertEqual(outfit.scene_of(event="出门买菜", discomfort=70.0), "out")
        # 没出门时「不适」照旧生效
        self.assertEqual(outfit.scene_of(event="看书", discomfort=70.0), "unwell")
        self.assertEqual(outfit.scene_of(cycle_phase=0), "unwell")

    def test_temperature_accepts_numbers_and_legacy_strings(self):
        """温度优先收数值；旧的天气展示串仍能解析；失配记 debug 而不是静默失配。"""
        self.assertEqual(outfit.coerce_temp_c(3), 3.0)
        self.assertEqual(outfit.coerce_temp_c(3.5), 3.5)
        self.assertEqual(outfit.coerce_temp_c("阴，3度"), 3.0)
        self.assertEqual(outfit.coerce_temp_c("12 C"), 12.0)
        self.assertIsNone(outfit.coerce_temp_c("多云"))
        self.assertIsNone(outfit.coerce_temp_c(None))
        self.assertIsNone(outfit.coerce_temp_c(float("nan")))
        self.assertEqual(outfit.scene_of(event="出门", temp_c="2 C"), "cold")
        self.assertEqual(outfit.scene_of(event="看书", temp_c=2.0), "cold")

    def test_preset_is_appended_not_replaced(self):
        """手写预设是用户写的东西：睡觉/洗澡只能追加说明，不能整段丢掉。"""
        got = outfit.resolve(
            preset="白色蕾丝连衣裙", name="小雨", persona="温柔女生", scene="sleep"
        )
        self.assertIn("白色蕾丝连衣裙", got)
        self.assertIn("睡衣", got)
        got = outfit.resolve(
            preset="白色蕾丝连衣裙", name="小雨", persona="温柔女生", scene="bath"
        )
        self.assertIn("白色蕾丝连衣裙", got)
        # 内置基础穿着照旧整段替换（没人在上面写过字）
        self.assertEqual(outfit.resolve(name="小雨", persona="温柔女生", scene="sleep"), "睡衣")
        self.assertEqual(outfit.resolve(name="小雨", persona="温柔女生", scene="bath"), "没穿衣服")

    def test_temp_only_call_still_gets_a_scene(self):
        """只给温度不给场景时，够冷就当冷天（旧调用方永远传场景，不受影响）。"""
        self.assertIn("外套", outfit.resolve(name="小雨", persona="温柔女生", temp_c=2.0))


if __name__ == "__main__":
    unittest.main()
