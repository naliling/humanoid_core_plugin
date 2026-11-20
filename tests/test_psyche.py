"""叙事心理层（psyche）单测：动力学规则 + 持久化 + 生成节奏。"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from humanoid.psyche import (
    Concern, Entry, NEEDS, PsycheState, PsycheStore, PsycheWriter, ViewOfPeer,
    _parse_psyche_json, worth_recording,
)


class _FakeResult:
    def __init__(self, text):
        self.ok = True
        self.text = text


class _FakeGateway:
    def __init__(self, text):
        self.text = text
        self.calls = []

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResult(self.text)


class PsycheWriterTest(unittest.TestCase):
    """生成 → 落盘 → 注入：端到端跑一遍这条链。"""

    def test_generated_text_becomes_injection(self):
        d = Path(tempfile.mkdtemp())
        store = PsycheStore(d, "bot1")
        raw = json.dumps({
            "entry": "他刚才那句随便，我有点在意",
            "concern": {"key": "casual", "text": "他那句随便到底什么意思",
                        "pull_toward": "他可能只是随口", "pull_away": "又怕我没接住",
                        "triggers": ["随便"], "significance": 0.8},
            "needs": {"understanding": 0.3},
            "view": {"traits": ["说话短"], "misread": "我以为他不在意"},
        }, ensure_ascii=False)
        gw = _FakeGateway("```json\n" + raw + "\n```")
        writer = PsycheWriter(store, gw, lambda: None, time_source=lambda: 1_000_000.0)
        wrote = asyncio.run(writer.write_once(
            "u1", user_text="随便吧", her_reply="我有点在意你说的随便"))
        self.assertTrue(wrote)
        self.assertEqual(len(gw.calls), 1)
        st = store.state("u1")
        self.assertIn("casual", st.concerns)
        # delta 0.3 被钳到 0.2（防单次跳变）：0.25 + 0.2 = 0.45
        self.assertAlmostEqual(st.needs["understanding"], 0.45)
        self.assertEqual(st.view.misread, "我以为他不在意")
        self.assertTrue(any(e.text == "他刚才那句随便，我有点在意" for e in store.journal))
        # 生成后 10 小时、夜里、清醒：心事该浮上来成为注入
        lines = st.injection_lines(local_hour=23, text="", body_modulation=1.0,
                                   now=1_000_000.0 + 10 * 3600)
        self.assertTrue(any("随便" in x for x in lines))

    def test_throttle_and_no_content_skip_call(self):
        d = Path(tempfile.mkdtemp())
        store = PsycheStore(d, "bot1")
        gw = _FakeGateway('{"entry":"x"}')
        writer = PsycheWriter(store, gw, lambda: None, time_source=lambda: 1_000_000.0)
        # 没值得记的内容：不调模型
        self.assertFalse(asyncio.run(writer.write_once(
            "u1", user_text="嗯", her_reply="好")))
        self.assertEqual(len(gw.calls), 0)


class ParseJsonTest(unittest.TestCase):
    def test_plain_and_fenced(self):
        self.assertEqual(_parse_psyche_json('{"entry": "x"}')["entry"], "x")
        self.assertEqual(_parse_psyche_json('```json\n{"entry":"y"}\n```')["entry"], "y")
        self.assertEqual(_parse_psyche_json('这里：{"entry":"z"} 以上是')["entry"], "z")
        self.assertIsNone(_parse_psyche_json("不是 json"))
        self.assertIsNone(_parse_psyche_json(""))


class PsycheLogicTest(unittest.TestCase):
    def test_needs_satisfy_and_frustrate(self):
        st = PsycheState("u1")
        self.assertAlmostEqual(st.needs["response"], 0.25)
        st.frustrate_need("response", 0.5)
        self.assertAlmostEqual(st.needs["response"], 0.75)
        st.satisfy_need("response", 0.3)
        self.assertAlmostEqual(st.needs["response"], 0.45)
        # 钳位
        st.frustrate_need("space", 5.0)
        self.assertAlmostEqual(st.needs["space"], 1.0)
        st.satisfy_need("space", 5.0)
        self.assertAlmostEqual(st.needs["space"], 0.0)

    def test_decay_recovers_needs_and_fades_concerns(self):
        now = 1_000_000.0
        st = PsycheState("u1")
        st.last_interaction_at = now
        st.frustrate_need("response", 0.7)
        st.add_concern(Concern(key="k", text="他上次那句话", ts=now, significance=0.9))
        st.decay(now + 3 * 86400)  # 三天后
        self.assertLess(st.needs["response"], 0.95)
        self.assertLess(st.concerns["k"].significance, 0.9)

    def test_rumination_night_boost_and_topic_trigger(self):
        now = 1_000_000.0
        st = PsycheState("u1")
        st.add_concern(Concern(key="k", text="他那句随便", ts=now - 10 * 3600,
                               significance=0.5, triggers=["随便"]))
        # 白天、无话题：0.5×成熟(约0.81)=0.4 < 0.55 → 不浮上来（安静是常态）
        day = st.ruminating(local_hour=13, text="今天天气不错", now=now)
        self.assertEqual(day, [])
        # 夜里加成 0.18 → 0.58 ≥ 阈值 → 浮上来
        night = st.ruminating(local_hour=23, text="", now=now)
        self.assertEqual([c.key for c in night], ["k"])
        # 话题戳中 +0.25 也能顶上来
        poked = st.ruminating(local_hour=13, text="你刚才说随便", now=now)
        self.assertEqual([c.key for c in poked], ["k"])
        # 刚反刍过立刻再问：被压住
        st.note_echo("k", now=now)
        suppressed = st.ruminating(local_hour=23, text="", now=now + 60)
        self.assertEqual(suppressed, [])

    def test_rumination_respects_body_modulation(self):
        now = 1_000_000.0
        st = PsycheState("u1")
        st.add_concern(Concern(key="k", text="想不通", ts=now - 10 * 3600,
                               significance=0.5))
        self.assertEqual(st.ruminating(local_hour=13, now=now, body_modulation=0.0), [])
        # 清醒(+1.0)×0.10=+0.10 → 0.5×0.81+0.10=0.5 < 0.55，还不够；
        # 再加夜间 0.18 → 0.69 ≥ 阈值才浮上来（困的时候确实不想纠结）
        self.assertEqual(st.ruminating(local_hour=13, now=now,
                                       body_modulation=1.0), [])
        self.assertEqual(len(st.ruminating(local_hour=23, now=now,
                                           body_modulation=1.0)), 1)

    def test_contradiction_coexists(self):
        now = 1_000_000.0
        st = PsycheState("u1")
        st.add_concern(Concern(key="k", text="想靠近又怕打扰", ts=now - 10 * 3600,
                               significance=0.7,
                               pull_toward="他其实对我挺好", pull_away="上次被冷落"))
        lines = st.ruminating(local_hour=2, now=now, body_modulation=1.0)
        self.assertTrue(lines)
        inject = st.injection_lines(local_hour=2, now=now, body_modulation=1.0)
        self.assertIn("想靠近又怕打扰", inject)
        self.assertTrue(any("又想靠近" in x for x in inject))

    def test_view_update_and_misread(self):
        st = PsycheState("u1")
        st.update_view(traits=["说话急"], expectation="他会在意", misread="我以为他在生气",
                       evidence=2)
        self.assertEqual(st.view.traits, ["说话急"])
        self.assertEqual(st.view.evidence_count, 2)
        # 新证据修正：重复 trait 不叠加，misread 被改写
        st.update_view(traits=["说话急", "心细"], misread="其实他只是累了", evidence=1)
        self.assertEqual(st.view.traits, ["说话急", "心细"])
        self.assertEqual(st.view.misread, "其实他只是累了")
        self.assertEqual(st.view.evidence_count, 3)

    def test_reframe_rewrites_old_interpretation(self):
        st = PsycheState("u1")
        st.journal.append(Entry(text="他说会来，我信了", concern_key="k",
                                then="他说会来，我信了"))
        st.add_concern(Concern(key="k", text="他说会来"))
        st.reframe("k", "他后来真的来了，说话算数")
        self.assertEqual(st.journal[0].then, "他说会来，我信了")
        self.assertEqual(st.journal[0].now_view, "他后来真的来了，说话算数")
        self.assertEqual(st.concerns["k"].text, "他后来真的来了，说话算数")

    def test_urge_to_speak_needs_pressure_and_uses_own_words(self):
        """v3.1.47：压强不够憋着不说；够了也只取她自己生成过的原句。"""
        now = 1_000_000.0
        st = PsycheState("u1")
        st.add_concern(Concern(key="k", text="他那句随便到底什么意思",
                               ts=now - 20 * 3600, significance=0.9))
        # 只有心事、需求都低：压强 = 0.55*0.25 + 0.35*0.5 ≈ 0.32 < 0.55 → 不说
        self.assertEqual(st.urge_to_speak(local_hour=23, body_modulation=1.0, now=now), "")
        # 需求欠到顶 + 心事发酵：0.55*1 + 0.35*0.5 ≈ 0.72 ≥ 0.55 → 憋不住
        st.frustrate_need("response", 1.0)
        urge = st.urge_to_speak(local_hour=23, body_modulation=1.0, now=now)
        self.assertEqual(urge, "他那句随便到底什么意思")
        self.assertNotIn("〔", urge)  # 抬头由 core 加，psyche 只给原句

    def test_proactive_pressure_range(self):
        st = PsycheState("u1")
        self.assertGreaterEqual(st.proactive_pressure(), 0.0)
        self.assertLessEqual(st.proactive_pressure(), 1.0)
        st.frustrate_need("response", 0.7)
        st.add_concern(Concern(key="k", text="惦记", ts=1_000_000.0 - 10 * 3600))
        high = st.proactive_pressure(now=1_000_000.0)
        self.assertGreater(high, 0.5)

    def test_worth_recording(self):
        self.assertFalse(worth_recording("嗯", "好"))
        self.assertTrue(worth_recording("其实我最近有点累", "怎么了"))
        self.assertTrue(worth_recording("随便", "我觉得你不太对劲"))


class PsycheStoreTest(unittest.TestCase):
    def test_roundtrip_state_and_journal(self):
        d = Path(tempfile.mkdtemp())
        store = PsycheStore(d, "bot1")
        st = store.state("u1")
        st.frustrate_need("understanding", 0.6)
        st.add_concern(Concern(key="k", text="那件事", triggers=["那件事"]))
        st.update_view(traits=["急"], misread="看错了")
        self.assertTrue(store.save_state())
        self.assertTrue(store.append_entry(Entry(text="我记下了", concern_key="k")))

        reopened = PsycheStore(d, "bot1")
        self.assertAlmostEqual(reopened.state("u1").needs["understanding"], 0.85)
        self.assertIn("k", reopened.state("u1").concerns)
        self.assertEqual(reopened.state("u1").view.misread, "看错了")
        self.assertEqual(len(reopened.journal), 1)
        self.assertEqual(reopened.journal[0].text, "我记下了")

    def test_corrupt_files_do_not_crash(self):
        d = Path(tempfile.mkdtemp())
        (d / "psyche_state.json").write_text("{broken", encoding="utf-8")
        (d / "psyche_journal.jsonl").write_text("not json\n\n{garbage}\n", encoding="utf-8")
        store = PsycheStore(d, "bot1")
        self.assertEqual(store.states, {})
        self.assertEqual(store.journal, [])

    def test_write_throttle(self):
        now = 1_000_000.0
        d = Path(tempfile.mkdtemp())
        store = PsycheStore(d, "bot1")
        self.assertTrue(store.should_write("u1", now=now))
        store.state("u1").last_entry_at = now
        self.assertFalse(store.should_write("u1", now=now + 60))
        self.assertTrue(store.should_write("u1", now=now + 200))


if __name__ == "__main__":
    unittest.main()
