"""跳过原因码（v3.1.4）：只做观测，不许改行为，也不许把内存吃穿。

三条底线：
1) 环缓冲有界——插件是常驻的，记多了不能一直涨；
2) 窗口按时间戳过滤——24 小时前的旧记录不该再出现在「最近」里；
3) 没记录时诊断报告里不出现这一节——绝大多数部署碰不到这些分支，
   空小节只会把真正的告警挤下去。
"""

from __future__ import annotations

import time
import unittest

from humanoid import diagnostics
from humanoid.config import HumanoidConfig


class _QuietResolver:
    def available_ids(self):
        return ["p1"]

    def resolve(self, provider_id):
        return None

    def resolve_global(self, umo=None):
        return None

    def id_of(self, provider):
        return "p1"


class _QuietGateway:
    gate = None

    def cooldown_remaining(self, provider_id, purpose):
        return 0.0

    def cooldowns(self, purpose):
        return {}

    def last_result(self, purpose):
        return None


def _report(**kwargs) -> str:
    return diagnostics.build_report(
        cfg=HumanoidConfig(),
        resolver=_QuietResolver(),
        gateway=_QuietGateway(),
        schedule_status={},
        activity_status={},
        version="0.0.0",
        **kwargs,
    )


class BufferTests(unittest.TestCase):
    def setUp(self):
        diagnostics.clear_reasons()

    def tearDown(self):
        diagnostics.clear_reasons()

    def test_buffer_is_bounded(self):
        # 超写一轮，只保留最后 capacity 条：常驻进程不能因为观测而涨内存。
        for _ in range(diagnostics.REASON_BUFFER_SIZE + 40):
            diagnostics.note_reason("ccb_cooldown")
        report = diagnostics.reason_report()
        self.assertEqual(report["stored"], diagnostics.REASON_BUFFER_SIZE)
        self.assertEqual(report["capacity"], diagnostics.REASON_BUFFER_SIZE)
        self.assertEqual(report["total"], diagnostics.REASON_BUFFER_SIZE)

    def test_empty_code_is_ignored(self):
        diagnostics.note_reason("")
        diagnostics.note_reason("   ")
        self.assertEqual(diagnostics.reason_report()["stored"], 0)

    def test_code_and_detail_are_truncated(self):
        diagnostics.note_reason("x" * 100, "y" * 200)
        item = diagnostics.reason_report()["top"][0]
        self.assertEqual(len(item["code"]), 48)
        self.assertEqual(len(item["last_detail"]), 80)

    def test_clear_reasons_empties_the_buffer(self):
        diagnostics.note_reason("inject_empty")
        diagnostics.clear_reasons()
        self.assertEqual(diagnostics.reason_report()["stored"], 0)
        self.assertEqual(diagnostics.reason_lines(), [])


class ReportTests(unittest.TestCase):
    def setUp(self):
        diagnostics.clear_reasons()

    def tearDown(self):
        diagnostics.clear_reasons()

    def test_report_structure(self):
        diagnostics.note_reason("ccb_cooldown", "30s")
        report = diagnostics.reason_report()
        self.assertEqual(
            set(report), {"window_seconds", "total", "capacity", "stored", "top"}
        )
        self.assertEqual(report["window_seconds"], diagnostics.REASON_WINDOW_SECONDS)
        self.assertEqual(report["total"], 1)
        self.assertEqual(report["stored"], 1)
        self.assertEqual(
            set(report["top"][0]), {"code", "count", "last_at", "last_detail"}
        )

    def test_counts_are_grouped_by_code(self):
        diagnostics.note_reason("ccb_cooldown")
        diagnostics.note_reason("ccb_cooldown")
        diagnostics.note_reason("inject_empty")
        report = diagnostics.reason_report()
        self.assertEqual(report["total"], 3)
        self.assertEqual(report["top"][0]["code"], "ccb_cooldown")
        self.assertEqual(report["top"][0]["count"], 2)

    def test_top_is_sorted_by_count_then_recency(self):
        diagnostics.note_reason("ccb_daily_cap")
        for _ in range(3):
            diagnostics.note_reason("closing_silence")
        diagnostics.note_reason("inject_empty")
        codes = [item["code"] for item in diagnostics.reason_report()["top"]]
        self.assertEqual(codes[0], "closing_silence")
        self.assertEqual(set(codes[1:]), {"ccb_daily_cap", "inject_empty"})

    def test_last_detail_keeps_the_latest_one(self):
        diagnostics.note_reason("ccb_not_qualified", "aff=40")
        diagnostics.note_reason("ccb_not_qualified", "aff=45")
        item = diagnostics.reason_report()["top"][0]
        self.assertEqual(item["last_detail"], "aff=45")

    def test_window_filters_old_entries(self):
        # 记一条，然后把「现在」推到窗口之外：它不该再被算进最近 24 小时。
        diagnostics.note_reason("background_after_stop")
        later = time.time() + diagnostics.REASON_WINDOW_SECONDS + 10.0
        report = diagnostics.reason_report(now=later)
        self.assertEqual(report["total"], 0)
        self.assertEqual(report["top"], [])
        self.assertEqual(report["stored"], 1)

    def test_custom_window_seconds_is_echoed(self):
        report = diagnostics.reason_report(window_seconds=600)
        self.assertEqual(report["window_seconds"], 600.0)


class ReasonLinesTests(unittest.TestCase):
    def setUp(self):
        diagnostics.clear_reasons()

    def tearDown(self):
        diagnostics.clear_reasons()

    def test_no_records_means_no_section(self):
        self.assertEqual(diagnostics.reason_lines(), [])

    def test_lines_show_code_and_count(self):
        for _ in range(3):
            diagnostics.note_reason("ccb_cooldown", "30s")
        diagnostics.note_reason("inject_empty")
        lines = diagnostics.reason_lines()
        self.assertTrue(lines)
        self.assertIn("【跳过原因码】", lines[0])
        self.assertIn("最近 24 小时共 4 次", lines[0])
        joined = "\n".join(lines)
        self.assertIn("ccb_cooldown ×3", joined)
        self.assertIn("inject_empty ×1", joined)
        self.assertIn("30s", joined)

    def test_lines_respect_the_limit(self):
        for index in range(6):
            for _ in range(index + 1):
                diagnostics.note_reason(f"code_{index}")
        lines = diagnostics.reason_lines(limit=2)
        body = [line for line in lines if line.startswith("- ")]
        self.assertEqual(len(body), 2)
        self.assertIn("code_5", body[0])

    def test_closing_codes_have_readable_chinese_labels(self):
        # 用户看的是「/你的状态」，不该对着一串英文码猜意思。
        diagnostics.note_reason("closing_silence")
        diagnostics.note_reason("closing_prompt")
        joined = "\n".join(diagnostics.reason_lines())
        self.assertIn("收尾静默", joined)
        self.assertIn("收尾提示", joined)

    def test_unknown_code_is_shown_raw_instead_of_crashing(self):
        diagnostics.note_reason("brand_new_code")
        joined = "\n".join(diagnostics.reason_lines())
        self.assertIn("brand_new_code ×1", joined)

    def test_report_section_appears_only_when_there_are_records(self):
        self.assertNotIn("【跳过原因码】", _report())
        diagnostics.note_reason("ccb_cooldown", "12s")
        text = _report()
        self.assertIn("【跳过原因码】", text)
        self.assertIn("ccb_cooldown ×1", text)
        self.assertIn("不落盘", text)


if __name__ == "__main__":
    unittest.main()
