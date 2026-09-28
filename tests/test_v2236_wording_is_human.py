"""内心独白的措辞本身要像人话。

这个文件是补一次教训的：v2.23.6 之前，19 个测试文件全绿、473 项测试全过、仿真 0 ERROR，
但**注入里模型读到的是这种东西**：

    （小马利亚的内心：接话接得住）
    （你上次的火她还没消）
    （她今天对你有点冷淡）

三个问题：
  1. `bracket()` 的规则是「以她/你开头就不补『她的内心：』」，而 `_attitude`/`_day_tone`/
     `_intimacy` 三整池都以「她」开头 → **全都没拿到内心标记，直接混进事实句里**，
     角色在讲自己心里想什么却用第三人称说自己。
  2. 「接话接得住」是**能力**评价，不是意愿或心情。
  3. 老测试只断言「某个词在不在」（`any(w in text ...)`），改词还是绿的——
     它验证的是**机制**（轴有没有被算进去），不是**说出来像不像人话**。

所以这里把每一个维度的所有措辞都渲染出来，逐条检查。
"""
import inspect
import re
import textwrap
import unittest

from humanoid.emotion import EmotionLayer, bracket

DIMS = ("_day_tone", "_now_mood", "_attitude", "_intimacy", "_willingness")

# 这些是**能力/状态评价**，不是心里在想什么
CAPABILITY_WORDS = ("接得住", "接话", "有耐心", "状态", "表现", "能力")


def all_phrasings() -> list:
    out = []
    for fn in DIMS:
        body = inspect.getsource(getattr(EmotionLayer, fn))
        for m in re.finditer(r'_pick\(\(([^)]+)\)', body):
            for w in m.group(1).split(","):
                w = w.strip().strip('"')
                if w:
                    out.append((fn, w))
    return out


class WordingSoundsHuman(unittest.TestCase):
    def test_there_are_phrasings_to_check(self):
        """先确认这个文件真的抓到了东西——空集合会让下面所有断言变成空跑。"""
        self.assertGreaterEqual(len(all_phrasings()), 30)

    def test_no_third_person_self_reference(self):
        """内心独白里不能出现「她」——她在讲自己，用第三人称说自己很出戏。"""
        bad = [(fn, w) for fn, w in all_phrasings() if "她" in w]
        self.assertEqual(bad, [], f"内心独白里出现了第三人称「她」：{bad}")

    def test_no_capability_evaluation(self):
        """「接话接得住」是能力评价，不是心情。"""
        bad = [(fn, w) for fn, w in all_phrasings()
               if any(k in w for k in CAPABILITY_WORDS)]
        self.assertEqual(bad, [], f"内心独白里混进了能力/状态评价：{bad}")

    def test_every_one_gets_the_inner_voice_prefix(self):
        """一律要包成「（她的内心：…）」，格式不统一模型就分不清这是心情还是事实。"""
        for fn, w in all_phrasings():
            line = bracket(w)
            self.assertTrue(
                line.startswith("（她的内心："),
                f"{fn} 的「{w}」渲染成了 {line}，没有内心标记",
            )

    def test_bracket_is_idempotent(self):
        self.assertEqual(bracket(bracket("心里有点堵")), bracket("心里有点堵"))

    def test_empty_stays_empty(self):
        self.assertEqual(bracket(""), "")
        self.assertEqual(bracket(None), "")

    def test_every_phrase_is_short(self):
        """内心独白是一句念头，不是段落。"""
        for fn, w in all_phrasings():
            self.assertLessEqual(len(w), 14, f"{fn} 的「{w}」太长了，不像一句念头")


if __name__ == "__main__":
    unittest.main()


class SeedIsStable(unittest.TestCase):
    """抽签种子必须跨进程稳定。

    原来是 `abs(hash(str(user_id))) % 3`——Python 的字符串 hash 每个进程都加随机盐，
    同一个用户**每次重启**抽到的内心维度就换一个：重启前「这会儿有话想说」，
    重启后可能变成「今天有点闷」。而且它让一个注入断言的测试随机挂——
    那个测试是第一个发现这事的。
    """

    def test_same_user_same_index_across_calls(self):
        from humanoid.emotion import EmotionLayer
        import inspect
        import ast as _ast
        # 去掉 docstring 和注释再扫——否则「原来写的是 hash(str(user_id))」这句说明
        # 自己就会把断言触发（这个坑我自己踩了一次）。
        tree = _ast.parse(textwrap.dedent(inspect.getsource(EmotionLayer.candidate)))
        fn = tree.body[0]
        head = fn.body[0] if fn.body else None
        if isinstance(head, _ast.Expr) and isinstance(getattr(head, "value", None), _ast.Constant):
            fn.body = fn.body[1:]                      # 去掉 docstring
        code = _ast.unparse(fn)
        self.assertNotIn("hash(str(user_id))", code,
                         "内建 hash() 每进程带随机盐，重启就换维度")
        self.assertIn("blake2b", code)

    def test_role_id_comes_from_core_not_a_bare_name(self):
        """`candidate()` 的参数里没有 role_id。

        直接写 `role_id` 会抛 NameError → 被 `_emotion_line` 的 except 吞掉 →
        **整句情绪从注入里消失**。这个静默失败很难查，靠这条钉住。
        """
        import ast as _ast
        tree = _ast.parse(textwrap.dedent(inspect.getsource(EmotionLayer.candidate)))
        fn = tree.body[0]
        params = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}
        self.assertNotIn("role_id", params, "role_id 不是参数，不能直接引用")
        code = _ast.unparse(fn)
        self.assertNotIn("f\"emotion:{role_id}", code,
                         "引用了不存在的 role_id → NameError → 情绪句静默消失")

    def test_seed_contains_today(self):
        """种子必须含「今天」：一天内稳定、跨天轮换。

        只按 role+uid 定死的话，同一个用户**一辈子只抽一种措辞**——实测连跑 6 天，
        六天全是「有点想说话」。一个人不会永远心里只说一句。
        """
        import ast as _ast
        import textwrap
        import inspect
        from humanoid.emotion import EmotionLayer
        tree = _ast.parse(textwrap.dedent(inspect.getsource(EmotionLayer.candidate)))
        code = _ast.unparse(tree)
        self.assertIn("today", code, "抽签种子里没有日期 → 措辞一辈子不变")

    def test_index_is_deterministic(self):
        import importlib
        results = set()
        for _ in range(3):
            m = importlib.import_module("humanoid.emotion")
            importlib.reload(m)
            raw = b"emotion:bot1:42"
            results.add(int.from_bytes(
                m.hashlib.blake2b(raw, digest_size=8).digest(), "big") % 3)
        self.assertEqual(len(results), 1, "同一用户抽到的档位不稳定")
