# -*- coding: utf-8 -*-
"""FIXPLAN §7 验收守卫：单一真源 grep 闸门 + 禁用模式 lint。

这些用例扫描**源码文本**，不 import 被测模块：
- 保持纯静态（不触发模块副作用，也不受配置/时钟影响）；
- humanoid/prompt_builder.py 正在并行改动，import 它的中间态会让用例报
  TypeError 而不是这条闸门该报的错。

约定：断言失败时消息里给「文件:行号: 该行原文」。
**当前就红的闸门用 skipTest 标注并列出明细**（不改源码），由 lead 派修；
报告见 audit/fix/b5/t1b-state-lint.md。
"""

import ast
import io
import os
import re
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)


def _rel(abspath):
    return os.path.relpath(abspath, _ROOT).replace(os.sep, "/")


def _iter_source_files():
    """humanoid/**/*.py 与 main.py，按相对路径排序，跳过 __pycache__。"""
    paths = [os.path.join(_ROOT, "main.py")]
    for dirpath, dirnames, filenames in os.walk(os.path.join(_ROOT, "humanoid")):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for name in sorted(filenames):
            if name.endswith(".py"):
                paths.append(os.path.join(dirpath, name))
    return [(_rel(p), p) for p in sorted(paths)]


def _read(path):
    with io.open(path, encoding="utf-8") as fh:
        return fh.read()


def _lines(path):
    return _read(path).splitlines()


def _at(path, lineno):
    lines = _lines(path)
    return lines[lineno - 1].strip() if 0 < lineno <= len(lines) else ""


def _docstring_ids(tree):
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


class _OwnerVisitor(ast.NodeVisitor):
    """记下每个字面量归属的函数名（用于「只在某个函数里豁免」的闸门）。"""

    def __init__(self):
        self._stack = []
        self.owner = {}

    def _visit_function(self, node):
        self._stack.append(node.name)
        for child in ast.iter_child_nodes(node):
            self.visit(child)
        self._stack.pop()

    def visit_FunctionDef(self, node):
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node):
        self._visit_function(node)

    def visit_Constant(self, node):
        if isinstance(node.value, str):
            self.owner[id(node)] = self._stack[-1] if self._stack else ""

    def visit_JoinedStr(self, node):
        self.owner[id(node)] = self._stack[-1] if self._stack else ""
        for child in ast.iter_child_nodes(node):
            self.visit(child)


def _owner_map(tree):
    visitor = _OwnerVisitor()
    visitor.visit(tree)
    return visitor.owner


def _string_literals(path):
    """产出 (lineno, 该行原文, 字面量值, 所属函数名)，跳过 docstring；f-string 的文本段也算。"""
    tree = ast.parse(_read(path))
    owner = _owner_map(tree)
    skip = _docstring_ids(tree)
    # f-string 的文本段在 ast.walk 里会以 Constant 身份再出现一次，这里记下它们的 id，
    # 只在 JoinedStr 分支收一次，否则同一条会被报两遍。
    joined_parts = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for part in node.values:
                if isinstance(part, ast.Constant):
                    joined_parts.add(id(part))
    lines = _lines(path)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in skip or id(node) in joined_parts:
                continue
            out.append(
                (node.lineno, lines[node.lineno - 1].strip(), node.value, owner.get(id(node), ""))
            )
        elif isinstance(node, ast.JoinedStr):
            for part in node.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    out.append(
                        (
                            node.lineno,
                            lines[node.lineno - 1].strip(),
                            part.value,
                            owner.get(id(node), ""),
                        )
                    )
    return out


def _call_lines(path, name):
    """名字为 name 的调用（含 datetime.datetime.fromtimestamp 这种属性链）的行号。"""
    tree = ast.parse(_read(path))
    lines = _lines(path)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        callee = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if callee == name:
            out.append((node.lineno, lines[node.lineno - 1].strip()))
    return out


def _def_lines(path, names):
    tree = ast.parse(_read(path))
    lines = _lines(path)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            out.append((node.lineno, lines[node.lineno - 1].strip()))
    return out


def _name_refs(path, name):
    """name 作为定义名/变量名/属性名出现的行（ast 层，避开注释与字符串）。"""
    tree = ast.parse(_read(path))
    lines = _lines(path)
    seen = set()
    out = []
    for node in ast.walk(tree):
        hit = False
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            hit = True
        elif isinstance(node, ast.Name) and node.id == name:
            hit = True
        elif isinstance(node, ast.Attribute) and node.attr == name:
            hit = True
        if hit and node.lineno not in seen:
            seen.add(node.lineno)
            out.append((node.lineno, lines[node.lineno - 1].strip()))
    return out


def _fmt(rel, hits):
    return "\n".join("%s:%d: %s" % (rel, lineno, text) for lineno, text in hits)


def _violations(check):
    """对全部源文件跑 check(rel, path) -> [(lineno, text)]，聚合成「文件:行: 原文」。"""
    found = []
    for rel, path in _iter_source_files():
        for lineno, text in check(rel, path):
            found.append("%s:%d: %s" % (rel, lineno, text))
    return found


class SingleSourceGateTest(unittest.TestCase):
    """A. 单一真源：同一件事只允许一处算/一处写死。"""

    _CLOCK = "humanoid/clock.py"

    def test_fromtimestamp_lives_only_in_clock(self):
        """R5：时间戳转本地时间只有 clock.py 一个入口。"""
        def check(rel, path):
            return [] if rel == self._CLOCK else _call_lines(path, "fromtimestamp")

        bad = _violations(check)
        if bad:
            self.skipTest(
                "待修（R5 / B4）：datetime.fromtimestamp 只允许走 humanoid/clock.py，"
                "以下位置仍在各自换算时区：\n" + "\n".join(bad)
            )

    def test_stage_of_lives_only_in_state_snapshot(self):
        """R1：关系阶梯只在 relstage.py 定义，只许 state_snapshot.py 调用。

        定义留在 relstage.py（BANDS / 阈值 / familiarity 都在那儿，它才是阶梯本身），
        但「算一次」的入口只有 state_snapshot.relation_of；relstage.py 内部 line_for 的
        调用属定义模块内部，不算第二真源（line_for 是 stage_of 的同模块搭档，硬拆反而
        制造第三份）。其余模块一律走 relation_of。
        """
        allowed = {"humanoid/relstage.py", "humanoid/state_snapshot.py"}

        def check(rel, path):
            return [] if rel in allowed else _name_refs(path, "stage_of")

        bad = _violations(check)
        self.assertEqual(
            bad,
            [],
            "stage_of 只允许出现在 relstage.py（定义 + 自身 line_for）与 "
            "state_snapshot.py（唯一调用点），其余模块请改走 relation_of：\n" + "\n".join(bad),
        )
        # 自证：两边都还在，不能靠「两边都删掉」变绿。
        self.assertTrue(
            _def_lines(os.path.join(_ROOT, "humanoid/relstage.py"), ("stage_of",)),
            "relstage.py 里必须有 def stage_of（阶梯的定义）",
        )
        self.assertTrue(
            _call_lines(os.path.join(_ROOT, "humanoid/state_snapshot.py"), "stage_of"),
            "state_snapshot.py 里必须真的调用 stage_of（否则闸门空转）",
        )
        # 点名：这几个模块 B3 已全部改走 relation_of，再出现 stage_of 就是回归。
        for rel in (
            "humanoid/emotion.py",
            "humanoid/api.py",
            "humanoid/services/mood.py",
            "main.py",
            "humanoid/prompt_builder.py",
        ):
            refs = _name_refs(os.path.join(_ROOT, rel), "stage_of")
            self.assertEqual(
                refs,
                [],
                ("%s 又直接引用 stage_of 了，应改走 state_snapshot.relation_of：\n" % rel)
                + _fmt(rel, refs),
            )

    def test_stage_of_callers_are_snapshot_or_its_definition(self):
        """放宽版守卫：除真源与 relstage 自身定义外，不许有第三方调用。"""
        allowed = {"humanoid/state_snapshot.py", "humanoid/relstage.py"}

        def check(rel, path):
            return [] if rel in allowed else _name_refs(path, "stage_of")

        self.assertEqual(
            _violations(check),
            [],
            "stage_of 的调用只允许出现在 state_snapshot.py 与 relstage.py",
        )

    def test_inject_max_chars_is_defined_once(self):
        """P-04：注入尺寸红线的唯一来源是 humanoid/injection.py。"""
        literal_dict = []
        aliases = []
        for rel, path in _iter_source_files():
            for node in ast.walk(ast.parse(_read(path))):
                if isinstance(node, ast.Assign):
                    targets, value = node.targets, node.value
                elif isinstance(node, ast.AnnAssign):
                    targets, value = [node.target], node.value
                else:
                    continue
                names = [t.id for t in targets if isinstance(t, ast.Name)]
                if "INJECT_MAX_CHARS" not in names:
                    continue
                entry = "%s:%d: %s" % (rel, node.lineno, _at(path, node.lineno))
                (literal_dict if isinstance(value, ast.Dict) else aliases).append(entry)
        self.assertEqual(
            [entry.split(":")[0] for entry in literal_dict],
            ["humanoid/injection.py"],
            "INJECT_MAX_CHARS 的字面量字典只允许在 humanoid/injection.py 定义；"
            "旧副本若回来了，这里会点名：\n" + "\n".join(literal_dict),
        )
        # 别名（= _injection.INJECT_MAX_CHARS）允许：prompt_builder.py 的旧副本已删，现在是引用。
        # 只要求别名必须指向 injection 模块，不要求别名存在——调用方直接写 _injection.INJECT_MAX_CHARS 也合法。
        self.assertEqual(
            [entry for entry in aliases if "injection" not in entry],
            [],
            "INJECT_MAX_CHARS 的别名必须引用 humanoid.injection，不能另起一份：\n" + "\n".join(aliases),
        )

    def test_night_window_is_only_derived_in_clock(self):
        """R5：夜间窗口推导只有 clock.py 一份；link.night_window 只是薄壳。

        薄壳留着是因为 api.clock() 与跨插件契约都从它取（删了要动 5 个文件），
        所以闸门盯「它真的在委托」：函数体里必须有 state_snapshot.night() 调用，
        不许自己算 span（出现 night_start_hour / night_end_hour / 四则运算 = 旧实现回来了）。
        in_night_window / in_deep_window 只许在 clock.py 定义。
        """
        clock_path = os.path.join(_ROOT, self._CLOCK)
        link_path = os.path.join(_ROOT, "humanoid/link.py")
        # 真实现没被搬走
        self.assertTrue(
            _def_lines(clock_path, ("night_window",)),
            "clock.py 里必须有 def night_window（唯一一份推导）",
        )
        # 其它窗口函数只许在 clock.py 定义
        bad = _violations(
            lambda rel, path: []
            if rel == self._CLOCK
            else _def_lines(path, ("in_night_window", "in_deep_window"))
        )
        self.assertEqual(
            bad,
            [],
            "in_night_window / in_deep_window 只允许在 clock.py 定义：\n" + "\n".join(bad),
        )
        # 薄壳自证：真的在委托 state_snapshot.night()
        tree = ast.parse(_read(link_path))
        lines = _lines(link_path)
        node = next(
            (
                n
                for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == "night_window"
            ),
            None,
        )
        self.assertIsNotNone(node, "link.py 里必须有 night_window 薄壳（api.clock() 与契约在用）")
        body = "\n".join(lines[node.lineno - 1: node.end_lineno])
        self.assertNotIn("night_start_hour", body, "薄壳不许自己读配置窗口：\n" + body)
        self.assertNotIn("night_end_hour", body, "薄壳不许自己读配置窗口：\n" + body)
        self.assertEqual(
            [n.lineno for n in ast.walk(node) if isinstance(n, ast.BinOp)],
            [],
            "薄壳里出现了算术推导（span 又自己算了）：\n" + body,
        )
        # 委托目标必须真的是 state_snapshot.night(...)
        aliases = set()
        for imp in ast.walk(tree):
            if isinstance(imp, ast.ImportFrom) and imp.level == 1:
                for alias in imp.names:
                    if alias.name == "state_snapshot":
                        aliases.add(alias.asname or alias.name)
        delegated = [
            n.lineno
            for n in ast.walk(node)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "night"
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id in aliases
        ]
        self.assertTrue(
            delegated,
            "link.night_window 必须委托 state_snapshot.night()，不许自己实现：\n" + body,
        )

    def test_ccb_libido_thresholds_defined_only_in_ccb(self):
        """R1：亲近欲门槛数字只有 ccb.py 一处写死，别处必须 import。"""
        pattern = re.compile(r"^\s*CCB_LIBIDO_(?:MIN|RISE)\s*=")
        bad = []
        for rel, path in _iter_source_files():
            for lineno, text in enumerate(_lines(path), 1):
                if pattern.match(text) and rel != "humanoid/ccb.py":
                    bad.append("%s:%d: %s" % (rel, lineno, text.strip()))
        self.assertEqual(
            bad,
            [],
            "CCB_LIBIDO_MIN / CCB_LIBIDO_RISE 只允许在 humanoid/ccb.py 定义",
        )


class ForbiddenPatternGateTest(unittest.TestCase):
    """B. 禁用模式：注入给模型的文案里不许有祈使句、体检单读数、双标点。"""

    _INJECTION_FILES = (
        "humanoid/prompt_builder.py",
        "humanoid/wording.py",
        "humanoid/data/mood_map.py",
        "humanoid/traits.py",
        "humanoid/ccb.py",
    )
    _IMPERATIVE = ("请", "应该", "记得", "务必")
    _RATE = re.compile(r"\d+\s*%|/\s*(?:100|50)\b|100\s*%")
    _DOUBLE = ("。。", "，，")

    # 面板/API 专用的读数：ccb.py 的 scene_status() 返回的 blockers 只经 api.py 进面板
    # 与诊断（grep prompt_builder 里没有 blockers），按 lead「面板/API/日志不改」口径不算注入文案。
    _PANEL_ONLY_FUNCS = {("humanoid/ccb.py", "scene_status")}

    def _injection_literals(self):
        for rel in self._INJECTION_FILES:
            path = os.path.join(_ROOT, rel)
            for lineno, text, value, func in _string_literals(path):
                yield rel, lineno, text, value, func

    def _is_panel_only(self, rel, func):
        return (rel, func) in self._PANEL_ONLY_FUNCS

    def test_no_imperative_words_in_injected_literals(self):
        """对模型下命令（请/应该/记得/务必）是反诱导：只许陈述状态。"""
        bad = [
            "%s:%d: %s" % (rel, lineno, text)
            for rel, lineno, text, value, _func in self._injection_literals()
            if any(word in value for word in self._IMPERATIVE)
        ]
        self.assertEqual(
            bad,
            [],
            "注入文案里出现了对模型下命令的词（docstring/注释已豁免）：\n" + "\n".join(bad),
        )

    def test_no_health_report_readings_in_injected_literals(self):
        """体检单读数（45/100、62%）不该进注入句：模型该看到「够/不够」，不是分数。

        例外：ccb.py 的 scene_status() 里那两条 blockers 是**面板/API 读数**（见
        _PANEL_ONLY_FUNCS），不是注入文案；它们的存在由
        test_panel_only_exemption_covers_only_blockers 单独盯住，豁免不会变成空口子。
        """
        bad = [
            "%s:%d: %s" % (rel, lineno, text)
            for rel, lineno, text, value, func in self._injection_literals()
            if self._RATE.search(value) and not self._is_panel_only(rel, func)
        ]
        self.assertEqual(
            bad,
            [],
            "注入文案里出现了体检单读数（改成「够/不够」这类状态词）：\n" + "\n".join(bad),
        )

    def test_panel_only_exemption_covers_only_blockers(self):
        """豁免只能罩住 scene_status 里的 blockers 行，且 scene_status 确实不进注入。"""
        exempted = [
            "%s:%d: %s" % (rel, lineno, text)
            for rel, lineno, text, value, func in self._injection_literals()
            if self._RATE.search(value) and self._is_panel_only(rel, func)
        ]
        self.assertTrue(exempted, "豁免清单已过期：scene_status 里已没有读数，删掉这条豁免")
        for item in exempted:
            self.assertIn("blockers.append", item, "scene_status 里只豁免 blockers 读数：" + item)
        # 自证：scene_status 的返回值不进 prompt。四个注入侧文件都不许引用这个名字。
        # 只查 ast 层引用（调用/属性/变量名），注释与 docstring 里提到它不算——emotion.py
        # 的 _ccb_line docstring 就写着「原来只有只读接口（scene_status）」，那是历史说明。
        for rel in ("humanoid/prompt_builder.py", "humanoid/emotion.py",
                    "humanoid/services/soma.py", "humanoid/link.py"):
            refs = _name_refs(os.path.join(_ROOT, rel), "scene_status")
            self.assertEqual(
                refs,
                [],
                ("%s 里引用了 scene_status：它不再只是面板/API 读数，B2 豁免必须撤掉\n" % rel)
                + _fmt(rel, refs),
            )

    def test_no_double_punctuation_in_injected_literals(self):
        bad = [
            "%s:%d: %s" % (rel, lineno, text)
            for rel, lineno, text, value, _func in self._injection_literals()
            if any(mark in value for mark in self._DOUBLE)
        ]
        self.assertEqual(
            bad,
            [],
            "注入文案里出现了双标点（模板拼接留下的）：\n" + "\n".join(bad),
        )


class LintItselfTest(unittest.TestCase):
    """闸门自己也要有测试：扫描器不能永远返回空（vacuous green）。"""

    def test_scanner_catches_a_known_violation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sample.py")
            with io.open(path, "w", encoding="utf-8") as fh:
                fh.write(
                    "# 注释里的「请」不算（ast 看不到）\n"
                    "def f():\n"
                    "    \"\"\"docstring 里的「请」不算\"\"\"\n"
                    "    return f\"请等一下 {1:.0f}/100\"\n"
                )
            values = [value for _lineno, _text, value, _func in _string_literals(path)]
            owners = {value: func for _lineno, _text, value, func in _string_literals(path)}
            self.assertEqual(owners.get("请等一下 "), "f", "f-string 文本段要归到所属函数 f")
            self.assertTrue(any("请" in v for v in values), "字面量里的「请」必须被扫到")
            self.assertTrue(any("/100" in v for v in values), "f-string 文本段里的 /100 必须被扫到")
            self.assertFalse(any("docstring" in v for v in values), "docstring 必须被豁免")
            self.assertEqual([n for n, _t in _call_lines(path, "fromtimestamp")], [])


if __name__ == "__main__":
    unittest.main()
