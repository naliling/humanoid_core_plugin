"""B7 / #154：`soma.feelings()` 里 `say()` 的 `span` 死参数已删。

分制在 v3.0.x 就从 `(value - floor) / span` 改成了「按档位给分」，但参数没跟着删：
六处调用照旧传 45/5/40/60，函数体一次都没读过 `span`。这一组测试钉两件事：
1. 签名里没有 `span`（否则后来的人会把它当成活的旋钮去调）；
2. 分制的实际口径没变：顶档拿满 `top`、中间档低于顶档、反向轴（没精神）低值拿满顶档
   并越过 prompt 侧 0.7 的门槛。
"""

from __future__ import annotations

import inspect
import re
import unittest

from humanoid.services.soma import SomaService
from tests.test_expression import DAY_SCHEDULE, SomaFixture, cfg, TZ  # noqa: F401

from datetime import datetime

START = datetime(2026, 8, 22, 9, 0, tzinfo=TZ)


def _fixture() -> SomaFixture:
    return SomaFixture(START, cfg(enable_cycle=False), DAY_SCHEDULE)


class SayTakesNoSpanTest(unittest.TestCase):
    def test_the_dead_span_parameter_is_gone(self):
        """`say()` 的签名必须正好是 (kind, value, floor, top, reverse)。

        `span` 从分制改档位那天起就没被读过；留着它，调参的人会改 45.0 以为能调分。
        """
        src = inspect.getsource(SomaService.feelings)
        match = re.search(r"def say\((.*?)\)\s*->\s*str:", src, re.S)
        self.assertIsNotNone(match, "找不到 say() 的定义，测试失效了")
        params = re.sub(r"\s+", " ", match.group(1)).strip()
        self.assertNotIn("span", params, f"死参数 span 又回来了：{params}")
        self.assertEqual(
            params,
            "kind: str, value: float, floor: float, top: float, reverse: bool = False",
        )


class BandScoreTest(unittest.TestCase):
    """分制跟着词走：顶档 = 满 `top`，往下按档位递减。"""

    def _quiet(self, soma) -> None:
        # 只留饥饿这一条，其余轴压到不进注入的档位。
        soma._set("sleep_pressure", 8.0)
        soma._set("sleep_debt", 0.0)
        soma._set("discomfort", 0.0)
        soma._set("chill", 0.0)
        soma._set("arousal", 60.0)

    def _hunger_score(self, value: float) -> float:
        soma = _fixture().soma
        self._quiet(soma)
        soma._set("hunger", value)
        scored = {text: score for score, text in soma.feelings(80.0)}
        hits = [
            score
            for text, score in scored.items()
            if text in ("很饿", "饿得发慌", "有些饿", "有点饿", "肚子有点空")
        ]
        self.assertEqual(len(hits), 1, f"饥饿只该出一条体感：{scored}")
        return hits[0]

    def test_top_band_takes_the_whole_top(self):
        """hunger=88 是顶档（「很饿」），分数必须是满的 0.8——旧公式只有 0.733，会被 0.75 门槛滤掉。"""
        self.assertAlmostEqual(self._hunger_score(88.0), 0.8, places=6)

    def test_middle_band_scores_below_the_top(self):
        """hunger=58 落在「有点饿」那一档：分数低于顶档，但不会低到被门控吃掉。"""
        middle = self._hunger_score(58.0)
        self.assertLess(middle, 0.8)
        self.assertGreaterEqual(middle, 0.8 * 0.34 - 1e-9)

    def test_reverse_axis_low_value_takes_the_whole_top(self):
        """没精神（arousal 很低）时「越难受越该说」：分数满档，且越过 prompt 侧 0.7 的门槛。

        旧公式在这里是 10/45=0.22，永远进不了上下文——这正是删掉 span 分制的原因。
        """
        soma = _fixture().soma
        self._quiet(soma)
        soma._set("hunger", 10.0)
        soma._set("energy", 20.0)
        soma._set("arousal", 10.0)
        scored = {text: score for score, text in soma.feelings(20.0)}
        hit = [
            score
            for text, score in scored.items()
            if text in ("脑子转不动", "整个人有点发沉", "精力不太够")
        ]
        self.assertEqual(len(hit), 1, f"低唤醒只该出一条：{scored}")
        self.assertAlmostEqual(hit[0], 0.8, places=6)
        self.assertGreater(hit[0], 0.7, "顶档都过不了 prompt 侧的 0.7 门槛，这一支就白写了")


if __name__ == "__main__":
    unittest.main()
