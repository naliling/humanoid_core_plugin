"""v3.0.7：开关必须做到它承诺的事（配置面板诚实性守卫）。

背景（用户 2026-11-05）：`_conf_schema.json` 里有一批开关，拨了不会发生它承诺的事。
这里钉死其中两条**能用静态事实判定**的，因为它们的失效方式都不会让任何行为测试变红：

一、`debug_mode` 打开后什么都不打印。
   AstrBot 把插件 logger 的级别设成全局 `log_level`（默认 INFO，
   backend/app/astrbot/core/log.py:571-599、default.py:307），而插件里这些开关门禁的
   全是 `logger.debug(...)` —— 记录在 logger 层就被丢掉，插件内也没有任何 setLevel。
   所以：**受这个开关门禁的日志必须是 info 级**。改回 debug 就红。

   （同类的 `mood_verbose_log` 已删：它和 `debug_mode` 都是「只影响日志」的开关，
   三个开关干一件事。删掉后它的三处日志并进 `debug_mode`。）

二、打断四项（安静间隔/冷却/保鲜期/叫住她就打断）只在大模型排日程的链路上被消费
   （humanoid/services/schedule.py:2658 `if cfg.use_llm_schedule and ...`），模板日程
   完全不读来访。所以它们必须带 `condition: {use_llm_schedule: true}`，否则面板上
   照常可拨、拨了没有任何效果。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "_conf_schema.json"

# 受这些开关门禁的日志必须是 info 级（见模块 docstring 第一条）
SWITCH_NAMES = ("debug_mode",)
# 回看几行算「同一段代码」：开关判断与日志调用之间通常只隔一行续行
LOOKBACK = 4


def _source_files() -> list[Path]:
    files = sorted((ROOT / "humanoid").rglob("*.py"))
    main = ROOT / "main.py"
    if main.is_file():
        files.append(main)
    return files


class SwitchGatedLogLevelTest(unittest.TestCase):
    """开关门禁的日志必须真的打得出来（info 级），不许再用 debug 级。"""

    def test_no_debug_call_under_a_log_switch(self) -> None:
        offenders: list[str] = []
        for path in _source_files():
            lines = path.read_text(encoding="utf-8").splitlines()
            for index, line in enumerate(lines):
                if ".debug(" not in line:
                    continue
                window = "\n".join(lines[max(0, index - LOOKBACK) : index + 1])
                for name in SWITCH_NAMES:
                    if name in window:
                        rel = path.relative_to(ROOT).as_posix()
                        offenders.append(f"{rel}:{index + 1}")
                        break
        self.assertEqual(
            [],
            offenders,
            "这些日志挂在 debug_mode 上却用 debug 级——"
            "AstrBot 默认 INFO，用户打开开关也看不到任何输出；改成 info 级：" + ", ".join(offenders),
        )


class DeletedSwitchTest(unittest.TestCase):
    """删掉的开关不许回到面板上（v3.0.7 删 mood_verbose_log）。"""

    def test_mood_verbose_log_is_gone(self) -> None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertNotIn("mood_verbose_log", schema)
        for path in _source_files():
            self.assertNotIn(
                "mood_verbose_log",
                path.read_text(encoding="utf-8"),
                f"{path.relative_to(ROOT).as_posix()} 还在读一个已经删掉的开关",
            )


class InterruptConditionTest(unittest.TestCase):
    """打断四项只在大模型排日程时生效，必须跟着主开关一起显示/隐藏。"""

    KEYS = (
        "schedule_interrupt_quiet_minutes",
        "schedule_interrupt_cooldown_minutes",
        "schedule_interrupt_ttl_minutes",
        "schedule_interrupt_on_call",
    )

    def setUp(self) -> None:
        self.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def test_every_interrupt_setting_follows_use_llm_schedule(self) -> None:
        for key in self.KEYS:
            with self.subTest(key=key):
                self.assertIn(key, self.schema)
                self.assertEqual(
                    {"use_llm_schedule": True},
                    self.schema[key].get("condition"),
                    f"{key} 在模板日程下完全无效（schedule.py:2658 只在 use_llm_schedule 时消费来访），"
                    "面板上必须跟着 use_llm_schedule 隐藏",
                )


if __name__ == "__main__":
    unittest.main()
