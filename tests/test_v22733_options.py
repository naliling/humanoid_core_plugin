"""面板上每一个配置项，都得真的接上。

（v2.27.33 全量审计）116 个 key 挨个查过：解析层没有死键、没有隐藏键、没有重复 key；
边界值（0 / 负数 / 天文数字 / 空串 / None / 错类型 / NaN / inf）喂进去一个都没炸，
数值项也都被夹在面板区间里。这里把「查过」变成每次跑测试都会重查的东西——
配置项的毛病只在**加配置项**的时候冒出来，而那正是最没人回头看的时刻。

五道闸门：

1. schema 的 key == dataclass 字段 == `from_raw` 真的读的 key。少一个是死选项，
   多一个是隐藏选项（面板上改不到、代码里却在读）。
2. 每个 key 在 `humanoid/config.py` 之外至少有一处**字面读取点**。只在 config.py 里出现、
   别处从来没人用的项，就是「面板上有、行为上没有」。两类例外单独列出来，而且都要证明
   自己不是免死金牌：三个 `mood_log_threshold_*` 走 `getattr(cfg, f"mood_log_threshold_{key}")`
   动态取键；`mood_affection_override` / `energy_recovery_phase_multipliers` 走 config.py 里的
   派生访问器 `affection_override_for` / `phase_recovery_multiplier`，调用方压根不出现 key 名。
3. 默认值与面板一致；数值项被填越界时，代码自己会夹住（面板拦不住的，代码必须拦）。
4. 上下限有两份写法：`minimum`/`maximum`（插件自己的 `test_schema.py` 认它）与 `slider`
   （面板认它）。逐项相等，否则「代码声称的范围」和「用户能拖到的范围」会各说各话。
5. README 里能对上的数字都跟上 schema：项数、默认值。面板上写着 105 项、实际 116 项
   这种事，用户没有第二个地方能发现。
6. 能观察的项，改一下就必须看得出变化——断言的是**变化**，不是具体文案：
   文案随时会改，变化不会。
"""

from __future__ import annotations

import ast
import dataclasses
import json
import re
import unittest
from pathlib import Path

from humanoid.config import HumanoidConfig
from humanoid.prompt_builder import estimate_tokens

from .test_v2273_life_note import _Harness

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "_conf_schema.json"
README_PATH = ROOT / "README.md"
CONFIG_PATH = ROOT / "humanoid" / "config.py"
MOOD_PATH = ROOT / "humanoid" / "services" / "mood.py"

# AstrBot 面板认的 type（docs.astrbot.app/dev/star/guides/plugin-config.html）。
PANEL_TYPES = {"string", "text", "int", "float", "bool", "object", "list", "dict", "template_list"}

# 同一个区间的两份写法：面板认 `slider`，插件自己的 test_schema.py 认 `minimum`/`maximum`。
PANEL_BOUNDS = {"minimum": "min", "maximum": "max"}

# 值不是按 key 名直接读的项，两条出路：
#   动态键名 —— 名字是 f-string 拼出来的，字面 grep 看不见；
#   派生访问器 —— 读的是 config.py 里一个算过的属性/方法，调用方根本不出现这个 key。
DYNAMIC_KEYS = {
    "mood_log_threshold_affection": "mood_log_threshold_{key}",
    "mood_log_threshold_libido": "mood_log_threshold_{key}",
    "mood_log_threshold_aggression": "mood_log_threshold_{key}",
}

DERIVED_KEYS = {
    "mood_affection_override": ("affection_override_for", "humanoid/services/mood.py"),
    "energy_recovery_phase_multipliers": ("phase_recovery_multiplier", "humanoid/services/energy.py"),
}


def _load_schema() -> tuple[dict, list[str]]:
    """读 schema，顺带抓重复 key（json 默认「后者胜」，会静默吃掉一个）。"""
    duplicates: list[str] = []

    def hook(pairs):
        seen = set()
        for key, _value in pairs:
            if key in seen:
                duplicates.append(key)
            seen.add(key)
        return dict(pairs)

    with open(SCHEMA_PATH, encoding="utf-8") as handle:
        return json.load(handle, object_pairs_hook=hook), duplicates


SCHEMA, DUPLICATE_KEYS = _load_schema()


def _from_raw_kwargs() -> set[str]:
    """`from_raw` 里 `cfg = cls(...)` 那一句真的读了哪些 key（AST，不执行）。"""
    tree = ast.parse(CONFIG_PATH.read_text(encoding="utf-8"))
    keys: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "cfg" for t in node.targets):
            continue
        for keyword in node.value.keywords:
            if keyword.arg:
                keys.add(keyword.arg)
    return keys


def _production_sources() -> dict[str, str]:
    """生产代码（不含 tests/），用来找字面读取点。"""
    out: dict[str, str] = {}
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith("tests/") or "__pycache__" in rel:
            continue
        out[rel] = path.read_text(encoding="utf-8", errors="replace")
    return out


def _adversarial(value_type: str) -> list:
    """面板拦不住的值：用户手改 data/config 下的 json 时什么都可能填。"""
    if value_type == "int":
        return [0, -1, 10 ** 9, -(10 ** 9), "", None, "abc", [], True, 3.7, 10 ** 20]
    if value_type == "float":
        return [0.0, -1.0, 1e12, -1e12, "", None, "abc", [], True, float("nan"), float("inf")]
    if value_type == "bool":
        return [0, 1, "yes", "", None, [], 2, -1]
    if value_type == "string":
        return ["", None, 0, [], "x" * 5000, 12345]
    if value_type == "list":
        return [[], None, "", 0, [None], [123], ["a", "b"], {"a": 1}, [[1, 2]], "a,b"]
    return [None, "", 0, [], {}]


def _readme_table() -> dict[str, str]:
    """README「关键配置项」那张表的 `key -> 默认值`。"""
    rows: dict[str, str] = {}
    for line in README_PATH.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\|\s*`([a-z_0-9]+)`\s*\|\s*([^|]+?)\s*\|", line)
        if match:
            rows[match.group(1)] = match.group(2)
    return rows


class SchemaWiringTest(unittest.TestCase):
    """面板、dataclass、解析器三张表必须一一对上。"""

    def test_no_duplicate_keys(self):
        self.assertEqual(DUPLICATE_KEYS, [], "schema 里有重复 key，后面那个会静默吃掉前面那个")

    def test_every_key_has_a_description_and_a_default(self):
        for key, meta in SCHEMA.items():
            with self.subTest(key=key):
                self.assertTrue(str(meta.get("description", "")).strip(), f"{key} 没有 description")
                self.assertIn("default", meta, f"{key} 没有 default")

    def test_types_are_the_ones_the_panel_understands(self):
        for key, meta in SCHEMA.items():
            with self.subTest(key=key):
                self.assertIn(meta.get("type"), PANEL_TYPES, f"{key} 的 type 面板不认，会渲染不出来")

    def test_schema_keys_are_exactly_the_dataclass_fields(self):
        names = {field.name for field in dataclasses.fields(HumanoidConfig)}
        self.assertEqual(names - set(SCHEMA), set(), "dataclass 有 schema 里没有的字段：面板上改不到")
        self.assertEqual(set(SCHEMA) - names, set(), "schema 有 dataclass 里没有的字段：面板上有、代码里没有")

    def test_from_raw_reads_every_key(self):
        kwargs = _from_raw_kwargs()
        self.assertEqual(set(SCHEMA) - kwargs, set(), "这些 key 面板上有、from_raw 根本没读")
        self.assertEqual(kwargs - set(SCHEMA), set(), "这些 key from_raw 在读、面板上却没有")

    def test_every_key_is_read_somewhere_outside_config(self):
        sources = _production_sources()
        others = {rel: text for rel, text in sources.items() if rel != "humanoid/config.py"}
        missing = [
            key
            for key in SCHEMA
            if key not in DYNAMIC_KEYS
            and key not in DERIVED_KEYS
            and not any(re.search(rf"\b{re.escape(key)}\b", text) for text in others.values())
        ]
        self.assertEqual(missing, [], f"这些 key 没有任何读取点（面板上有、行为上没有）：{missing}")

    def test_dynamic_keys_are_still_read_dynamically(self):
        mood = MOOD_PATH.read_text(encoding="utf-8")
        for key, template in DYNAMIC_KEYS.items():
            with self.subTest(key=key):
                self.assertIn(template, mood, f"{key} 既没有字面读取点，也没有动态读取点")

    def test_derived_keys_really_have_a_consumer(self):
        """白名单不能是免死金牌：访问器得定义在 config.py 里，而且真的有人在别处调它。"""
        config_text = CONFIG_PATH.read_text(encoding="utf-8")
        sources = _production_sources()
        for key, (accessor, consumer) in DERIVED_KEYS.items():
            with self.subTest(key=key):
                self.assertIn(f"def {accessor}", config_text, f"{key} 的访问器 {accessor} 不在 config.py 里")
                text = sources.get(consumer)
                self.assertIsNotNone(text, f"{consumer} 不存在，白名单写错了")
                self.assertIn(f".{accessor}(", text, f"{key} 的访问器 {accessor} 在 {consumer} 里没人调用")

    def test_defaults_match_the_schema(self):
        cfg = HumanoidConfig.from_raw({})
        for key, meta in SCHEMA.items():
            with self.subTest(key=key):
                parsed = getattr(cfg, key)
                expected = meta["default"]
                if isinstance(expected, str) and isinstance(parsed, (int, float)) and not isinstance(parsed, bool):
                    # 面板上只有下拉框、代码里当数字用（energy_decay_rate 就是这种）。
                    self.assertAlmostEqual(float(parsed), float(expected), places=6)
                elif isinstance(expected, list):
                    self.assertEqual(list(parsed), list(expected))
                else:
                    self.assertEqual(parsed, expected)

    def test_minimum_maximum_mirror_the_slider(self):
        """`minimum`/`maximum` 与 `slider` 是同一个区间的两份写法：面板认 `slider`，
        插件自己的 `test_schema.py` 认 `minimum`/`maximum`。两套必须逐项相等，
        否则「面板上放行的范围」和「代码声称的范围」会各说各话。"""
        for key, meta in SCHEMA.items():
            for field, slider_key in PANEL_BOUNDS.items():
                if field not in meta:
                    continue
                with self.subTest(key=key, field=field):
                    slider = meta.get("slider")
                    self.assertIsInstance(slider, dict, f"{key} 有 {field} 却没有 slider")
                    self.assertIn(slider_key, slider, f"{key} 的 slider 缺 {slider_key}")
                    self.assertEqual(float(meta[field]), float(slider[slider_key]))


class HintTellsTheTruthTest(unittest.TestCase):
    """hint 里承诺的行为，代码里得真的那么干——否则面板就是在骗人。"""

    def test_log_threshold_hints_admit_that_zero_means_unset(self):
        """`threshold <= 0.0` 会退回内置上限，而 0 又是滑块最小格：
        hint 不说，用户拖到 0 只会以为「最灵敏」，实际拿到的是默认值。"""
        mood = MOOD_PATH.read_text(encoding="utf-8")
        self.assertRegex(
            mood,
            r"if threshold <= 0\.0:\s*\n\s*threshold = float\(floor\)",
            "mood.py 里的「0 退回内置上限」没了，hint 却还在这么说",
        )
        for key, floor in (
            ("mood_log_threshold_affection", "0.6"),
            ("mood_log_threshold_libido", "0.5"),
            ("mood_log_threshold_aggression", "0.8"),
        ):
            with self.subTest(key=key):
                hint = SCHEMA[key]["hint"]
                self.assertIn("填 0 视同没填", hint)
                self.assertIn(floor, hint)


class BoundaryTest(unittest.TestCase):
    """面板拦不住的值，代码必须自己接住。"""

    def test_adversarial_values_never_raise(self):
        for key, meta in SCHEMA.items():
            for value in _adversarial(str(meta.get("type", ""))):
                with self.subTest(key=key, value=repr(value)[:40]):
                    cfg = HumanoidConfig.from_raw({key: value})
                    self.assertIsInstance(cfg, HumanoidConfig)

    def test_numeric_values_are_clamped_to_the_panel_range(self):
        checked = 0
        for key, meta in SCHEMA.items():
            if meta.get("type") not in ("int", "float"):
                continue
            slider = meta.get("slider")
            if not isinstance(slider, dict) or "min" not in slider or "max" not in slider:
                continue
            low, high = float(slider["min"]), float(slider["max"])
            span = max(1.0, high - low)
            for value in (low - 1, high + 1, low - span * 1000, high + span * 1000):
                with self.subTest(key=key, value=value):
                    parsed = float(getattr(HumanoidConfig.from_raw({key: value}), key))
                    self.assertGreaterEqual(parsed, low, f"{key} 填 {value} 之后低于面板下限")
                    self.assertLessEqual(parsed, high, f"{key} 填 {value} 之后高于面板上限")
                    checked += 1
        self.assertGreater(checked, 0, "一个带 slider 的项都没查到，是测试自己坏了")


class ObservableOptionsTest(unittest.TestCase):
    """不是「有代码读它」，是「读了以后行为真的变了」。"""

    def _block(self, raw: dict, **kwargs) -> str:
        harness = _Harness(dict({"timezone_city": "北京"}, **raw))
        try:
            return harness.core.build_injection("10001", **kwargs)
        finally:
            harness.store.flush_sync()

    def _note(self, raw: dict) -> str:
        harness = _Harness(dict({"timezone_city": "北京"}, **raw))
        try:
            return harness.core.prompt_builder.background_note()
        finally:
            harness.store.flush_sync()

    def test_inject_activity_context_changes_how_much_is_given(self):
        low = self._block({"inject_activity_context": "mood_only"})
        high = self._block({"inject_activity_context": "full"})
        self.assertNotEqual(low, high)
        self.assertLess(len(low), len(high), "低侵入档反而给得更多，说明档位接反了")

    def test_soma_enabled_switches_the_body_layer(self):
        self.assertNotEqual(self._block({"soma_enabled": True}), self._block({"soma_enabled": False}))

    def test_mood_enabled_in_group_only_touches_group_chat(self):
        on = self._block({"mood_enabled_in_group": True}, is_group=True)
        off = self._block({"mood_enabled_in_group": False}, is_group=True)
        self.assertNotEqual(on, off)
        self.assertEqual(
            self._block({"mood_enabled_in_group": True}),
            self._block({"mood_enabled_in_group": False}),
            "这个开关只管群聊，不该动私聊",
        )

    def test_enable_chat_awareness_shows_up_in_group_chat(self):
        on = self._block({"enable_chat_awareness": True}, is_group=True)
        off = self._block({"enable_chat_awareness": False}, is_group=True)
        self.assertNotEqual(on, off)
        self.assertIn("还有别人", on)
        self.assertNotIn("还有别人", off)

    def test_show_city_time_in_low_intrusion_adds_the_city(self):
        on = self._note({"show_city_time_in_low_intrusion": True})
        off = self._note({"show_city_time_in_low_intrusion": False})
        self.assertNotEqual(on, off)
        self.assertIn("北京", on)
        self.assertNotIn("北京", off)

    def test_inject_token_budget_drops_low_salience_sentences(self):
        harness = _Harness({"timezone_city": "北京"})
        try:
            builder = harness.core.prompt_builder
            sents = [("甲" * 120, 9.0), ("乙" * 120, 1.0)]
            harness.box.reload({"timezone_city": "北京", "inject_token_budget": 200})
            tight = builder._finish(sents, harness.box.value, "小雨", "TA")
            harness.box.reload({"timezone_city": "北京", "inject_token_budget": 20000})
            loose = builder._finish(sents, harness.box.value, "小雨", "TA")
        finally:
            harness.store.flush_sync()
        self.assertIn("甲", tight)
        self.assertNotIn("乙", tight, "预算卡到 200 token 还留着低显著度的句子")
        self.assertIn("甲", loose)
        self.assertIn("乙", loose)
        self.assertLess(estimate_tokens(tight), estimate_tokens(loose))


class ReadmeTest(unittest.TestCase):
    """README 里写死的数字会过期，而过期的文档比没有文档更糟：
    用户照着它去面板上找那个默认值，找到的是另一个数。"""

    @staticmethod
    def _normalize(value) -> str:
        if isinstance(value, bool):
            return "开" if value else "关"
        if value == "":
            return "空"
        return str(value)

    def test_readme_states_the_real_option_count(self):
        match = re.search(r"面板上共\s*(\d+)\s*项", README_PATH.read_text(encoding="utf-8"))
        self.assertIsNotNone(match, "README 里找不到「面板上共 N 项」这句")
        self.assertEqual(int(match.group(1)), len(SCHEMA), "README 写的项数和 _conf_schema.json 对不上")

    def test_readme_defaults_match_the_schema(self):
        rows = _readme_table()
        checked = 0
        for key, cell in rows.items():
            meta = SCHEMA.get(key)
            if meta is None:
                continue
            checked += 1
            with self.subTest(key=key):
                self.assertEqual(cell.strip(), self._normalize(meta["default"]), f"README 里 {key} 的默认值写错了")
        self.assertGreater(checked, 10, "README 表格没解析到几行，是测试自己坏了")


if __name__ == "__main__":
    unittest.main()
