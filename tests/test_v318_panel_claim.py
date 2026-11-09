"""v3.1.8：文档声称与 schema 实况的一致性护栏（审核报告 M-4 / 待确认 2）。

M-4 是「CHANGELOG 说面板按 9 块分组，实现里一个 object 都没有」。改掉那句话之后，
同样的问题还会再犯——因为**没有任何断言**把「文档里写的数字」和「schema 里的实况」
绑在一起，审核报告当时的待确认 2 说的就是这一点（collapsed 相关断言为 0）。

这里绑三件事：
    1) `_conf_schema.json` 的顶层项数、带 `condition` 的项数、`type == object` 的项数；
    2) CHANGELOG 里那句面板描述写的数字；
    3) README 里不再出现「按 N 块分组」这类没有实现的说法。

数字变了就必须同时改文档，否则这条会红——这才是「声称可证伪」。
"""

import json
import os
import re
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)


def _read(name: str) -> str:
    with open(os.path.join(_ROOT, name), "r", encoding="utf-8") as handle:
        return handle.read()


def _schema() -> dict:
    return json.loads(_read("_conf_schema.json"))


class PanelClaimTest(unittest.TestCase):
    """面板描述必须与 schema 实况对得上。"""

    def test_schema_is_flat_and_counts_match_the_documented_numbers(self):
        schema = _schema()
        self.assertEqual(len(schema), 144, "顶层项数变了，CHANGELOG 那句 144 项要跟着改")
        conditional = sum(
            1
            for item in schema.values()
            if isinstance(item, dict) and "condition" in item
        )
        self.assertEqual(conditional, 94, "带 condition 的项数变了，文档里的 94 要跟着改")
        # AstrBot 只把 type == object 的项渲染成 config-section；没有 object 就没有分组。
        grouped = [
            key
            for key, item in schema.items()
            if isinstance(item, dict) and item.get("type") == "object"
        ]
        self.assertEqual(grouped, [], "出现 object 项说明面板真的分组了，CHANGELOG 要改说法")

    def test_changelog_panel_sentence_matches_the_flat_schema(self):
        changelog = _read("CHANGELOG.md")
        self.assertIn(
            "面板是一整块列表，不做分组；144 项里 94 项跟着各自的主开关显隐。",
            changelog,
            "面板描述被改动或删掉了：M-4 的修法就是这一句，别让它悄悄漂回去",
        )
        # 「按 9 块分组」这类无实现支撑的说法不许再出现（v3.1.7 的原句）。
        self.assertNotRegex(
            changelog,
            r"面板按\s*\d+\s*块分组",
            "又写了没有实现支撑的分组说法",
        )

    def test_readme_does_not_promise_panel_grouping(self):
        readme = _read("README.md")
        self.assertNotRegex(
            readme,
            r"按\s*\d+\s*块分组|分成\s*\d+\s*块",
            "README 也不许承诺 schema 里不存在的分组",
        )


if __name__ == "__main__":
    unittest.main()
