"""v3.1.8 隐私加固（审核 M-1 / M-2 / L-9）。

这几条的共性是：修没修，**行为测试都不会变红**——权限没收紧、日志里多打了
几千字聊天原文，功能照常跑。所以这里钉两件事：

一、日志里不许再出现聊天原文（`<user_message>` 的内容、「她说」的原话）与完整 QQ 号；
二、状态文件与它所在目录在 POSIX 上必须是 600 / 700。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from .astrbot_stub import install as install_astrbot_stub

install_astrbot_stub()

from humanoid import state as state_module  # noqa: E402
from humanoid.services.mood import _redact_prompt_for_log  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class RedactPromptTest(unittest.TestCase):
    """M-2：debug 日志里的 prompt 必须脱敏。"""

    def test_user_message_content_is_hidden(self) -> None:
        prompt = '<user_message n="1">我今天被老板骂了，很难受</user_message>'
        out = _redact_prompt_for_log(prompt)
        self.assertNotIn("老板", out)
        self.assertNotIn("很难受", out)
        self.assertIn("[已脱敏", out)
        # 结构要留着，排查时才知道送了几条
        self.assertIn('<user_message n="1">', out)
        self.assertIn("</user_message>", out)

    def test_spoke_lines_are_hidden(self) -> None:
        prompt = "  · 她说「我等你到十点」——对方接住了"
        out = _redact_prompt_for_log(prompt)
        self.assertNotIn("我等你到十点", out)
        self.assertIn("她说「已脱敏」", out)
        self.assertIn("对方接住了", out)

    def test_structure_lines_survive(self) -> None:
        prompt = "【她在这之前说过的，以及对方有没有接住】"
        self.assertEqual(prompt, _redact_prompt_for_log(prompt))


class TightenPermissionsTest(unittest.TestCase):
    """M-1 / L-7：POSIX 上 600 / 700，Windows 上静默跳过。"""

    def test_posix_uses_600_and_700(self) -> None:
        path = Path(tempfile.gettempdir()) / "hc318_state.json"
        calls: list[tuple[Path, int]] = []
        with (
            mock.patch.object(state_module.os, "name", "posix"),
            mock.patch.object(
                state_module.os,
                "chmod",
                side_effect=lambda p, m: calls.append((str(p), m)),
            ),
        ):
            state_module._tighten_permissions(path)
        self.assertEqual(
            [(str(path), 0o600), (str(path.parent), 0o700)],
            calls,
        )

    def test_windows_is_a_noop(self) -> None:
        path = Path(tempfile.gettempdir()) / "hc318_state.json"
        calls: list[tuple[Path, int]] = []
        with (
            mock.patch.object(state_module.os, "name", "nt"),
            mock.patch.object(
                state_module.os,
                "chmod",
                side_effect=lambda p, m: calls.append((str(p), m)),
            ),
        ):
            state_module._tighten_permissions(path)
        self.assertEqual([], calls)

    def test_atomic_write_tightens_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "state.json"
            calls: list[tuple[Path, int]] = []
            with (
                mock.patch.object(state_module.os, "name", "posix"),
                mock.patch.object(
                    state_module.os,
                    "chmod",
                    side_effect=lambda p, m: calls.append((str(p), m)),
                ),
            ):
                state_module._atomic_write(target, "{}")
            self.assertTrue(target.is_file())
            self.assertIn((str(target), 0o600), calls)
            self.assertIn((str(target.parent), 0o700), calls)


class LogLeakGuardTest(unittest.TestCase):
    """静态守卫：别再有人把完整 prompt / QQ 号塞回日志。"""

    def test_mood_prompt_is_not_logged_whole(self) -> None:
        text = (ROOT / "humanoid" / "services" / "mood.py").read_text(encoding="utf-8")
        self.assertIn("_redact_prompt_for_log(prompt)", text)
        self.assertNotIn("情绪分析请求: {prompt}", text)

    def test_sibling_logs_mask_the_id(self) -> None:
        text = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("兄弟 bot {user_id}", text)
        self.assertIn("def _mask_id(", text)
        self.assertIn('f"{text[:3]}***{text[-2:]}"', text)


class ReadmeDisclosureTest(unittest.TestCase):
    """M-1 的另一半：README 必须明写「会存原话」与存在哪。"""

    def test_readme_discloses_storage(self) -> None:
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("## 数据与边界", text)
        self.assertIn("data/plugin_data/humanoid_core/state.json", text)
        self.assertIn("600", text)
        self.assertIn("700", text)
