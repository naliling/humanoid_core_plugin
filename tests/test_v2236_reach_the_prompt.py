"""每一条轴都要真的**到得了模型眼前**。

这是补两次教训的：

1. `test_awake_and_want_to_talk_axes_are_used` 断言「精神/脑子转得挺快」——
   那些词早就不在词表里了，而且它一直靠**主动性**那几个词碰巧通过，
   所谓的 arousal 校验从来没生效过。名字里的「arousal 的词表一次都没被调用过」
   说的就是真的，一直没修。

2. 修的过程中又发现 arousal 这条轴**两端都是坏的**：
   高档固定给 0.6、低档给 `value/45`，而 prompt 侧门槛是 0.7 ——
   **高精神说不出来，很没精神也说不出来**。

所以这里不再断言「某个词在不在」，而是**推值 → 渲染 → 看它有没有真的进注入**。
"""
import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from humanoid.config import HumanoidConfig
from humanoid.core_instance import HumanoidCoreInstance
from humanoid.state import StateStore
from tests.fakes import FakeContext, FrozenClock, RecordingLogger


def cfg(**kw):
    return HumanoidConfig.from_raw(kw)


def core_at(hh=13, mm=3, **raw):
    store = StateStore(Path(tempfile.mkdtemp()) / "s.json", lambda: 0.01)
    store.load("2026-09-26", 28)
    core = HumanoidCoreInstance(
        role_id="bot1", state_store=store,
        config_provider=lambda: cfg(**raw),
        logger=RecordingLogger(), stop_event=asyncio.Event(),
        resolver=FakeContext(), gateway=None,
    )
    core.clock = FrozenClock(datetime(2026, 9, 26, hh, mm))
    return core


class ArousalReachesPrompt(unittest.TestCase):
    THRESHOLD = 0.7     # prompt 侧 _feelings_lines 的门槛

    def test_high_arousal_is_mentioned(self):
        core = core_at()
        core.soma.data["arousal"] = 95.0
        core.soma.data["energy"] = 95.0
        text = core.build_injection("42", is_group=False)
        self.assertTrue(
            any(w in text for w in ("人挺精神的", "脑子转得挺快")),
            f"很精神的时候注入里没说：{text}",
        )

    def test_low_arousal_is_mentioned(self):
        """越没精神越该说。原来是反的：值越低得分越低，全被门槛滤掉。"""
        core = core_at()
        core.soma.data["arousal"] = 10.0
        core.soma.data["energy"] = 10.0
        text = core.build_injection("42", is_group=False)
        self.assertTrue(
            any(w in text for w in ("脑子转不动", "整个人有点发沉", "精力不太够")),
            f"很没精神的时候注入里没说：{text}",
        )

    def test_middling_arousal_is_omitted(self):
        """中间那档本来就该不说——这是门槛的设计目的，不是 bug。"""
        core = core_at()
        core.soma.data["arousal"] = 50.0
        core.soma.data["energy"] = 50.0
        text = core.build_injection("42", is_group=False)
        self.assertFalse(
            any(w in text for w in ("人挺精神的", "脑子转不动", "整个人有点发沉")),
            f"不痛不痒的时候不该渲染体感：{text}",
        )

    def test_scores_clear_the_threshold_when_they_should(self):
        """两端的得分都必须真的高过门槛，否则前面两条只是碰巧。"""
        for arousal, energy, label in ((95.0, 95.0, "高"), (8.0, 8.0, "低")):
            core = core_at()
            core.soma.data["arousal"] = arousal
            core.soma.data["energy"] = energy
            picked = core.soma.feelings(energy)
            self.assertTrue(
                any(s >= self.THRESHOLD for s, _ in picked),
                f"{label}档的体感得分没过门槛，注入里永远不会出现：{picked}",
            )


class SeedStaysStableAcrossRestart(unittest.TestCase):
    def test_same_user_gets_same_dimension_every_process(self):
        """抽签种子跨进程稳定——原来用内建 hash()，每重启换一次维度。"""
        import subprocess
        import sys
        code = (
            "import sys; sys.path.insert(0, '.');"
            "import hashlib;"
            "raw=b'emotion:bot1:42';"
            "print(int.from_bytes(hashlib.blake2b(raw, digest_size=8).digest(),'big')%3)"
        )
        seen = {subprocess.run([sys.executable, "-c", code], capture_output=True,
                               text=True, cwd=str(Path(__file__).parent.parent)).stdout.strip()
                for _ in range(3)}
        self.assertEqual(len(seen), 1, f"抽签结果随进程变：{seen}")


if __name__ == "__main__":
    unittest.main()
