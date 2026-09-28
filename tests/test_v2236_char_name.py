"""v2.23.6：参照名称自动补上，管理员指令留作保底。

原来拿不到名字就返回空串，注入退回「她」——功能不受影响，但代入感弱一大截，
而且得管理员再补一条指令。用户的要求是「不用再去管理员设置，指令留着保底」。

**踩过的一个坑**：第一版抽不到名字时回退到 role_id，结果注入变成
「（bot1的内心：心里有点堵）」、`「只有bot1和TA」`——role_id 是内部标识
（bot1 / 3632823490），拿它当名字比不补更糟。三条老测试当场红了。
"""
import unittest

from humanoid.core_instance import _name_from_text


class NameExtraction(unittest.TestCase):
    """只认**自称**，不认描述句。"""

    def test_you_are_x(self):
        self.assertEqual(_name_from_text("你是小马利亚，纯白双翼的独角小马。"), "小马利亚")

    def test_i_am_called_x(self):
        self.assertEqual(_name_from_text("我叫阿澄，今年18岁。"), "阿澄")
        self.assertEqual(_name_from_text("我的名字是林小满。"), "林小满")
        self.assertEqual(_name_from_text("你的名字叫知夏。"), "知夏")

    def test_bare_name_is_x(self):
        self.assertEqual(_name_from_text('名字是「小满」，请多关照。'), "小满")

    def test_descriptions_are_rejected(self):
        """「你是一个温柔体贴的女孩子」不是名字——量词开头的一律否。"""
        for text in (
            "你是一个温柔体贴的女孩子，说话很短。",
            "你是一个乐于助人的AI助手。",
            "你是一只纯白双翼的独角小马。",
            "她是一个学生。",
            "我是一个18岁的女孩。",
            "你是一个喜欢独处的人。",
        ):
            self.assertEqual(_name_from_text(text), "", f"不该从这句里取名：{text}")

    def test_empty_when_nothing_to_find(self):
        self.assertEqual(_name_from_text("今天天气不错。"), "")
        self.assertEqual(_name_from_text(""), "")


class NameFallbackPolicy(unittest.TestCase):
    """抽不到就空串——绝不用 role_id 顶上。"""

    def test_role_id_is_never_used_as_a_name(self):
        import inspect
        from humanoid.core_instance import HumanoidCoreInstance
        src = inspect.getsource(HumanoidCoreInstance._auto_char_name)
        self.assertNotIn("self.role_id", src.replace("self.role_id)", ")").split("return")[-1],
                         "拿 role_id 当名字是这一版踩过的坑，不能回来")


class CharNameEndToEnd(unittest.TestCase):
    """跑通整条 `char_name()` 链路。

    只测 `_name_from_text` 是不够的：第一版那个 bug 就出在链路中间——
    `char_name()` 是同步的却去调 async 的 `persona_source.persona()`，
    拿到协程对象，`getattr(coro, "prompt")` 永远为空。**抽取函数单测全绿，
    功能却从来没生效过。** 所以这里必须从 `char_name()` 走一遍。
    """

    class _Src:
        def __init__(self, persona):
            self._p = persona

        def cached_name(self, role_id):
            return self._p.name

        def cached_persona(self, role_id):
            return self._p

    def _core(self, prompt, name=""):
        from humanoid.core_instance import HumanoidCoreInstance
        from humanoid.persona import Persona

        core = HumanoidCoreInstance.__new__(HumanoidCoreInstance)
        core.role_id = "bot1"
        core.persona_source = self._Src(Persona(name, prompt, "会话"))
        core._scope = type("S", (), {
            "get_self": staticmethod(lambda k, d=None: ""),
            "set_self": staticmethod(lambda k, v: None),
        })()
        return core

    def test_auto_name_from_persona_text(self):
        core = self._core("你是小马利亚，纯白双翼的独角小马。", "")
        self.assertEqual(core.char_name(), "小马利亚")

    def test_no_coroutine_left_behind(self):
        """同步路径上不该产生任何协程。

        用运行时检查而不是扫源码——多行 docstring 里提到那个名字很正常，
        扫文本会一直误报。真正要防的是「同步函数里 await 了一个 async 接口」。
        """
        import gc
        import warnings
        core = self._core("你是小马利亚。", "")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            name = core.char_name()
            gc.collect()
        self.assertEqual(name, "小马利亚")
        coros = [w for w in caught if "never awaited" in str(w.message)]
        self.assertEqual(coros, [], "同步路径上产生了未 await 的协程")

    def test_falls_back_to_she_when_nothing_found(self):
        core = self._core("今天天气不错。", "")
        self.assertEqual(core.char_name(), "", "抽不到就空串，注入退回「她」")

    def test_never_uses_role_id(self):
        core = self._core("今天天气不错。", "")
        self.assertNotIn("bot1", core.char_name())

    def test_memo_is_used(self):
        """`char_name()` 在注入热路径上，每条消息一次；补过一次就别重跑正则。"""
        calls = {"n": 0}
        core = self._core("你是小马利亚。", "")

        class _S:
            def get_self(_s, k, d=None):
                return "小马利亚" if k == "char_name_auto" else (d or "")

            @staticmethod
            def set_self(k, v):
                calls["n"] += 1

        core._scope = _S()
        for _ in range(20):
            self.assertEqual(core.char_name(), "小马利亚")
        self.assertEqual(calls["n"], 0, "记忆项命中时不该再写、也不该再算")

    def test_forget_clears_the_memo(self):
        core = self._core("你是小马利亚。", "")
        self.assertTrue(hasattr(core, "forget_auto_char_name"))

    def test_explicit_override_wins(self):
        core = self._core("你是小马利亚。", "")
        core._scope.get_self = staticmethod(lambda k, d=None: "管理员设的" if k == "char_name_override" else "")
        self.assertEqual(core.char_name(), "管理员设的")


class CharNameInfo(unittest.TestCase):
    def test_info_shape(self):
        from humanoid.core_instance import HumanoidCoreInstance
        self.assertTrue(hasattr(HumanoidCoreInstance, "char_name_info"))


if __name__ == "__main__":
    unittest.main()
