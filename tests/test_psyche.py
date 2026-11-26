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
        user_text = "我平时说话很直接，随便吧"
        raw = json.dumps({
            "entry": "他刚才那句随便，我有点在意",
            "concern": {"key": "casual", "text": "他那句随便到底什么意思",
                        "pull_toward": "他可能只是随口", "pull_away": "又怕我没接住",
                        "triggers": ["随便"], "significance": 0.8},
            "needs": {"understanding": 0.3},
            "view": {
                "traits": [
                    {"text": "我平时说话很直接", "evidence": "我平时说话很直接"},
                    {"text": "说话短", "evidence": "随便吧"},
                ],
                "expectation": {"text": "他会在意", "evidence": "随便吧"},
                "misread": {"text": "我以为他不在意", "evidence": "随便吧"},
            },
        }, ensure_ascii=False)
        gw = _FakeGateway("```json\n" + raw + "\n```")
        writer = PsycheWriter(store, gw, lambda: None, time_source=lambda: 1_000_000.0)
        wrote = asyncio.run(writer.write_once(
            "u1", user_text=user_text, her_reply="我有点在意你说的随便"))
        self.assertTrue(wrote)
        self.assertEqual(len(gw.calls), 1)
        st = store.state("u1")
        self.assertIn("casual", st.concerns)
        # delta 0.3 被钳到 0.2（防单次跳变）：0.25 + 0.2 = 0.45
        self.assertAlmostEqual(st.needs["understanding"], 0.45)
        self.assertEqual(st.view.traits, ["我平时说话很直接"])
        self.assertEqual(st.view.trait_evidence["我平时说话很直接"], "我平时说话很直接")
        self.assertEqual(st.view.expectation_source, "user_text")
        self.assertEqual(st.view.expectation_evidence, "随便吧")
        self.assertEqual(st.view.misread, "我以为他不在意")
        self.assertEqual(st.view.misread_evidence, "随便吧")
        entry = next(e for e in store.journal if e.text == "他刚才那句随便，我有点在意")
        self.assertEqual(entry.source_kind, "model_internal")
        self.assertEqual(entry.source_text, user_text)
        # 生成后 10 小时、夜里、清醒：心事该浮上来成为注入
        lines = st.injection_lines(local_hour=23, text="", body_modulation=1.0,
                                   now=1_000_000.0 + 10 * 3600)
        self.assertEqual(lines, [st.concerns["casual"].text], "注入只能回传已存心事原文")
        self.assertNotIn(st.view.misread, lines)

    def test_followup_writer_prompt_labels_view_hypotheses(self):
        store = PsycheStore(Path(tempfile.mkdtemp()), "bot1")
        st = store.state("u1")
        source = "我喜欢慢慢熟，他说需要一点空间"
        st.update_view(
            traits=[{"text": "我喜欢慢慢熟", "evidence": "我喜欢慢慢熟"}],
            expectation={"text": "他可能需要空间", "evidence": "他说需要一点空间"},
            misread={"text": "我是不是误会了", "evidence": "他说需要一点空间"},
            source_text=source,
        )
        gw = _FakeGateway('{"entry":"我还在想这件事"}')
        writer = PsycheWriter(store, gw, lambda: None, time_source=lambda: 1_000_000.0)
        self.assertTrue(asyncio.run(writer.write_once(
            "u1", user_text="我今天发现，先处理手头的事让我安心一点", her_reply="我听见了")))
        prompt = gw.calls[0]["prompt"]
        self.assertIn("证据来自较早的用户原文，并非本轮内容", prompt)
        self.assertNotIn("来源为本轮用户原文", prompt)
        self.assertIn("他说需要一点空间", prompt)
        self.assertIn("我今天发现，先处理手头的事让我安心一点", prompt)
        self.assertIn("对方明确原文（逐字片段，非归纳）", prompt)

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
        self.assertEqual(inject, ["想靠近又怕打扰"], "只投递已存心事，不合成新的心理台词")

    def test_view_update_requires_verbatim_source_evidence(self):
        st = PsycheState("u1")
        source = "我平时说话比较急，他会在意我；他刚才语气很短，其实他只是累了"
        st.update_view(
            traits=[{"text": "我平时说话比较急", "evidence": "我平时说话比较急"}],
            expectation={"text": "他会在意我", "evidence": "他会在意我"},
            misread={"text": "我以为他在生气", "evidence": "他刚才语气很短"},
            source_text=source,
        )
        self.assertEqual(st.view.traits, ["我平时说话比较急"])
        self.assertEqual(st.view.trait_evidence["我平时说话比较急"], "我平时说话比较急")
        self.assertEqual(st.view.expectation_source, "user_text")
        self.assertEqual(st.view.expectation_evidence, "他会在意我")
        self.assertEqual(st.view.misread_evidence, "他刚才语气很短")
        self.assertEqual(st.view.evidence_count, 3)

        st.update_view(
            traits=[
                {"text": "我平时说话比较急", "evidence": "我平时说话比较急"},
                {"text": "我其实很心细", "evidence": "我其实很心细"},
            ],
            misread={"text": "我可能误会他了", "evidence": "其实他只是累了"},
            source_text="我其实很心细；其实他只是累了",
        )
        self.assertEqual(st.view.traits, ["我平时说话比较急", "我其实很心细"])
        self.assertEqual(st.view.misread, "我可能误会他了")
        self.assertEqual(st.view.expectation, "他会在意我")
        self.assertEqual(st.view.evidence_count, 4)

        st.update_view(expectation=None, misread=None, source_text=source,
                       clear_expectation=True, clear_misread=True)
        self.assertEqual(st.view.expectation, "")
        self.assertEqual(st.view.expectation_source, "")
        self.assertEqual(st.view.expectation_evidence, "")
        self.assertEqual(st.view.misread, "")
        self.assertEqual(st.view.misread_source, "")
        self.assertEqual(st.view.misread_evidence, "")
        self.assertEqual(st.view.evidence_count, 2)

    def test_unsupported_view_inferences_and_legacy_fields_are_dropped(self):
        st = PsycheState("u1")
        changed = st.update_view(
            traits=[
                {"text": "外向", "evidence": "我今天很开心"},
                {"text": "外向", "evidence": "我不是很外向"},
            ],
            expectation={"text": "他会在意", "evidence": "不存在的引文"},
            misread="我以为他在生气",
            source_text="我今天很开心；我不是很外向",
        )
        self.assertFalse(changed)
        self.assertEqual(st.view.traits, [])
        self.assertEqual(st.view.expectation, "")
        self.assertEqual(st.view.misread, "")

        legacy = ViewOfPeer.from_dict({
            "traits": ["外向"], "expectation": "他会在意",
            "misread": "我以为他生气", "evidence_count": 9,
        })
        self.assertEqual(legacy.traits, [])
        self.assertEqual(legacy.expectation, "")
        self.assertEqual(legacy.misread, "")
        self.assertEqual(legacy.evidence_count, 0)

        old_entry = Entry.from_dict({"text": "我记下了"})
        self.assertEqual(old_entry.source_kind, "legacy_unattributed")
        self.assertEqual(old_entry.source_text, "")
        forged_entry = Entry.from_dict({"text": "来源不明", "source_kind": "user_explicit"})
        self.assertEqual(forged_entry.source_kind, "legacy_unattributed")

    def test_reply_hints_do_not_project_peer_view_hypotheses(self):
        st = PsycheState("u1")
        source = "我平时喜欢慢慢熟，他刚才语气很短"
        st.update_view(
            traits=[{"text": "我平时喜欢慢慢熟", "evidence": "我平时喜欢慢慢熟"}],
            expectation={"text": "他会在意", "evidence": "他刚才语气很短"},
            misread={"text": "我以为他生气了", "evidence": "他刚才语气很短"},
            source_text=source,
        )
        st.frustrate_need("response", 1.0)
        lines = st.injection_lines(local_hour=23, body_modulation=1.0, now=1_000_000.0)
        self.assertNotIn("我平时喜欢慢慢熟", lines)
        self.assertNotIn(st.view.expectation, lines)
        self.assertNotIn(st.view.misread, lines)

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
        st.update_view(
            traits=[{"text": "我有点急", "evidence": "我有点急"}],
            misread={"text": "我可能看错了", "evidence": "他今天说话很短"},
            source_text="我有点急；他今天说话很短",
        )
        self.assertTrue(store.save_state())
        self.assertTrue(store.append_entry(Entry(
            text="我记下了", concern_key="k", source_kind="model_internal",
            source_text="那件事",
        )))

        reopened = PsycheStore(d, "bot1")
        self.assertAlmostEqual(reopened.state("u1").needs["understanding"], 0.85)
        self.assertIn("k", reopened.state("u1").concerns)
        self.assertEqual(reopened.state("u1").view.misread, "我可能看错了")
        self.assertEqual(len(reopened.journal), 1)
        self.assertEqual(reopened.journal[0].text, "我记下了")
        self.assertEqual(reopened.journal[0].source_kind, "model_internal")
        self.assertEqual(reopened.journal[0].source_text, "那件事")

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
