"""v3.1.7：注入侧把「这一轮摆出来的素材」记进 materials_used。

写侧在 main.py，读侧在 humanoid/services/life_material.py + humanoid/anchors.py。
为什么写侧在注入处：Core 看不到 social 发出去的正文，能确定的事实只有「这张卡
这一轮出现在她眼前的材料里」；保守去重的代价是偶尔少提一件旧事，收益是同一件
小事不会被连着几天反复端上来。

这里只测写侧：记什么、怎么去重、坏数据怎么 fail-open。
"""

from __future__ import annotations

import time
import types
import unittest

from .astrbot_stub import install as install_astrbot_stub

install_astrbot_stub()

from .test_main_integration import SKIP_REASON, _load_main  # noqa: E402

from humanoid.services import life_material  # noqa: E402

try:  # pragma: no cover - 取决于运行环境
    main_module = _load_main()
except Exception as exc:  # pragma: no cover
    main_module = None
    SKIP_REASON = f"无法加载 main.py：{exc}"


class FakeScope:
    """RoleScope 的最小替身：只实现写侧真正用到的那两个方法。"""

    def __init__(self, data=None) -> None:
        self.data = dict(data or {})
        self.updates = 0

    def get_self(self, key, default=None):
        return self.data.get(key, default)

    def update_self(self, key, value):
        self.updates += 1
        self.data[key] = value


def _card(card_id: str, anchor_key: str = "", title: str = "在楼下看到一只猫") -> dict:
    return {"id": card_id, "anchor_key": anchor_key or f"life:{card_id}", "title": title}


@unittest.skipIf(main_module is None, "main.py 加载失败")
class NoteMaterialsUsedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = time.time()
        self.core = types.SimpleNamespace(scope=FakeScope())

    def _rows(self):
        return self.core.scope.get_self(life_material.MATERIALS_USED_KEY, [])

    def test_records_each_card(self):
        got = main_module._note_materials_used(
            self.core, [_card("c1"), _card("c2")], self.now
        )
        self.assertEqual(got, 2)
        rows = self._rows()
        self.assertEqual([r["card_id"] for r in rows], ["c1", "c2"])
        self.assertEqual(rows[0]["anchor_key"], "life:c1")
        self.assertAlmostEqual(rows[0]["used_at"], self.now, places=3)

    def test_same_card_overwrites_instead_of_duplicating(self):
        main_module._note_materials_used(self.core, [_card("c1")], self.now - 10)
        main_module._note_materials_used(self.core, [_card("c1")], self.now)
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["used_at"], self.now, places=3)

    def test_capped_at_limit(self):
        cards = [_card(f"c{i}") for i in range(140)]
        main_module._note_materials_used(self.core, cards, self.now)
        self.assertEqual(len(self._rows()), life_material.MATERIALS_USED_LIMIT)
        # 丢的是最旧的：最新的 128 张还在
        ids = {r["card_id"] for r in self._rows()}
        self.assertIn("c139", ids)
        self.assertNotIn("c0", ids)

    def test_empty_cards_is_noop(self):
        self.assertEqual(main_module._note_materials_used(self.core, [], self.now), 0)
        self.assertEqual(main_module._note_materials_used(self.core, None, self.now), 0)
        self.assertEqual(self._rows(), [])

    def test_non_dict_cards_ignored(self):
        got = main_module._note_materials_used(self.core, ["x", 3, _card("c1")], self.now)
        self.assertEqual(got, 1)
        self.assertEqual([r["card_id"] for r in self._rows()], ["c1"])

    def test_card_without_id_or_anchor_is_dropped(self):
        got = main_module._note_materials_used(self.core, [{"title": "没 id"}], self.now)
        self.assertEqual(got, 1)
        self.assertEqual(self._rows(), [])

    def test_missing_scope_fails_open(self):
        for core in (types.SimpleNamespace(), types.SimpleNamespace(scope=None)):
            self.assertEqual(
                main_module._note_materials_used(core, [_card("c1")], self.now), 0
            )

    def test_scope_without_methods_fails_open(self):
        core = types.SimpleNamespace(scope=object())
        self.assertEqual(
            main_module._note_materials_used(core, [_card("c1")], self.now), 0
        )

    def test_broken_scope_fails_open(self):
        class Broken(FakeScope):
            def update_self(self, key, value):
                raise RuntimeError("state.json 写不进去")

        core = types.SimpleNamespace(scope=Broken())
        self.assertEqual(
            main_module._note_materials_used(core, [_card("c1")], self.now), 0
        )

    def test_bad_now_falls_back_to_wall_clock(self):
        got = main_module._note_materials_used(self.core, [_card("c1")], "not-a-number")
        self.assertEqual(got, 1)
        self.assertGreater(self._rows()[0]["used_at"], self.now - 5)

    def test_written_rows_are_what_the_read_side_filters(self):
        main_module._note_materials_used(self.core, [_card("c1")], self.now)
        keys = life_material.used_keys(self._rows(), self.now)
        self.assertIn("c1", keys)
        self.assertIn("life:c1", keys)
        # 7 天以后不再算「刚用过」
        later = self.now + life_material.MATERIALS_USED_WINDOW_SECONDS + 1
        self.assertEqual(life_material.used_keys(self._rows(), later), set())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
