"""叙事心理层（psyche）：让角色有「会积累、会惦记、会长」的心理日子。

## 这一层补的是什么

身体层（soma / energy / mood 的数值）管的是**生理的日子**：精力涨落、睡眠债、
好感度加减。那不是「心理」——数值每天衰减回基线，不留下任何「经历过什么」的痕迹。
psyche 管的是**心理的日子**：她惦记什么、她怎么看对方、她没解开的小心思，跨轮次、
跨会话地活着。

## 七条动力学（都是确定性规则，不调模型）

1. **需求驱动**：被回应 / 被理解 / 自己的空间 / 新鲜感，四个需求被互动满足或挫败，
   欠着就调制语气与主动权。不是「心情分数」，是「她现在缺什么」。
2. **反刍发酵**：未完成的心事（concern）带显著性，随时间发酵；夜里、闲下来、被相似
   话题戳到时激活。安静是常态，总念叨才假。
3. **心智理论**：`view_of_peer` 是「我眼中的你」，从行为里推出来，会更新、会推错，
   新证据能修正。她会「看错人」，这份误判本身就是戏。
4. **矛盾共存**：concern 允许同时存在互相拉扯的两条（亲近又戒备），不压成单一分数。
5. **记忆重构**：旧事被新经历重新解释——同一段过去在不同心境下味道不同。journal
   条目带「当时怎么想 / 现在怎么看」两层，新证据可改写旧解释。
6. **身体缝合**：注入的心理句由身体状态调制（困了更不想纠结、睡醒后别扭淡了）。
   psyche 不读 body 数值本身，调用方把调制后的 `body_modulation` 传进来。
7. **阈值触发主动**：需求欠太久 / 心事发酵够了 / 误判积累 → `proactive_pressure()`
   给出压强，交给自主社交层决定何时开口、说什么。

## 内容来源：生成，不是内置

心理文字**全部由模型生成**（每轮低频、事件驱动），psyche 只存、只算时序、只做筛选。
内置模板只在两个地方兜底：诊断面板的「无内容」说明，以及生成失败时不注入（宁缺勿编）。
绝不把写死的句子当她的想法注入——那就是新的念稿源。

## 持久化

落两份文件（各角色独立，跟 state.json 并列）：
* `psyche_journal.jsonl`：自述本 + 事件账本，一行一条，append-only。
* `psyche_state.json`：需求余额、心事、心智模型、阈值计时等结构化状态。

原子写 + 坏值兜底，沿用 state.json 的纪律：读坏了一个文件不能让这一轮注入挂掉。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# 心理层自己的常量——不进 config（保持增强的最小面），想调就是改这里。
JOURNAL_FILE = "psyche_journal.jsonl"
STATE_FILE = "psyche_state.json"

#: 注入块抬头。出站照抄闸靠它识别「这是她自己写的心理话」，不把它当念稿删掉。
INJECTION_MARK = "〔我和他之间〕"
#: v3.1.47：她自己想开口的那句（与反刍注入分开的抬头，照抄闸同样放行）。
URGE_MARK = "〔憋着想跟你说〕"

# 四个需求。欠得越多（deficit 越大），越想被满足。
NEEDS: Tuple[str, ...] = ("response", "understanding", "space", "novelty")
NEED_LABELS: Dict[str, str] = {
    "response": "被回应",
    "understanding": "被理解",
    "space": "自己的空间",
    "novelty": "新鲜感",
}

# 反刍激活阈值：显著性 × 时间衰减 超过这个数才浮上来。
RUMINATION_THRESHOLD = 0.55
# 夜里（调用方本地小时 22-5）反刍加成。
NIGHT_BOOST = 0.18
# 每轮自然衰减：心事显著性每天乘这个数（没被提起就慢慢淡）。
SIGNIFICANCE_DAILY_DECAY = 0.88
# 需求 deficit 每天自然回落（被满足后不会一直惦记）。
NEED_DAILY_RECOVERY = 0.10

# journal 最多留多少条（超出裁掉最旧的）。
JOURNAL_MAX = 400
# 一次注入最多带几条心理句。
INJECT_MAX_LINES = 2


def _now() -> float:
    return time.time()


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class Entry:
    """模型写下的内心记录，不是用户原话或已核实事实。"""

    text: str                      # 第一人称原文（模型生成）
    role_id: str = ""             # bot account owner
    user_id: str = ""             # peer owner
    kind: str = "self"             # self / event / reframe（重构）
    ts: float = field(default_factory=_now)
    # 关联的心事 key（这条属于哪件惦记的事）
    concern_key: str = ""
    # 当时怎么想（原始），现在怎么看（重构后可改）——记忆重构的两层
    then: str = ""
    now_view: str = ""
    # 显著性：越高越容易被反刍到
    significance: float = 0.5
    # 这条记录当时她的身体调制标记（生成时带上，如 "困" "刚醒"）
    body_tag: str = ""
    source_kind: str = "legacy_unattributed"
    source_text: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(raw: Any) -> Optional["Entry"]:
        if not isinstance(raw, dict):
            return None
        text = str(raw.get("text") or "").strip()
        if not text:
            return None
        source_kind = str(raw.get("source_kind") or "legacy_unattributed")
        if source_kind not in ("model_internal", "legacy_unattributed"):
            source_kind = "legacy_unattributed"
        return Entry(
            text=text,
            role_id=str(raw.get("role_id") or "").strip(),
            user_id=str(raw.get("user_id") or "").strip(),
            kind=str(raw.get("kind") or "self"),
            ts=_safe_float(raw.get("ts"), _now()),
            concern_key=str(raw.get("concern_key") or ""),
            then=str(raw.get("then") or raw.get("text") or ""),
            now_view=str(raw.get("now_view") or ""),
            significance=_clamp(_safe_float(raw.get("significance"), 0.5), 0.0, 1.0),
            body_tag=str(raw.get("body_tag") or ""),
            source_kind=source_kind,
            source_text=str(raw.get("source_text") or ""),
        )


@dataclass
class Concern:
    """一件她没解开的小事：反刍、矛盾、阈值都围着它转。"""

    key: str
    text: str                      # 第一人称，模型生成
    ts: float = field(default_factory=_now)
    last_echo: float = 0.0         # 上次被反刍到的时刻
    significance: float = 0.6      # 显著性（随时间衰减，被提起会回涨）
    # 矛盾：同一件事的正反两面，可以同时存在
    pull_toward: str = ""          # 想靠近的理由
    pull_away: str = ""            # 想退开的理由
    resolved: bool = False
    # 被相似话题戳到的关键词（模型生成时给）
    triggers: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(raw: Any) -> Optional["Concern"]:
        if not isinstance(raw, dict):
            return None
        key = str(raw.get("key") or "").strip()
        text = str(raw.get("text") or "").strip()
        if not key or not text:
            return None
        triggers = raw.get("triggers") or []
        if not isinstance(triggers, (list, tuple)):
            triggers = []
        return Concern(
            key=key,
            text=text,
            ts=_safe_float(raw.get("ts"), _now()),
            last_echo=_safe_float(raw.get("last_echo"), 0.0),
            significance=_clamp(_safe_float(raw.get("significance"), 0.6), 0.0, 1.0),
            pull_toward=str(raw.get("pull_toward") or ""),
            pull_away=str(raw.get("pull_away") or ""),
            resolved=bool(raw.get("resolved", False)),
            triggers=[str(t) for t in triggers if str(t).strip()][:8],
        )


@dataclass
class ViewOfPeer:
    """带原文依据的主观模型印象；未核实的假设不得作为用户事实。"""

    traits: List[str] = field(default_factory=list)
    trait_evidence: Dict[str, str] = field(default_factory=dict)
    expectation: str = ""
    expectation_source: str = ""
    expectation_evidence: str = ""
    misread: str = ""
    misread_source: str = ""
    misread_evidence: str = ""
    updated_at: float = field(default_factory=_now)
    evidence_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(raw: Any) -> Optional["ViewOfPeer"]:
        if not isinstance(raw, dict):
            return None
        raw_traits = raw.get("traits") or []
        raw_evidence = raw.get("trait_evidence") or {}
        if not isinstance(raw_traits, (list, tuple)):
            raw_traits = []
        if not isinstance(raw_evidence, dict):
            raw_evidence = {}
        traits: List[str] = []
        trait_evidence: Dict[str, str] = {}
        for raw_trait in raw_traits:
            trait = str(raw_trait).strip()
            evidence = str(raw_evidence.get(trait) or "").strip()
            if trait and evidence and trait == evidence and trait not in traits:
                traits.append(trait)
                trait_evidence[trait] = evidence
        traits = traits[:8]
        trait_evidence = {t: trait_evidence[t] for t in traits}

        def hypothesis(name: str) -> Tuple[str, str, str]:
            text = str(raw.get(name) or "").strip()
            source = str(raw.get(f"{name}_source") or "")
            evidence = str(raw.get(f"{name}_evidence") or "").strip()
            if not text or source != "user_text" or not evidence:
                return "", "", ""
            return text, source, evidence

        expectation, expectation_source, expectation_evidence = hypothesis("expectation")
        misread, misread_source, misread_evidence = hypothesis("misread")
        verified_count = len(traits) + bool(expectation_evidence) + bool(misread_evidence)
        return ViewOfPeer(
            traits=traits,
            trait_evidence=trait_evidence,
            expectation=expectation,
            expectation_source=expectation_source,
            expectation_evidence=expectation_evidence,
            misread=misread,
            misread_source=misread_source,
            misread_evidence=misread_evidence,
            updated_at=_safe_float(raw.get("updated_at"), _now()),
            evidence_count=int(verified_count),
        )


# ---------------------------------------------------------------------------
# 结构化状态
# ---------------------------------------------------------------------------

class PsycheState:
    """一个角色、一个对方的心里账。按 (role_id, user_id) 分。"""

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id
        # 需求余额：0=满足，1=欠到顶。互动满足会降，忽略/冷落会涨。
        self.needs: Dict[str, float] = {n: 0.25 for n in NEEDS}
        self.concerns: Dict[str, Concern] = {}
        self.view = ViewOfPeer()
        self.journal: List[Entry] = []
        # 上次写入/上次被提起的时刻
        self.last_entry_at: float = 0.0
        self.last_interaction_at: float = _now()
        self.last_proactive_at: float = 0.0
        # 运行期剂量旋钮（v3.1.53）：默认值=模块常量；PsycheStore.state() 会把
        # 面板配置覆盖上来。不参与 to_dict/from_dict，不落盘。
        self.rumination_threshold: float = RUMINATION_THRESHOLD
        self.inject_max_lines: int = INJECT_MAX_LINES

    # ---- 需求 ----
    def satisfy_need(self, name: str, amount: float) -> None:
        if name in self.needs:
            self.needs[name] = _clamp(self.needs[name] - amount, 0.0, 1.0)

    def frustrate_need(self, name: str, amount: float) -> None:
        if name in self.needs:
            self.needs[name] = _clamp(self.needs[name] + amount, 0.0, 1.0)

    def decay(self, now: float | None = None) -> None:
        """时间推进：需求自然回落、心事显著性衰减。按距上次的时间按比例算。"""
        now = now if now is not None else _now()
        elapsed = max(0.0, now - self.last_interaction_at)
        days = elapsed / 86400.0
        if days > 0:
            for n in NEEDS:
                self.needs[n] = _clamp(
                    self.needs[n] * (1.0 - NEED_DAILY_RECOVERY) ** days, 0.0, 1.0
                )
            for c in self.concerns.values():
                c.significance = _clamp(
                    c.significance * (SIGNIFICANCE_DAILY_DECAY ** days), 0.0, 1.0
                )
        self.last_interaction_at = now

    def top_need(self) -> Tuple[str, float]:
        name = max(NEEDS, key=lambda n: self.needs[n])
        return name, self.needs[name]

    # ---- 心事 ----
    def add_concern(self, concern: Concern) -> None:
        if concern.resolved:
            return
        self.concerns[concern.key] = concern

    def resolve_concern(self, key: str) -> None:
        c = self.concerns.get(key)
        if c:
            c.resolved = True
            c.significance = 0.0

    def active_concerns(self) -> List[Concern]:
        return [c for c in self.concerns.values() if not c.resolved and c.significance > 0.05]

    # ---- 反刍：此刻哪件事该浮上来 ----
    def ruminating(self, *, local_hour: int = 12, text: str = "",
                   body_modulation: float = 0.0,
                   now: float | None = None) -> List[Concern]:
        """返回此刻够格浮上来的心事（最多 INJECT_MAX_LINES 条）。

        显著性 × 时间成熟 × 夜间加成 × 话题戳中 × 身体调制 − 刚提过的抑制。
        body_modulation：正数=清醒有精力（更想得动），负数=困/累（不想纠结）。
        """
        now = now if now is not None else _now()
        night = 1.0 if (local_hour >= 22 or local_hour < 5) else 0.0
        out: List[Tuple[float, Concern]] = []
        for c in self.active_concerns():
            age = max(0.0, now - c.ts) / 3600.0
            mature = 1.0 - math.exp(-age / 6.0)          # 6 小时内逐渐「成熟」
            since_echo = (now - c.last_echo) / 3600.0 if c.last_echo else 999.0
            fresh = 0.0 if since_echo < 1.0 else min(1.0, since_echo / 12.0)
            topic = 0.0
            if text:
                low = text.lower()
                topic = 0.25 if any(t and t.lower() in low for t in c.triggers) else 0.0
            score = c.significance * mature + night * NIGHT_BOOST + topic
            score += body_modulation * 0.10
            score *= (0.5 + 0.5 * fresh)                 # 刚提过就压住，别连着念叨
            if score >= getattr(self, "rumination_threshold", RUMINATION_THRESHOLD):
                out.append((score, c))
        out.sort(key=lambda x: x[0], reverse=True)
        return [c for _, c in out[:getattr(self, "inject_max_lines", INJECT_MAX_LINES)]]

    def note_echo(self, key: str, now: float | None = None) -> None:
        now = now if now is not None else _now()
        c = self.concerns.get(key)
        if c:
            c.last_echo = now
            c.significance = _clamp(c.significance + 0.15, 0.0, 1.0)

    # ---- 心智理论 ----
    def update_view(self, traits: Sequence[Any] = (), expectation: Any = None,
                    misread: Any = None, *, source_text: str = "",
                    clear_expectation: bool = False, clear_misread: bool = False,
                    now: float | None = None) -> bool:
        source_text = str(source_text or "")
        now = now if now is not None else _now()
        changed = False
        for item in traits:
            if not isinstance(item, dict):
                continue
            trait = str(item.get("text") or "").strip()
            evidence = str(item.get("evidence") or "").strip()
            if not trait or not evidence or trait != evidence or evidence not in source_text:
                continue
            if trait not in self.view.traits:
                self.view.traits.append(trait)
                self.view.trait_evidence[trait] = evidence
                changed = True
            elif self.view.trait_evidence.get(trait) != evidence:
                self.view.trait_evidence[trait] = evidence
                changed = True
        self.view.traits = self.view.traits[:8]
        self.view.trait_evidence = {
            t: self.view.trait_evidence[t] for t in self.view.traits
            if self.view.trait_evidence.get(t)
        }

        def update_hypothesis(name: str, value: Any, clear: bool) -> None:
            nonlocal changed
            if clear:
                for suffix in ("", "_source", "_evidence"):
                    attr = name + suffix
                    if getattr(self.view, attr):
                        setattr(self.view, attr, "")
                        changed = True
                return
            if not isinstance(value, dict):
                return
            text = str(value.get("text") or "").strip()
            evidence = str(value.get("evidence") or "").strip()
            if not text or not evidence or evidence not in source_text:
                return
            if (getattr(self.view, name) != text
                    or getattr(self.view, f"{name}_source") != "user_text"
                    or getattr(self.view, f"{name}_evidence") != evidence):
                setattr(self.view, name, text)
                setattr(self.view, f"{name}_source", "user_text")
                setattr(self.view, f"{name}_evidence", evidence)
                changed = True

        update_hypothesis("expectation", expectation, clear_expectation)
        update_hypothesis("misread", misread, clear_misread)
        if changed:
            self.view.evidence_count = (
                len(self.view.traits)
                + bool(self.view.expectation_evidence)
                + bool(self.view.misread_evidence)
            )
            self.view.updated_at = now
        return changed

    # ---- 记忆重构：新经历改写旧解释 ----
    def reframe(self, concern_key: str, new_view: str,
                now: float | None = None) -> bool:
        now = now if now is not None else _now()
        for e in self.journal:
            if e.concern_key == concern_key:
                e.now_view = str(new_view)
                if not e.then:
                    e.then = e.text
        c = self.concerns.get(concern_key)
        if c:
            c.text = str(new_view) or c.text
        return True

    # ---- 主动压强：交给自主社交层 ----
    def proactive_pressure(self, *, local_hour: int = 12,
                           now: float | None = None) -> float:
        """0..1，越高越该主动开口。规则确定，不产文字。"""
        now = now if now is not None else _now()
        _, top = self.top_need()
        rum = len(self.ruminating(local_hour=local_hour, now=now))
        rum_score = min(1.0, rum / 2.0)
        misread_score = 0.5 if self.view.misread else 0.0
        pressure = 0.55 * top + 0.35 * rum_score + 0.10 * misread_score
        return _clamp(pressure, 0.0, 1.0)

    # ---- 注入文本（只做「选中哪几句」的拼装，文字本身是生成的）----
    def urge_to_speak(self, *, local_hour: int = 12, text: str = "",
                       body_modulation: float = 0.0,
                       now: float | None = None) -> str:
        """此刻她「自己想说」的一句，来自反刍/需求/误判——不是插件排的由头。

        与 `injection_lines` 的区别：injection 只拼「够格浮上来的心事」；urge 看的是
        「她心里有没有憋着一句非说不可的话」——压强过了阈值才给，而且只取她自己
        生成过的第一人称原文（concern.text），不在这里造新句子。

        「发言像被插件引起」的根子是：社交层只拿日程/天气这类排出来的由头开口。
        有了 urge，模型手里就有一句「她惦记着、压了一阵子」的话，开口动机来自她
        自己心里的账，而不是插件今天替她安排了什么。安静仍是常态——压强不够就
        返回空串。
        """
        now = now if now is not None else _now()
        pressure = self.proactive_pressure(local_hour=local_hour, now=now)
        if pressure < 0.55:
            return ""
        lines = self.injection_lines(
            local_hour=local_hour, text=text,
            body_modulation=body_modulation, now=now,
        )
        if not lines:
            return ""
        # 只递第一句最要紧的：一次憋一句才像人，倒豆子不像。
        return lines[0]

    def injection_lines(self, *, local_hour: int = 12, text: str = "",
                        body_modulation: float = 0.0,
                        now: float | None = None) -> List[str]:
        """拿反刍到的心事 + 最欠的需求，拼成第一人称的轻提示。

        这些文字都是模型自己之前生成的 concern.text，psyche 只负责「把哪句递上去」。
        未核实的 peer-view 假设不会进入 reply hints。绝不在这里造新句子——造了就是新的念稿源。
        """
        now = now if now is not None else _now()
        lines: List[str] = []
        for c in self.ruminating(local_hour=local_hour, text=text,
                                 body_modulation=body_modulation, now=now):
            lines.append(c.text)

        # Peer-view hypotheses remain internal and are not projected as reply hints.
        return lines[:getattr(self, "inject_max_lines", INJECT_MAX_LINES)]

    # ---- 序列化 ----
    def to_dict(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id,
            "needs": self.needs,
            "concerns": {k: c.to_dict() for k, c in self.concerns.items()},
            "view": self.view.to_dict(),
            "last_entry_at": self.last_entry_at,
            "last_interaction_at": self.last_interaction_at,
            "last_proactive_at": self.last_proactive_at,
        }

    @staticmethod
    def from_dict(raw: Any) -> Optional["PsycheState"]:
        if not isinstance(raw, dict):
            return None
        uid = str(raw.get("user_id") or "")
        if not uid:
            return None
        st = PsycheState(uid)
        needs = raw.get("needs") or {}
        if isinstance(needs, dict):
            for n in NEEDS:
                st.needs[n] = _clamp(_safe_float(needs.get(n), 0.25), 0.0, 1.0)
        concerns = raw.get("concerns") or {}
        if isinstance(concerns, dict):
            for k, v in concerns.items():
                c = Concern.from_dict(v)
                if c:
                    st.concerns[str(k)] = c
        v = ViewOfPeer.from_dict(raw.get("view"))
        if v:
            st.view = v
        st.last_entry_at = _safe_float(raw.get("last_entry_at"), 0.0)
        st.last_interaction_at = _safe_float(raw.get("last_interaction_at"), _now())
        st.last_proactive_at = _safe_float(raw.get("last_proactive_at"), 0.0)
        return st


# ---------------------------------------------------------------------------
# 持有多人的心理账 + 落盘
# ---------------------------------------------------------------------------

class PsycheStore:
    """一个角色的一份心理库：含所有对方的状态 + 自述本，负责原子读写。"""

    def __init__(self, data_dir: Path | str, role_id: str,
                 config_provider: Any = None) -> None:
        self.dir = Path(data_dir)
        self.role_id = str(role_id)
        self.config_provider = config_provider
        self.states: Dict[str, PsycheState] = {}
        self.journal: List[Entry] = []
        self._load()

    def _cfg(self, name: str, default: Any) -> Any:
        provider = self.config_provider
        if callable(provider):
            return getattr(provider(), name, default)
        if provider is not None:
            return getattr(provider, name, default)
        return default

    def _apply_runtime_config(self, st: "PsycheState") -> "PsycheState":
        """把面板上的剂量旋钮挂到 state（不序列化、不落盘），给纯方法读。"""
        st.rumination_threshold = float(
            self._cfg("psyche_rumination_threshold", RUMINATION_THRESHOLD))
        st.inject_max_lines = int(
            self._cfg("psyche_inject_max_lines", INJECT_MAX_LINES))
        return st

    def _owner_tag(self) -> str:
        return hashlib.sha256(self.role_id.encode("utf-8")).hexdigest()[:24]

    def _state_path(self) -> Path:
        return self.dir / f"psyche_state.{self._owner_tag()}.json"

    def _journal_path(self) -> Path:
        return self.dir / f"psyche_journal.{self._owner_tag()}.jsonl"

    def _load(self) -> None:
        try:
            raw = json.loads(self._state_path().read_text(encoding="utf-8"))
            if isinstance(raw, dict) and str(raw.get("role_id") or "") == self.role_id:
                people = raw.get("people") or {}
                if isinstance(people, dict):
                    for uid, p in people.items():
                        st = PsycheState.from_dict(p)
                        if st:
                            self.states[str(uid)] = st
        except (OSError, ValueError, TypeError):
            pass
        try:
            with self._journal_path().open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        raw = json.loads(line)
                    except (ValueError, TypeError):
                        continue  # 坏行跳过，不影响其余记录
                    e = Entry.from_dict(raw)
                    if e and e.role_id == self.role_id and e.user_id:
                        self.journal.append(e)
            self.journal = self.journal[-JOURNAL_MAX:]
        except (OSError, ValueError):
            pass

    def state(self, user_id: str) -> PsycheState:
        st = self.states.get(user_id)
        if st is None:
            st = PsycheState(user_id)
            self.states[user_id] = st
        return self._apply_runtime_config(st)

    def _atomic_write(self, path: Path, data: str) -> bool:
        """临时文件 + replace，Windows 上被占用就放弃这次写（不阻塞主路径）。"""
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.dir), prefix=".psyche-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(data)
                os.replace(tmp, path)
                return True
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                return False
        except OSError:
            return False

    def save_state(self) -> bool:
        payload = {
            "role_id": self.role_id,
            "people": {uid: st.to_dict() for uid, st in self.states.items()},
        }
        return self._atomic_write(self._state_path(),
                                  json.dumps(payload, ensure_ascii=False, indent=2))

    def append_entry(self, entry: Entry) -> bool:
        if entry.role_id and entry.role_id != self.role_id:
            return False
        if not str(entry.user_id or "").strip():
            return False
        entry.role_id = self.role_id
        self.journal.append(entry)
        self.journal = self.journal[-JOURNAL_MAX:]
        # append：把新行直接追加（比整本重写便宜；坏行下次 _load 会跳过）
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            with self._journal_path().open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
            return True
        except OSError:
            return False

    # ---- 生成节奏：事件驱动 + 节流 ----
    def should_write(self, user_id: str, *, now: float | None = None,
                     min_interval: float = 180.0, daily_cap: int = 40) -> bool:
        """该不该为这条消息生成一条心理记录。

        事件驱动由调用方判（用 `worth_recording`），这里只管节流：
        距上次至少 min_interval 秒、当天不超过 daily_cap 条。
        """
        now = now if now is not None else _now()
        st = self.state(user_id)
        if now - st.last_entry_at < min_interval:
            return False
        today = sum(1 for e in self.journal
                    if e.role_id == self.role_id and e.user_id == str(user_id)
                    and 0 <= now - e.ts < 86400)
        return today < daily_cap


def worth_recording(user_text: str, her_reply: str = "", *,
                    mood_shift: float = 0.0) -> bool:
    """这一轮值不值得写一条：有情绪波动、有自我暴露、有关于对方的新信息。"""
    if mood_shift >= 0.25:
        return True
    blob = f"{user_text} {her_reply}"
    # 自我暴露 / 感受句 / 关系指向词——命中才写，闲聊不写。
    markers = ("我觉得", "我想", "我有点", "其实", "说实话", "不知道为什么",
               "你", "我们", "发现", "突然", "想起", "上次", "记得", "但是", "不过")
    return any(m in blob for m in markers) and len(blob.strip()) >= 4


# ---------------------------------------------------------------------------
# 生成写入：内容全部由模型产出，psyche 只负责「何时写、写到哪、读回哪句」
# ---------------------------------------------------------------------------

PSYCHE_PROMPT = """你在扮演的角色刚刚和人聊完一轮。请以这个角色的第一人称，写她此刻心里真正留下的一点东西——不是复述对话，是她自己没说出口的、事后还在心里转的那一小下。

角色人设：
{persona}

这一轮对话：
对方：{user_text}
她：{her_reply}

她此刻的身体感觉（会影响心思）：{body_tag}
她现在惦记的旧事（模型生成的主观内心记录，不是对方的已核实事实）：{old_concerns}
她眼里对方的样子（逐项保留来源；假设不是事实）：{view}

要求：
- 全部用第一人称「我」，是她的心里话，不是旁白、不是给对方看的汇报。
- entry 是模型生成的内部记录，不是对方说过的话；保留本轮对方原文作为来源，不要把推断改写成对方事实。
- traits 只可逐字摘录对方原文中明确、稳定的自我描述或一般偏好；不能从一次行为、语气或状态概括。trait 文本必须与 evidence 完全一致。
- expectation 和 misread 是可撤回的模型假设，必须逐字引用本轮对方原文作为 evidence；没有可引用依据时输出 null，不要沿用成事实。
- 旧印象仅供角色反思；不能把旧假设升级为事实。若本轮有新证据可改写或撤回旧假设。
- 只写真正留下的东西。如果这一轮什么都没留下，entry 就写空字符串。
- 严格输出 JSON，不要解释、不要 Markdown 代码块：
{{
  "entry": "一句第一人称的心里话，没留下就空字符串",
  "concern": {{"key": "短标识(英文或数字)", "text": "这件没解开的事(第一人称)", "pull_toward": "想靠近的理由", "pull_away": "想退开的理由", "triggers": ["会戳到这件事的词"]}} 或 null,
  "resolve": "要放下的旧事的 key 或空字符串",
  "reframe": {{"key": "旧事 key", "now_view": "现在重新怎么想这件事"}} 或 null,
  "view": {{"traits": [{{"text": "对方明确自述的逐字片段", "evidence": "本轮对方原文中的逐字依据"}}], "expectation": {{"text": "可撤回假设", "evidence": "本轮对方原文逐字依据"}} 或 null, "misread": {{"text": "可撤回假设", "evidence": "本轮对方原文逐字依据"}} 或 null}} 或 null,
  "needs": {{"response": -0.1到0.1, "understanding": -0.1到0.1, "space": -0.1到0.1, "novelty": -0.1到0.1}}
}}
needs 里正数=这件事被挫败(更缺)，负数=被满足(不那么缺)。只写这一轮真实变化的，没变化的写 0。"""


class PsycheWriter:
    """低频、事件驱动的生成写入。复用日程的模型链与保留额度。"""

    def __init__(self, store: "PsycheStore", gateway: Any, config: Any,
                 persona_provider: Any = None, logger: Any = None,
                 time_source: Any = None):
        self.store = store
        self.gateway = gateway
        self.config = config
        self.persona_provider = persona_provider
        self.log = logger
        self._time = time_source or _now
        self.last_error = ""

    def _cfg(self, name: str, default: Any) -> Any:
        return getattr(self.config(), name, default) if callable(self.config) else getattr(self.config, name, default)

    async def write_once(self, user_id: str, *, user_text: str, her_reply: str,
                         body_tag: str = "", local_hour: int = 12) -> bool:
        """生成并落盘一条心理记录。返回是否真的写了。失败一律吞掉（增强不能拖累主链路）。"""
        if self.gateway is None:
            return False
        st = self.store.state(user_id)
        now = self._time()
        # 事件驱动：没值得记的东西就不写；再过节流闸。
        if not worth_recording(user_text, her_reply):
            return False
        if not self.store.should_write(user_id, now=now,
                                       min_interval=float(self._cfg("psyche_min_interval_seconds", 180.0)),
                                       daily_cap=int(self._cfg("psyche_daily_cap", 40))):
            return False
        # 人设（同步读缓存；拿不到就空串，生成退化但不炸）
        persona = ""
        if callable(self.persona_provider):
            try:
                persona = str(self.persona_provider() or "")
            except Exception:
                persona = ""
        old = "、".join(
            f"模型生成的主观内心记录（非已核实事实）：{c.text}"
            for c in st.active_concerns()[:3]
        ) or "（没有）"
        view_parts = [
            f"对方明确原文（逐字片段，非归纳）：{trait}〔依据：{st.view.trait_evidence[trait]}〕"
            for trait in st.view.traits
            if st.view.trait_evidence.get(trait) == trait
        ]
        if (st.view.expectation and st.view.expectation_source == "user_text"
                and st.view.expectation_evidence):
            view_parts.append(
                f"可撤回的模型假设（不是对方事实；证据来自较早的用户原文，并非本轮内容）："
                f"{st.view.expectation}〔证据：{st.view.expectation_evidence}〕"
            )
        if st.view.misread and st.view.misread_source == "user_text" and st.view.misread_evidence:
            view_parts.append(
                f"可撤回的模型假设（不是对方事实；证据来自较早的用户原文，并非本轮内容）："
                f"{st.view.misread}〔证据：{st.view.misread_evidence}〕"
            )
        view = "；".join(view_parts) or "（尚无带直接原文依据的印象）"
        prompt = PSYCHE_PROMPT.format(
            persona=persona[:600] or "（未设置）",
            user_text=str(user_text)[:300],
            her_reply=str(her_reply)[:300],
            body_tag=body_tag or "没什么特别",
            old_concerns=old,
            view=view,
        )
        try:
            result = await self.gateway.generate(
                prompt=prompt,
                chain=self._cfg("schedule_provider_ids", ()),
                allow_global=bool(self._cfg("schedule_allow_global_fallback", True)),
                timeout=float(self._cfg("psyche_llm_timeout_seconds",
                                        self._cfg("schedule_llm_timeout_seconds", 30))),
                attempts_per_provider=1,
                purpose="日程生成",  # 复用日程保留额度，不挤占聊天预算、不改 llm.py 核心
                role_label=str(getattr(self.store, "role_id", "") or ""),
            )
        except Exception as exc:
            self.last_error = f"生成异常：{exc}"
            if self.log:
                self.log.warning(f"{LOG_PREFIX_PSYCHE} {self.last_error}")
            return False
        if not getattr(result, "ok", False) or not getattr(result, "text", ""):
            return False
        data = _parse_psyche_json(getattr(result, "text", ""))
        if data is None:
            self.last_error = "生成结果不是合法 JSON"
            return False
        return self._apply(
            user_id, data, now=now, body_tag=body_tag, source_text=str(user_text),
        )

    def _apply(self, user_id: str, data: Dict[str, Any], *, now: float,
               body_tag: str = "", source_text: str = "") -> bool:
        st = self.store.state(user_id)
        wrote = False
        # 放下旧事
        resolve = str(data.get("resolve") or "").strip()
        if resolve:
            st.resolve_concern(resolve)
            wrote = True
        # 重构旧解释
        reframe = data.get("reframe")
        if isinstance(reframe, dict):
            key = str(reframe.get("key") or "").strip()
            if key:
                st.reframe(key, str(reframe.get("now_view") or ""), now=now)
                self.store.append_entry(Entry(
                    text=str(reframe.get("now_view") or ""), kind="reframe",
                    role_id=self.store.role_id, user_id=str(user_id),
                    ts=now, concern_key=key, now_view=str(reframe.get("now_view") or ""),
                    source_kind="model_internal", source_text=source_text,
                ))
                wrote = True
        # 新心事
        cdata = data.get("concern")
        c = Concern.from_dict(cdata) if isinstance(cdata, dict) else None
        if c:
            c.ts = now
            st.add_concern(c)
            wrote = True
        # 自述 entry
        entry_text = str(data.get("entry") or "").strip()
        if entry_text:
            self.store.append_entry(Entry(
                text=entry_text, kind="self", ts=now,
                role_id=self.store.role_id, user_id=str(user_id),
                concern_key=c.key if c else "",
                then=entry_text, body_tag=body_tag,
                significance=c.significance if c else 0.4,
                source_kind="model_internal", source_text=source_text,
            ))
            wrote = True
        # 心智模型更新
        vdata = data.get("view")
        if isinstance(vdata, dict):
            traits = vdata.get("traits") or []
            if not isinstance(traits, (list, tuple)):
                traits = []
            wrote = st.update_view(
                traits=traits,
                expectation=vdata.get("expectation"),
                misread=vdata.get("misread"),
                source_text=source_text,
                clear_expectation=("expectation" in vdata and vdata.get("expectation") is None),
                clear_misread=("misread" in vdata and vdata.get("misread") is None),
                now=now,
            ) or wrote
        # 需求变化
        needs = data.get("needs")
        if isinstance(needs, dict):
            for n in NEEDS:
                delta = _safe_float(needs.get(n), 0.0)
                delta = _clamp(delta, -0.2, 0.2)
                if delta > 0:
                    st.frustrate_need(n, delta)
                elif delta < 0:
                    st.satisfy_need(n, -delta)
                if delta:
                    wrote = True
        if wrote:
            st.last_entry_at = now
            self.store.save_state()
        return wrote


LOG_PREFIX_PSYCHE = "[humanoid_core/psyche] "


def _parse_psyche_json(text: str) -> Optional[Dict[str, Any]]:
    """从模型输出里抠出 JSON：容忍 ```json 围栏和前后废话。"""
    if not text:
        return None
    raw = str(text).strip()
    # 去掉 Markdown 代码围栏
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
    # 找第一个 { 到最后一个 }
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(raw[start:end + 1])
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None

