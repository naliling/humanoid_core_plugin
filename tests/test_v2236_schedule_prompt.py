"""v2.23.6：日程 prompt 的松绑、举例、时长与切换衔接。

起因是一组「她的日程很差」的反馈。逐条查下来根因都在 prompt 里，不是模型笨：

- 「一天下来每天都差不多」——`persona_block` 结尾写着「设定里没提到的，留白即可」。
  那不是防造身份，是命令模型不发挥，于是每段都挑最安全那件事。
- 「小马会拿手指拿手机」——同一条。人设里写了物种，模型还是退回通用模板。
- 「一生成就包括好长时间」——「怎么决定」只教了「什么时候该换」，
  没有教「一段能有多短」，于是 minutes 天然偏长。
- 「看着很随便」——唯一的示例是占位符 `<她在做的事>`，画面感是当初特意删掉的。
  矫枉过正：模型手里只剩人设 + 一堆规则 + 一个空模板。
"""
import unittest

from humanoid.config import HumanoidConfig
from humanoid.persona import Persona
from humanoid.services.schedule import segment_prompt, Slot


def cfg(**kw) -> HumanoidConfig:
    return HumanoidConfig.from_raw(kw)


PERSONA = "你是小马利亚，纯白双翼的独角小马，坐着要收翅膀。\n你今年18岁，很有主见。\n你喜欢夜里出去溜达。"


def build(**kw) -> str:
    return segment_prompt(
        cfg(), now_text="16:10", weekday="二", persona=Persona("小马利亚", PERSONA, "会话"),
        body={"now": "16:10", "energy": 70.0, "hunger": 60.0, "asleep": False},
        prev=Slot({"start": "15:40", "end": "16:10", "event": "整理个案笔记", "location": "书桌前"}),
        elapsed_minutes=30, **kw)


class SchedulePromptLoosened(unittest.TestCase):
    def test_no_longer_tells_the_model_to_leave_blanks(self):
        """「留白即可」是日程雷同的根因，必须撤掉。"""
        p = build()
        self.assertNotIn("留白", p, "还在命令模型留白——那会让每一天都一样")
        self.assertNotIn("不要替他编", p)

    def test_still_forbids_inventing_a_second_identity(self):
        """撤掉的是「留白」，不是「别造身份」——那条得留。"""
        p = build()
        self.assertIn("不要给她硬造第二份身份", p)

    def test_tells_it_to_follow_what_the_persona_says(self):
        """人设里写了物种/外貌就必须照着，这是小马长手指的直接解法。"""
        p = build()
        self.assertIn("必须照着来", p)
        self.assertIn("别写成通用的", p)

    def test_tells_it_not_to_pick_the_same_easy_thing_every_day(self):
        self.assertIn("别每天都挑同一个最省事的选择", build())

    def test_persona_budget_actually_grew(self):
        """原来写死 1200，写细的人设会被砍掉尾巴。"""
        self.assertGreaterEqual(cfg().schedule_persona_max_chars, 2000)
        long_persona = "\n".join(f"第{i}行设定" for i in range(400))
        p = segment_prompt(
            cfg(), now_text="16:10", weekday="二",
            persona=Persona("长设定", long_persona, "会话"))
        # 第 300 行以后应该还在（1200 字符大概只能到 90 行）
        self.assertIn("第300行设定", p, "人设的尾部被砍掉了")


class SchedulePromptExamples(unittest.TestCase):
    def test_examples_are_concrete_and_varied(self):
        """占位符换回有画面的举例，而且长度不一样。"""
        p = build()
        self.assertNotIn('"<她在做的事>"', p, "示例还是空占位符")
        self.assertIn("举例", p)
        self.assertIn("不是让你照抄", p)

    def test_examples_cover_both_short_and_long(self):
        """22 分钟和 145 分钟并列，本身就是在示范「长短都正常」。"""
        p = build()
        self.assertIn("22", p)
        self.assertIn("145", p)

    def test_examples_cover_continue(self):
        self.assertIn('{"continue": true}', p := build()) or self.assertIn("continue", p)


class ScheduleDurationGuidance(unittest.TestCase):
    def test_says_minutes_reflects_the_actual_task(self):
        p = build()
        self.assertIn("minutes 要真的反映这件事要多久", p)
        self.assertIn("别因为「反正要填一段时间」就往长的填", p)

    def test_says_bounds_are_not_a_target(self):
        self.assertIn("上下限，不是目标值", build())

    def test_gives_short_segments_permission(self):
        self.assertIn("15~30 分钟", build())


class ScheduleTransitionContinuity(unittest.TestCase):
    """粒度变细之后，段与段之间接不接得上比时长更要紧。"""

    def test_asks_for_plausible_switches(self):
        p = build()
        self.assertIn("换一件事要接得上", p)
        self.assertIn("别一段之内从一个地方跳到另一个", p)

    def test_tells_it_staying_put_means_continue(self):
        self.assertIn("continue=true 就好", build())


class ScheduleNowToggle(unittest.TestCase):
    """`inject_schedule_now` 已删除：日程全面不进对话，不再需要开关。"""

    def test_config_item_is_gone(self):
        from humanoid.config import DEFAULTS

        self.assertFalse(
            hasattr(DEFAULTS, "inject_schedule_now"),
            "开关又回来了：日程该是全面不进对话，而不是可选项",
        )

    def test_schedule_text_never_reaches_injection(self):
        """日程事件名不许出现在注入里（三档一起验）。"""
        import asyncio
        import tempfile
        from pathlib import Path

        from humanoid.core_instance import HumanoidCoreInstance
        from humanoid.state import StateStore
        from tests.fakes import FakeContext, FrozenClock, RecordingLogger

        from datetime import datetime

        moment = datetime(2026, 9, 26, 15, 20)
        for mode in ("medium", "full", "mood_only"):
            with self.subTest(mode=mode):
                store = StateStore(Path(tempfile.mkdtemp()) / "s.json", lambda: 0.01)
                store.load("2026-09-26", 28)
                conf = cfg(inject_activity_context=mode)
                core = HumanoidCoreInstance(
                    role_id="bot1", state_store=store, config_provider=lambda: conf,
                    logger=RecordingLogger(), stop_event=asyncio.Event(),
                    resolver=FakeContext(), gateway=None,
                )
                frozen = FrozenClock(moment)
                core.clock = frozen
                for name in ("schedule", "soma", "energy", "social", "process", "mood"):
                    svc = getattr(core, name, None)
                    if svc is not None and hasattr(svc, "_clock"):
                        svc._clock = frozen
                core.scope.update_self(
                    today_date="2026-09-26",
                    daily_schedule=[{
                        "start": "13:30", "end": "17:30", "event": "跟客户过方案",
                        "location": "会议室", "emotion": "紧绷", "energy_rate": -0.1,
                    }],
                )
                text = core.build_injection("42", is_group=False)
                self.assertNotIn("跟客户过方案", text, f"{mode} 档漏出日程：\n{text}")
                self.assertNotIn("她今天", text, f"{mode} 档漏出日程：\n{text}")


if __name__ == "__main__":
    unittest.main()
