"""v3.1.41：诊断报告要看得懂。

issue #10 的现象：`/拟人 诊断` 只回了四行——

    〖拟人诊断〗v3.1.39

    【AstrBot 可用对话模型 id】
    ……还有 69 行没显示

根因不是数据丢了，是模型 id 列表被拼成一行放在报告开头：用户配了六十来个模型，
那一行就两千多字符，超过整条消息的长度预算，后面的检查项全被截掉。

这一版：①开头先说怎么读；②模型 id 一行一个、挪到报告最末尾（超过 40 个只列
前 40 个并指路）；③诊断单独放宽长度预算；④`_clip_text` 遇到超长单行时截断该行
再收尾，不再把后面整段吞掉。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from humanoid.config import HumanoidConfig
from humanoid.data import cities
from humanoid.diagnostics import MODEL_ID_ROWS, build_report

from .test_main_integration import main_module

IDS_SECTION = "【AstrBot 可用对话模型 id】"


class _Resolver:
    def __init__(self, ids: list[str]) -> None:
        self._ids = list(ids)

    def available_ids(self) -> list[str]:
        return list(self._ids)

    def resolve(self, provider_id: str):
        return None

    def resolve_global(self, umo=None):
        return None

    def id_of(self, provider) -> str:
        return "p1"


class _Gateway:
    gate = None

    def cooldown_remaining(self, provider_id, purpose) -> float:
        return 0.0

    def cooldowns(self, purpose) -> dict:
        return {}

    def last_result(self, purpose):
        return None


def _report(ids: list[str]) -> str:
    return build_report(
        cfg=HumanoidConfig(),
        resolver=_Resolver(ids),
        gateway=_Gateway(),
        schedule_status={},
        activity_status={},
        version="3.1.41",
    )


class ReadableReportTest(unittest.TestCase):
    def test_how_to_read_comes_before_the_sections(self):
        text = _report(["p1"])
        legend = text.index("每一段是一项检查")
        self.assertLess(legend, text.index("【今日日程】"), "怎么读必须写在检查项前面")

    def test_model_ids_are_one_per_line_and_sit_at_the_end(self):
        text = _report(["deepseek/one", "deepseek/two"])
        self.assertIn("  - deepseek/one", text)
        self.assertIn("  - deepseek/two", text)
        self.assertNotIn("['deepseek/one'", text, "不能再把整份列表拼成一行")
        self.assertGreater(
            text.index(IDS_SECTION),
            text.index("【今日日程】"),
            "模型 id 是最长的一段，必须放在末尾，截断时才不会挤掉检查项",
        )

    def test_a_long_id_list_does_not_swallow_the_report(self):
        ids = [f"deepseek/model-{index:02d}" for index in range(60)]
        text = _report(ids)
        sent = main_module._clip_text(text, main_module.DIAGNOSTICS_OUTPUT_CHARS)
        self.assertNotIn("行没显示", sent, "整份报告要能一次发完，不该再出现截断提示")
        self.assertIn("【今日日程】", sent)
        self.assertIn("【调用节流】", sent)
        self.assertIn("  - deepseek/model-00", sent)
        self.assertNotIn("deepseek/model-59", sent, "超过 40 个只列前 40 个")
        self.assertIn(f"还有 {60 - MODEL_ID_ROWS} 个没列", sent)

    def test_the_note_says_where_the_rest_is(self):
        text = _report([f"p{index}" for index in range(MODEL_ID_ROWS + 5)])
        self.assertIn("「服务提供商」", text)


class ClipKeepsWhatComesAfterALongLineTest(unittest.TestCase):
    """中间一行特别长时，后面的内容不该整段消失。"""

    def test_a_long_middle_line_is_cut_and_the_rest_is_counted(self):
        lines = ["〖拟人诊断〗v3.1.41", "【第一段】", "x" * 3000, "【第二段】", "【第三段】"]
        out = main_module._clip_text("\n".join(lines), 1500)
        self.assertLessEqual(len(out), 1500)
        self.assertIn("【第一段】", out, "长行前面的内容要留着")
        self.assertTrue(out.endswith("行没显示"), out[-20:])

    def test_a_giant_first_line_is_still_hard_capped(self):
        out = main_module._clip_text("昵称" * 3000, 1500)
        self.assertTrue(out.startswith("昵称"))
        self.assertLessEqual(len(out), 1500)


class ReadmeCityCountTest(unittest.TestCase):
    """README 里写的地名表条数必须和代码里的真表一致。"""

    def _merged_count(self) -> int:
        bulk: set[str] = set()
        for blob in cities._BULK_BY_ZONE.values():
            bulk.update(name.strip() for name in blob.split("|") if name.strip())
        hand: set[str] = set()
        for attr in (
            "_CHINA",
            "_CHINA_PROVINCES",
            "_JAPAN",
            "_JAPAN_PREFECTURES",
            "_RUSSIA",
            "_RUSSIA_SUBJECTS",
            "_FAMOUS_PLACES",
            "_EXTRA_ALIASES",
        ):
            hand.update(getattr(cities, attr).keys())
        return len(bulk | hand)

    def test_readme_number_matches_the_table(self):
        readme = (Path(main_module.__file__).parent / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"地名表共 {self._merged_count():,} 条", readme)
