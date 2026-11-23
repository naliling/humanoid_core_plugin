"""v3.1.51：修「出站守卫误删角色感句子」——bot 说话不像角色的直接根因。

真机证据（2026-11-23 astrbot.log，26 条出站拦截）：被删的几乎全是角色感最强的句子——
动作神态（挑起眉毛轻笑了一声 / 斜眼睨着你）、逐日递进的天气（外头正落着四度寒雨
→ 三度寒雨）、角色口吻（有本宫的翅膀护着）、感官细节（鼻尖冻得发麻）。
根因两处：
1. 照抄闸默认开，而注入框架用第一人称鼓励她把状态说出口，两处设计直接打架；
2. 复读闸用「6 字相互包含 + 地点出现 2 次」把递进天气和习惯动作当复读。

本测试锁：照抄闸默认关；放宽后角色感句子保住；真正的原样复读仍被删。
"""
from __future__ import annotations

import unittest

from humanoid import config as config_mod
from humanoid import reply_guard


class DefaultsTest(unittest.TestCase):
    def test_echo_guard_defaults_off(self):
        """照抄闸默认关：不再把她说出口的身体/感官句删掉。"""
        self.assertFalse(config_mod.DEFAULTS.injection_echo_guard_enabled)

    def test_repeat_thresholds_are_pinned(self):
        self.assertEqual(config_mod.DEFAULTS.repeat_dup_min_chars, 10)
        self.assertEqual(config_mod.DEFAULTS.repeat_dup_max_len_delta, 2)
        self.assertEqual(config_mod.DEFAULTS.repeat_place_only_tail_chars, 6)

    def test_config_from_raw_picks_up_thresholds(self):
        built = config_mod.HumanoidConfig.from_raw({
            "repeat_dup_min_chars": 12,
            "repeat_dup_max_len_delta": 3,
            "repeat_place_only_tail_chars": 4,
        })
        self.assertEqual(built.repeat_dup_min_chars, 12)
        self.assertEqual(built.repeat_dup_max_len_delta, 3)
        self.assertEqual(built.repeat_place_only_tail_chars, 4)


class CharacterVoiceKeptTest(unittest.TestCase):
    """放宽后：角色的动作神态、递进天气、感官句都不再被误删。"""

    def test_weather_progression_is_not_a_repeat(self):
        recent = ["外头正落着四度寒雨呀"]
        text = "外头正落着三度寒雨。"
        new, dropped = reply_guard.trim_repeats(text, recent)
        self.assertEqual(dropped, [])
        self.assertEqual(new, text)

    def test_place_plus_action_is_not_a_repeat(self):
        recent = ["我在厨房呢，刚把碗洗完"]
        text = "我在厨房，顺手把灶台也擦了一把。"
        new, dropped = reply_guard.trim_repeats(text, recent)
        self.assertEqual(dropped, [])
        self.assertEqual(new, text)

    def test_sensory_detail_is_not_a_repeat(self):
        recent = ["鼻尖冻得发麻，指尖也凉透了"]
        text = "鼻尖冻得发麻，呼出的气都成了白雾。"
        new, dropped = reply_guard.trim_repeats(text, recent)
        self.assertEqual(dropped, [])
        self.assertEqual(new, text)

    def test_pure_place_report_is_still_dropped(self):
        """真正的报备句（整句几乎只有地点）仍要删——护栏没有失效。"""
        recent = ["我在厨房呢", "厨房里还有点活儿"]
        new, dropped = reply_guard.trim_repeats("我在厨房呢。", recent)
        self.assertEqual(dropped, ["我在厨房呢"])
        self.assertTrue(new.strip())

    def test_verbatim_clause_is_still_dropped(self):
        """一字不差的长句原样复读仍要删（其余小句保住，不发空）。"""
        recent = ["她把炉子添了块柴，屋里慢慢暖起来，你坐会儿。" ]
        text = "她把炉子添了块柴，屋里慢慢暖起来，你回来啦？"
        new, dropped = reply_guard.trim_repeats(text, recent)
        self.assertTrue(dropped)
        self.assertIn("你回来啦", new)
        self.assertNotIn("把炉子添了块柴", new)


if __name__ == "__main__":
    unittest.main()
