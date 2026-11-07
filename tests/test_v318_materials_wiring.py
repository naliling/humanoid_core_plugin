"""v3.1.8：materials_used 的接线护栏（审核报告 L-3）。

L-3 的原话是「删掉 main.py:2497-2500 那个 if 仍全绿」——`tests/test_v317_materials_used.py`
只测台账函数本身（`_note_materials_used` 的入参、去重、异常兜底），没有任何用例约束
「它到底有没有被接进注入侧的证据卡」。于是把那段 if 删掉，3048 项测试照样全绿。

这里补的是**接线**本身：静态读 main.py，确认证据卡那段里仍然
    1) 有 `if cards["materials"]:` 这个闸（没有卡就不写台账，也不报码）；
    2) 闸里调 `_note_materials_used(core, cards["materials"])` 并把返回值当条件；
    3) 真的记到了才 `note_reason("materials_used", ...)`；
    4) 位置在 `_evidence_note(...)` 之后、`_provenance_line(...)` 之前——
       证据卡整段是 fail-open 的，接错了地方（比如挪到 try 外面）就等于让它能拖垮回复。

静态断言不是理想的集成测试，但它精确对应「删掉仍全绿」这个缺陷形态：改动那段代码
必然触发这里。真正的端到端（造事件 → 跑 handler → 断言 reason 码）要拉起整个插件，
代价远高于它守的东西；等真有集成测试骨架时再补。
"""

import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_MAIN = os.path.join(os.path.dirname(_HERE), "main.py")


def _read_main() -> str:
    with open(_MAIN, "r", encoding="utf-8") as handle:
        return handle.read()


class MaterialsUsedWiringTest(unittest.TestCase):
    """证据卡里那三行必须还在，且顺序与嵌套关系不能变。"""

    def setUp(self):
        self.source = _read_main()

    def test_materials_branch_still_guards_the_ledger_write(self):
        self.assertIn('if cards["materials"]:', self.source)
        branch = self.source.index('if cards["materials"]:')
        tail = self.source[branch : branch + 400]
        self.assertIn(
            '_note_materials_used(core, cards["materials"])',
            tail,
            "materials 台账的写入点不见了：L-3 会重新变成「删掉也全绿」",
        )
        self.assertIn(
            'note_reason("materials_used"',
            tail,
            "记到台账后必须报 materials_used 码，否则排查时看不见",
        )

    def test_ledger_write_only_counts_when_used(self):
        # 返回值必须真的当条件用：`used_now = ...` 之后紧跟 `if used_now:`
        branch = self.source.index('if cards["materials"]:')
        tail = self.source[branch : branch + 400]
        self.assertRegex(
            tail,
            r"used_now = _note_materials_used\(core, cards\[\"materials\"\]\)\s*\n\s*if used_now:",
            "返回值没被当条件，0 条也会报码",
        )

    def test_wiring_sits_inside_the_evidence_card_block(self):
        evidence_at = self.source.index("evidence = _evidence_note(")
        branch = self.source.index('if cards["materials"]:')
        provenance_at = self.source.index("provenance = _provenance_line(")
        self.assertLess(evidence_at, branch, "台账写在证据卡生成之前？那用的还是空列表")
        self.assertLess(branch, provenance_at, "台账挪到出处段之后，语序与注释都会错位")


if __name__ == "__main__":
    unittest.main()
