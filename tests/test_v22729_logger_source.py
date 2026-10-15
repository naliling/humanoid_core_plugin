"""上架硬规则：`logger` 只能来自 `astrbot.api`，插件代码里不许出现标准库 `logging`。

v2.27.28 被上架自动安全审核退回，唯一必改项就是这个。审核原文点名的三处：
`humanoid/config.py`、`humanoid/prompt_builder.py`、`humanoid/services/mood.py`
——它们 `import logging` + `logging.getLogger(__name__)`，不符合「logger 必须且只能
`from astrbot.api import logger`，严禁内置 logging 模块」。

同一批被一起改掉的还有三个只把对象叫 `_LOG` 的模块（`diagnostics.py`、`jsonx.py`、
`outfit.py`）：审核是按变量名找的，所以只点了三个，但它们的违规性质完全相同。

这条守卫因此**按整棵生产树**扫，不按那三个文件名扫——只要生产代码里再出现
`import logging` / `from logging import ...` 就红，免得下次换个变量名又漏过去。
"""

from __future__ import annotations

import ast
import importlib
import logging
import unittest
from pathlib import Path

import astrbot.api

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
LOG_MODULE = "logging"

#: 生产源码：业务包 + 插件入口（`tests/` 自己要 import logging 做断言，不在此列）。
PRODUCTION_FILES = sorted((PLUGIN_ROOT / "humanoid").rglob("*.py")) + [PLUGIN_ROOT / "main.py"]

#: v2.27.29 起 logger 一律改从 `astrbot.api` 取的六个模块（点名的三个 + 同性质的三个）。
ASTROBOT_LOGGER_MODULES = (
    "humanoid.config",
    "humanoid.prompt_builder",
    "humanoid.diagnostics",
    "humanoid.jsonx",
    "humanoid.outfit",
    "humanoid.services.mood",
)


def _logging_imports(path: Path) -> list[str]:
    """AST 级检查：只看真正的 import 语句。

    不用文本正则，是因为「logging」会合法地出现在注释、docstring、变量名里
    （本仓库就有若干处解释这条规则的注释），正则会把它们误判成违规。
    """
    found: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == LOG_MODULE or alias.name.startswith(LOG_MODULE + "."):
                    found.append(f"line {node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == LOG_MODULE or module.startswith(LOG_MODULE + "."):
                names = ", ".join(a.name for a in node.names)
                found.append(f"line {node.lineno}: from {module} import {names}")
    return found


class NoStdlibLoggingTest(unittest.TestCase):
    """审核规则本身：生产代码里一个标准库 logging 都不许有。"""

    def test_production_tree_scanned(self):
        """确保下面那条扫描真的扫到了东西（否则它可能空转变绿）。"""
        relative = {p.relative_to(PLUGIN_ROOT).as_posix() for p in PRODUCTION_FILES}
        self.assertIn("main.py", relative)
        self.assertIn("humanoid/config.py", relative)
        self.assertIn("humanoid/prompt_builder.py", relative)
        self.assertIn("humanoid/services/mood.py", relative)
        # v2.27.29 实测：humanoid/ 下 38 个 .py + main.py = 39。留点余量但不设成 0。
        self.assertGreaterEqual(len(relative), 35, f"扫到的生产文件太少：{len(relative)}")

    def test_no_production_module_imports_stdlib_logging(self):
        offenders = {
            path.relative_to(PLUGIN_ROOT).as_posix(): _logging_imports(path)
            for path in PRODUCTION_FILES
        }
        offenders = {name: hits for name, hits in offenders.items() if hits}
        self.assertEqual(
            offenders,
            {},
            "上架规则：logger 必须且只能 `from astrbot.api import logger`，"
            f"严禁内置 logging 模块。违规文件：{offenders}",
        )


class AstrbotLoggerWiringTest(unittest.TestCase):
    """六个模块拿到的必须是 `astrbot.api.logger` 本体，不能是 `logging.getLogger` 的产物。"""

    def test_astrbot_api_logger_is_not_a_stdlib_logger(self):
        logger = astrbot.api.logger
        self.assertIsNotNone(logger)
        self.assertNotIsInstance(
            logger,
            logging.Logger,
            "astrbot.api.logger 应当是 AstrBot 的按模块解析代理，而不是某个固定 Logger",
        )
        for method in ("debug", "info", "warning", "error", "exception"):
            self.assertTrue(hasattr(logger, method), method)

    def test_每个模块的_logger_都来自_astrbot_api(self):
        for name in ASTROBOT_LOGGER_MODULES:
            with self.subTest(module=name):
                module = importlib.import_module(name)
                self.assertFalse(
                    hasattr(module, "logging"),
                    f"{name} 仍在模块命名空间里保留了标准库 logging",
                )
                attr = "logger" if hasattr(module, "logger") else "_LOG"
                self.assertTrue(
                    hasattr(module, attr), f"{name} 既没有 logger 也没有 _LOG"
                )
                self.assertIs(
                    getattr(module, attr),
                    astrbot.api.logger,
                    f"{name}.{attr} 不是 astrbot.api.logger（可能又用回了 logging.getLogger）",
                )

    def test_astrbot_logger_resolves_by_calling_module(self):
        """代理按调用方模块名解析——`assertLogs("humanoid.config")` 全靠这条才抓得到。"""
        with self.assertLogs(__name__, level="WARNING") as caught:
            astrbot.api.logger.warning("logger 解析探针")
        self.assertTrue(
            any("logger 解析探针" in line for line in caught.output), caught.output
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
