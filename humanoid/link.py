"""与「自主拟人社交」插件之间的契约。

v1.7.4 的社交层直接按结构读 Core 的 state.json（`roles[bid].self.energy` 等）。那是
无契约的结构耦合：Core 改个字段名，联动就静默断掉，而且 Core 当时根本没有「她现在多想
说话」这一层可以给它。

现在两边各写各的文件，永不并发写同一个：

* Core → `state.json` 里 `roles[bid].self.contract`，带 `v` 版本号，字段只加不改。
* Social → 自己目录下的 `humanoid_signals.json`，Core 只读，并按 mtime 忽略过期数据。

社交层拿不到契约时（Core 没装、或版本太老）会退回读旧字段，功能不中断。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional

from . import __version__
from . import contract as _contract
from . import state_snapshot as _snapshot
from .clock import in_deep_window, in_night_window
from .life_log import parse_open
from .services.schedule import (
    day_phrases,
    schedule_wake_text,
    sleep_spans,
)

#: v3.0.3 起为 3：`body` 里的身体轴数值可以是 `null`（`available:false` 表示「这一项
#: 没在跑/不知道」，而不是「不困、不饿、醒着」）。数值域新增 null 对按算术读的下游是
#: **破坏性**变更，所以必须抬版本——社交层看到 v<3 就还按「数字都是真的」读。
CONTRACT_VERSION = 3
#: 结构标识。**现状要说实**：截至 autonomous_social v1.28.17，社交层并不校验它、也不读
#: `fields_hash`（它的 core_bridge.py 自认没有校验方）——这三键目前是 Core 侧导出的死数据，
#: 保留是为了「只加不改」：以后对方接上校验时不必再动 Core。
CONTRACT_SCHEMA = "humanoid.contract/3"
#: 这一版契约提供的能力。社交层按能力判断走哪条路，不靠版本号猜。
#: 只加不改：`dreams`（她昨晚做的梦）与 `wake`（醒来状态/睡眠惯性）是 v2.28.0 新增的；
#: `event`（刚刚发生的事够重）**v2.28.5 起就有**（旧注释误写成 v2.29.0——那是个从未发布
#: 过的版本号，联动 P2-28），3.0.0 起继续提供只读能力。API_VERSION 保持 2。
#: v2.28.9 再补 `identity`（她是谁：参照名/人设标签）与 `outfit`（她此刻穿着）——
#: 社交层主动开口时终于能「以她的身份、说她当下的事」说话。
#: v3.0.3 起 `body.*` / `wake.asleep` 可能是 null，读之前先看 `body.available`
#: （false = 这一块不可用，原因在 `body.reason`）。**三态不是一条 capability**：
#: 这个元组的语义是「每个名字在契约里有一个对应的块」（测试逐条断言），而三态是一种
#: 读法，没有自己的块。要判「对面懂不懂三态」，看 `v >= 3` 或 `schema` 结尾的版本号；
#: 旧契约里数字恒为真值，不能把 null 当 0。
CONTRACT_CAPABILITIES = (
    "body", "feelings", "form", "activity", "day", "ongoing", "routine",
    "dreams", "wake", "event", "identity", "outfit",
)

# 社交层写回的信号文件：超过这个秒数就不采信。
#
# 它的心跳间隔是**可配的**（`heartbeat_max_minutes`：默认 15 分钟、最大 120 分钟），
# 所以 TTL 必须盖得住「最大间隔 + 余量」。原来写死 900 秒，正好等于默认的 15 分钟 ——
# 任何调度抖动都会越界；用户把心跳配成 120 分钟时，约 87% 的时间都判「已过期」，
# Core 会把社交层写回的信号整段丢掉（表现为「她刚主动找过谁」永远不生效）。
#
# 写方可以在信号文件里用 `ttl_seconds` 声明自己实际的保鲜期，这里优先采用它；
# 这个常量是写方没声明时的兜底。
SIGNALS_TTL_SECONDS = 3 * 3600.0
# 写方声明的 TTL 也要夹一下：声明成一周就失去「社交层已经停了」的检测意义了。
SIGNALS_TTL_MAX_SECONDS = 6 * 3600.0
# 下限：声明得比一分钟还短，只会让信号频繁被判过期，没有意义。
SIGNALS_TTL_MIN_SECONDS = 60.0
SOCIAL_PLUGIN_DIR_NAME = "astrbot_plugin_autonomous_social"
SIGNALS_FILE_NAME = "humanoid_signals.json"
# 信号文件正常只有几百字节。涨到兆级说明写方出事了（或文件坏了），读进来只会把内存
# 吃光；判它「读不到」比判它「里面没有内容」安全——见 `status()` 里 `unreadable` 的说明。
SIGNALS_MAX_BYTES = 1 << 20


#: 偏移来源：她的城市解析出了 IANA 时区（偏移是**她的**）。
OFFSET_SOURCE_ZONE = "zone"
#: 偏移来源：退回宿主机时间（城市认不出 / 缺 tzdata / 明确跟随系统）——偏移是**这台机器的**。
OFFSET_SOURCE_HOST = "host"
#: 偏移来源：连宿主偏移都拿不到（naive 时钟）。此时 `minutes` 必为 None。
OFFSET_SOURCE_UNKNOWN = "unknown"


def utc_offset(core: Any, moment: Any = None) -> dict[str, Any]:
    """她此刻的 UTC 偏移，以及**这个偏移是哪来的**：契约 `time` 块与门面 `clock()` 的唯一一份。

    为什么需要来源：`Clock.now()` 在城市认不出 / 缺 tzdata 时退回宿主机时间
    （clock.py:136-140），此时 `now.utcoffset()` 给的是这台机器的偏移。旧代码把它当成
    「她的偏移」导出，而契约注释自己写着「算不出来时必须是 None 而不是 0：当成 0 等于把
    她的城市当 UTC」（审核 跨插件契约 5）。现在偏移照给（宿主机偏移并非无用——同机消费方
    拿它换算 epoch 仍然正确），但用 `source` 说清它不是她的：`zone` / `host` / `unknown`。

    返回 `{"minutes": int|None, "source": str, "tz": str}`：
    * `minutes` 为 None 只在 naive 时钟（连宿主偏移都没有）时出现；
    * `tz` 只在 `zone` 时非空——宿主机时区**不是**她的城市，导出去会让消费方以为她在那里。
    """
    if moment is None:
        try:
            moment = core.clock.now()
        except Exception:
            moment = None

    minutes: Optional[int] = None
    try:
        delta = moment.utcoffset() if moment is not None else None
    except Exception:
        delta = None
    if delta is not None:
        minutes = int(delta.total_seconds() // 60)
    if minutes is None:
        return {"minutes": None, "source": OFFSET_SOURCE_UNKNOWN, "tz": ""}

    clock = getattr(core, "clock", None)
    try:
        resolved: Any = getattr(clock, "tz", None)
    except Exception:
        resolved = None
    if resolved is not None:
        # 真 Clock：`tz` 是解析出来的 ZoneInfo（None = 退回宿主机）。
        return {
            "minutes": minutes,
            "source": OFFSET_SOURCE_ZONE,
            "tz": str(getattr(resolved, "key", "") or ""),
        }

    # 桩 / 老 Core 没有 `tz`：退回 `zone_state()` 的 zone_name 与 note（两者都为空 = 没解析出来）。
    state = None
    try:
        state = clock.zone_state()
    except Exception:
        state = None
    if state is not None:
        zone_name = str(getattr(state, "zone_name", "") or "")
        note = str(getattr(state, "note", "") or "")
        if zone_name and not note:
            return {"minutes": minutes, "source": OFFSET_SOURCE_ZONE, "tz": zone_name}
        return {"minutes": minutes, "source": OFFSET_SOURCE_HOST, "tz": ""}

    # 连 zone_state 都没有（极端替身）：只认 moment 自带的 IANA 名。
    key = str(getattr(getattr(moment, "tzinfo", None), "key", "") or "")
    if key:
        return {"minutes": minutes, "source": OFFSET_SOURCE_ZONE, "tz": key}
    return {"minutes": minutes, "source": OFFSET_SOURCE_HOST, "tz": ""}


def build_contract(core: Any) -> dict[str, Any]:
    """导出这份角色的身体快照。只读，不推进任何状态。"""
    cfg = core.config
    now = core.clock.now()
    snap = core.snapshot(refresh=False)
    soma = snap.get("soma") or {}
    form = snap.get("form_policy") or {}
    slot = core.schedule.current_slot() or {}
    slots = core.schedule.current_slots()
    now_minutes = now.hour * 60 + now.minute
    # 她实际的夜只算一次，`time` 块与 `routine` 块共用（同一份契约里不该有两个答案）。
    window = night_window(core, cfg)
    hour_of_day = now.hour + now.minute / 60.0
    try:
        # 社交层拿这个去说「我刚从X回来」，不拿它就只能说「我在休息」。
        day = day_phrases(slots, now_minutes, past_limit=3, future_limit=2)
    except Exception:
        day = {}
    feelings: list[str] = []
    if cfg.soma_enabled:
        try:
            # 与聊天注入**同一个口径**：按显著度从高到低排，再取前三条。
            # 原来是 `feelings(...)[:3]`（按生成顺序截），只要有三条体感，
            # 「今天到这会」「手上正在做」「等下」就全被挤掉——主动消息退化成
            # 「我现在很饿」，她在哪、在干什么整个消失，和聊天时像两个人。
            ranked = sorted(
                core.soma.feelings(float(snap["energy"]["value"])),
                key=lambda item: item[0],
                reverse=True,
            )
            feelings = [text for _, text in ranked[:3]]
        except Exception:
            feelings = []
    offset_info = utc_offset(core, now)
    contract = {
        "v": CONTRACT_VERSION,
        # 版本自述只认真实包版本（humanoid.__version__）：拿不到实例属性时退回导入值，
        # 绝不写死字符串（联动 P2-28：旧代码在注释里写过不存在的 v2.29.0）。
        "core_version": str(getattr(core, "version", "") or __version__),
        "generated_at": round(now.timestamp(), 1),
        "time": {
            "local": now.strftime("%Y-%m-%d %H:%M"),
            "hour": now.hour,
            "weekday": snap.get("weekday", ""),
            # 地名可能不知道（display_city 现在可以是 None）：契约里"不知道"写成空串，
            # 不写成 null——下游按字符串读的地方很多，null 会让它们炸。
            "city": snap.get("city") or "",
            # 与 `routine.night_start_hour / night_end_hour` **同一个窗口**：以前这里按日程
            # （`in_scheduled_night`）、`routine` 块按生物钟夜（日程 → 配置兜底），同一份
            # 契约里「现在是不是夜里」能有两个答案（审核 跨插件契约 8）。
            "is_night": in_night_window(hour_of_day, window),
            "is_deep_sleep": in_deep_window(hour_of_day, window, cfg.night_deep_sleep_ratio),
            # 社交层跟 Core 跑在同一台机器上，但它自己的时段判断用的是本机时钟。
            # 给出偏移量比给出一个时刻更可靠：拿它把 epoch 换算成她的本地小时即可。
            # 算不出来时必须是 None 而不是 0：当成 0 等于把她的城市当 UTC，错得静悄悄。
            "utc_offset_minutes": offset_info["minutes"],
            # 这个偏移是谁的：`zone`（她的城市）/ `host`（这台机器，城市没解析出来）/
            # `unknown`（naive 时钟）。旧代码把宿主机偏移当成她的偏移导出——「算不出来时
            # 必须是 None」这句承诺在退化路径上从来没成立过（审核 跨插件契约 5）。
            "utc_offset_source": offset_info["source"],
            # 生效的 IANA 名；空串意味着她其实没拿到真时区（缺 tzdata / 城市认不出 / 跟随系统）。
            # `source=host` 时这里**必须**是空串：宿主机时区不是她的城市。
            "tz": offset_info["tz"],
        },
        "body": _body_block(core, cfg, snap, soma),
        "feelings": feelings,
        "form": {
            "max_chars": int(form.get("max_chars", 120)),
            "question_bias": float(form.get("question_bias", 0.35)),
            "long_reply_ok": bool(form.get("long_reply_ok", True)),
            "burst_ok": bool(form.get("burst_ok", False)),
        },
        "activity": {
            "name": str(slot.get("event", "")),
            "schedule_event": str(slot.get("event", "")),
            "location": str(slot.get("location", "")),
        },
        # 今天这条时线：刚做过什么 / 正在做什么 / 接下来做什么。
        # 她还没做完的那几件事。social 那边拿它当由头（「还在整理那份个案笔记」）
        # ——这是最像人的一种主动开口：说自己手上真在干的事，而不是天气。
        "ongoing": [str(x) for x in (core.schedule.ongoing_tasks() or []) if str(x).strip()][:3],
        # 「她自己想做的」——社交层拿它当由头（"她今天想去那家书店"），
        # 这是最像人的一种主动开口：说自己真想做的事，不是天气、也不是工作。
        "wants": [str(x) for x in (day.get("wants") or []) if str(x).strip()][:2],
        # 刚刚发生的事够重（被夸/被骂/被敷衍）。社交层拿它当由头——
        # 她被骂了一句却反过来主动找你说话，那才是活人；不知道这件事，
        # 它只会说天气和节日。空 dict = 这会儿没有这样一件事。
        "event": _event_block(core),
        "day": {
            "doing": str(day.get("doing") or ""),
            "done": [str(x) for x in (day.get("done") or []) if str(x).strip()],
            "next": [str(x) for x in (day.get("next") or []) if str(x).strip()],
            # 今天最近一段睡眠的梦（最多 3 条）。旧日程没有这个键 → 空列表；
            # 社交层拿它派「梦」由头（「她昨晚做了个梦」），没有就不说。
            "dreams": [
                str(x).strip() for x in (day.get("dreams") or []) if str(x or "").strip()
            ][:3],
        },
        # 醒来状态：睡着没有、醒来多久、睡眠惯性还剩几分钟、是不是被消息吵醒的。
        # 社交层可以用它避免「明明在睡却像生龙活虎」；拿不到时是空 dict（旧 Core 也一样）。
        "wake": _wake_block(core, cfg),
        # 日程是按哪个 AstrBot 人设排的：社交层靠它确认两边用的是同一个人。
        "persona": str(core.schedule.status().get("persona", "") or ""),
        # 她是谁（v2.28.9，只加不改）：社交层主动开口前要知道「用哪个名字/哪副人设说话」。
        # 两句短字符串，拿不到就是空串——绝不编名字，绝不因为人设没预热好让契约生成失败。
        "identity": _identity_block(core),
        # 她此刻穿着（v2.28.9，只加不改）：{"enabled","text","source"}。
        # 社交层拿它说「今天穿得少」这类真实细节，而不是每一轮都只有天气。
        # 拿不到（旧 Core / 穿搭关 / 判不出物种）→ 空 dict，社交层按「不知道」处理。
        "outfit": _outfit_block(core),
        # 作息：社交层靠它知道她睡够没睡够，诊断靠它提示两套时间不一致。
        "routine": _routine_block(core, cfg, slots, window),
        # 天气只递真天气：没配 Key 时那句「为什么没有」是给她主人看的，社交层拿到只会照着说。
        "weather": _contract_weather(snap.get("weather")),
        # 今天是什么节日（没有则空串）。社交层拿它派「节日」由头/破冰话题——
        # 节日是可预测的客观事实，不该让模型现编。
        "holiday": _contract_holiday(core),
        # 契约里没有按用户的数据（1000 人时会把文件撑大）；这些路径是稳定承诺。
        #
        # v3.0.0（联动 P2-25）：原来只有 5 条，而社交层实际按结构读的用户级键有 20+ 个
        # （ccb_*、attention、mood_tag、turn_count、base_* 等）。补齐后**注释要说实**：
        # 截至 autonomous_social v1.28.17，社交层并不读取/校验 `paths` 本身
        # （core_bridge.py 自认没有校验方），真正的消费点是它直接按结构读 state.json。
        # 保留并补齐是为了「只加不改」：哪天 social 接上校验，Core 这边不用再改。
        "paths": {
            # 旧承诺（保持不动）。
            "user_affection": "roles.<bid>.users.<uid>.mood.affection",
            "user_nickname": "roles.<bid>.users.<uid>.nickname",
            "user_display_name": "roles.<bid>.users.<uid>.display_name",
            "user_last_interaction": "roles.<bid>.users.<uid>.last_interaction",
            "user_said": "roles.<bid>.users.<uid>.said",
            # v3.0.0 补齐：social/core_bridge.py 实际读取的用户级键（只加不改）。
            "user_base_affection": "roles.<bid>.users.<uid>.base_affection",
            "user_name": "roles.<bid>.users.<uid>.name",
            "user_nickname_src": "roles.<bid>.users.<uid>.nickname_src",
            "user_mood_tag": "roles.<bid>.users.<uid>.mood_tag",
            "user_mood": "roles.<bid>.users.<uid>.mood",
            "user_mood_libido": "roles.<bid>.users.<uid>.mood.libido",
            "user_mood_base_libido": "roles.<bid>.users.<uid>.mood.base_libido",
            "user_mood_turn_count": "roles.<bid>.users.<uid>.mood.turn_count",
            "user_attention": "roles.<bid>.users.<uid>.attention",
            "user_last_message": "roles.<bid>.users.<uid>.last_message",
            "user_first_met": "roles.<bid>.users.<uid>.first_met",
            # v3.0.0（联动 P2-3）：TA 说话 / 她说话分开记（只加不改）。
            "user_peer_said_at": "roles.<bid>.users.<uid>.peer_said_at",
            "user_her_said_at": "roles.<bid>.users.<uid>.her_said_at",
            "user_ccb_done_count": "roles.<bid>.users.<uid>.ccb_done_count",
            "user_ccb_last_at": "roles.<bid>.users.<uid>.ccb_last_at",
            "user_ccb_satisfy": "roles.<bid>.users.<uid>.ccb_satisfy",
            "user_ccb_stage": "roles.<bid>.users.<uid>.ccb_stage",
            "user_ccb_turns": "roles.<bid>.users.<uid>.ccb_turns",
            "user_ccb_aftercare_until": "roles.<bid>.users.<uid>.ccb_aftercare_until",
        },
    }
    # ─── 握手三键（契约 v2）：只加不改 ───────────────────
    #
    # 设计意图：社交层以前只能靠 `v >= 1` 猜「对面是不是新版本」，字段对不上时静默
    # 拿到 None；这三键本想把「猜」变成「校验」。
    #
    # **现状（v3.0.0 如实注释）**：截至 autonomous_social v1.28.17，社交层全仓 0 命中，
    # 既不读 `schema` 也不校验 `fields_hash`——core 侧导出即死数据（联动 P2-25/P2-4）。
    # 保留而非删除的原因是「只加不改」：对方将来接上校验时，Core 侧不用再动。
    # 维护者看契约时不要误以为已经有人在用它们做兼容性检查。
    #
    # `fields_hash` 只覆盖**结构**（键名），不含取值——取值随时在变，那是 `generated_at`
    # 的活。所以它稳定、可缓存，只有真的增删字段时才会变。
    contract["schema"] = CONTRACT_SCHEMA
    contract["capabilities"] = list(CONTRACT_CAPABILITIES)
    contract["fields_hash"] = _fields_hash(contract)
    return contract


def _body_block(core: Any, cfg: Any, snap: dict[str, Any], soma: dict[str, Any]) -> dict[str, Any]:
    """身体块：13 个键一个不改（社交层按这些键读），但**「没在跑」与「不困」必须分得开**。

    v3.0.0 之前 `refresh_contract` 只要 `soma_enabled=false` 就整份停发，连带停掉
    identity/日程/天气/节日/事件/作息这些**与身体无关**的块；社交层把「没读到契约」
    误诊成「旧版 Core，升级到 v2.14+」（联动 P1-C/P2-26）。现在契约照发。

    v3.0.3（联动 跨插件契约 2）：关掉身体轴时**不再给中性数值**。以前这一块填的是
    `hunger=0.0 / arousal=50.0 / asleep=False / social_desire=50.0`——这些值与「她真的
    不饿、唤醒度居中、醒着、不想说话」**完全同形**，接收方按算术读，读出来是「她状态
    正常」。事实是「这一项我不知道」。现在的表达是：

    * `available`：这一块可不可用；`source`：值从哪来（`soma` / `disabled`）；
      `reason`：不可用的原因（可用时是空串）；
    * 不可用时，八个身体轴与「睡着」一律给 `null`，**不给 0/50/false**；
    * 精力 / 社交能量 / 周期这三项与身体轴开关无关，照给真实值（它们从来不是「没在跑」）。

    消费方必须按 `available` 分支：false 时不能把这些键当数字算，也不能当「不困」。
    """
    energy = round(float(snap["energy"]["value"]), 1)
    social_energy = round(float(snap["social_energy"]["value"]), 1)
    cycle_day = int(core.energy.cycle_day)
    cycle_phase = (snap.get("cycle") or "").strip()
    if cfg.soma_enabled and soma:
        asleep = soma.get("asleep")
        return {
            "available": True,
            "source": "soma",
            "reason": "",
            "energy": energy,
            "energy_text": snap["energy"]["text"],
            "sleep_pressure": soma.get("sleep_pressure"),
            "sleep_debt": soma.get("sleep_debt"),
            "hunger": soma.get("hunger"),
            "discomfort": soma.get("discomfort"),
            "arousal": soma.get("arousal"),
            # 三态：身体轴自己在说「不知道」时（空窗、睡眠窗口判不出）也传 None，
            # 不要 bool(None) 把它压成「醒着」。
            "asleep": None if asleep is None else bool(asleep),
            "last_sleep_hours": soma.get("last_sleep_hours"),
            "social_energy": social_energy,
            "social_desire": soma.get("social_desire"),
            "cycle_day": cycle_day,
            "cycle_phase": cycle_phase,
        }
    return {
        "available": False,
        "source": "disabled",
        "reason": "soma_disabled",
        "energy": energy,
        "energy_text": snap["energy"]["text"],
        "sleep_pressure": None,
        "sleep_debt": None,
        "hunger": None,
        "discomfort": None,
        "arousal": None,
        "asleep": None,
        "last_sleep_hours": None,
        "social_energy": social_energy,
        "social_desire": None,
        "cycle_day": cycle_day,
        "cycle_phase": cycle_phase,
    }


def _event_block(core: Any) -> dict[str, Any]:
    """刚刚发生的事够重——一句「她经历了什么」。

    只读、不取走：契约是给社交层看的快照，取了这一轮决定就用不上了。
    拿不到（旧 Core / 没这个机制）时空 dict，社交层按「没有这件事」处理。

    **故意不带 `age_seconds`**：它在事件存在的 30 分钟里每秒都在变，而契约挂在
    每分钟一次的身体推进上，于是那半小时里每分钟都要重写一遍全量 state.json
    （线上那份已经 2.4 MB）。社交层不需要精确秒数——由头有时效，
    那是 anchors 的 TTL 在管，不是这里的活。
    """
    try:
        pending = core.schedule.pending_event()
    except Exception:
        return {}
    if not isinstance(pending, dict) or not pending:
        return {}
    phrase = str(pending.get("phrase") or "").strip()
    if not phrase:
        return {}
    return {"phrase": phrase}


def _identity_block(core: Any) -> dict[str, Any]:
    """她是谁：`char_name`（参照名称）+ `persona_label`（今天这份日程按哪个人设排的）。

    v2.28.9 新增（只加不改）：社交层主动开口时，语气与自称要跟聊天侧同一个人。
    这里只出两个短字符串：

    * 拿不到（旧 Core / 人设还没预热 / 内部异常）→ 空串，**绝不拿 role_id 或「她」顶上**：
      role_id 是内部标识，社交层拿它叫人或自称会把「bot1」念出来；
    * 整个块永远存在（两个键都在），取值可以是空串——社交层按能力判断即可，
      不需要再猜「这个键是没有还是坏了」。

    只读：`char_name()` 在极端情况下会自愈一次自动补名（写一个内部 memo），那是
    Core 自己的既有行为，不会因为导出契约而多出用户级数据。
    """
    try:
        char_name = str(core.char_name() or "").strip()
    except Exception:
        char_name = ""
    try:
        persona_label = str(core.persona_label() or "").strip()
    except Exception:
        persona_label = ""
    try:
        sp = core.species_profile()
    except Exception:
        sp = {}
    if not isinstance(sp, dict):
        sp = {}
    try:
        sp_conf = float(sp.get("confidence") or 0.0)
    except (TypeError, ValueError):
        sp_conf = 0.0
    # 只有**真认出来**的才导出：人类不给 "species":"人类" 这种噪音。
    if sp_conf >= 0.65:
        sp_label = str(sp.get("label") or "").strip()
        sp_traits = [
            str(t) for t in (sp.get("traits") or []) if str(t).strip()
        ][:4]
    else:
        sp_label, sp_traits = "", []
    return {
        "char_name": char_name,
        "persona_label": persona_label,
        # 物种 / 身体结构（v2.28.11，只加不改）：社交层主动消息也能提一句
        # 「她是小马」；判不出就是空串/空列表，调用方按「不知道」处理。
        "species": sp_label,
        "body_traits": sp_traits,
    }


def _outfit_block(core: Any) -> dict[str, Any]:
    """她此刻穿着：只导出 `{"enabled","text","source"}` 三个短字段（v2.28.9）。

    `core.outfit_info()` 还带 `kind`（物种识别结果），那是面板用的内部细节；
    跨插件接口只给社交层真正要用的三样，形状固定、体积小。
    拿不到（旧 Core / 没这个方法 / 抛异常 / 返回非 dict）→ `{}`，
    社交层按「不知道她穿什么」处理，不因为这一块让整份契约生成失败。
    """
    try:
        info = core.outfit_info()
    except Exception:
        return {}
    if not isinstance(info, dict) or not info:
        return {}
    try:
        return {
            "enabled": bool(info.get("enabled")),
            "text": str(info.get("text") or "").strip(),
            "source": str(info.get("source") or "").strip(),
        }
    except Exception:
        return {}


def _fields_hash(contract: dict[str, Any]) -> str:
    """结构指纹：排序后的「键路径」的 sha256 前 16 位。"""
    paths: list[str] = []

    def walk(node: Any, prefix: str) -> None:
        if not isinstance(node, dict):
            return
        for key in sorted(node):
            path = f"{prefix}.{key}" if prefix else str(key)
            paths.append(path)
            walk(node[key], path)

    walk(contract, "")
    raw = "\n".join(sorted(paths)).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()[:16]


def _wake_block(core: Any, cfg: Any = None) -> dict[str, Any]:
    """醒来状态：`soma.wake_state()` 的安全导出（拿不到 / 坏数据 → 空 dict）。

    只导出固定六个键并做类型清洗：契约是跨插件接口，对面不该因为 Core 内部换了
    一个字段形状就拿到一个藏着 None/NaN 的字典。

    **身体轴没在跑时不导出**（B5 矩阵 D4）：`soma_enabled=false` 时 `body` 块是
    `available:false / asleep:null`，而 `wake_state()` 会吐出上次开启时留下的
    `asleep:false`——同一份契约里于是有两个「是否睡着」。这里改成不可用块。
    `cfg` 缺省时从 `core.config` 取；两者都没有（老 Core / 单测直接喂 SimpleNamespace）
    时保持旧行为，只做形状清洗。
    """
    if cfg is None:
        cfg = getattr(core, "config", None)
    if cfg is not None and not getattr(cfg, "soma_enabled", True):
        return _contract.unavailable("soma_disabled")
    try:
        state = core.soma.wake_state()
    except Exception:
        return {}
    if not isinstance(state, dict):
        return {}

    def _num(value: Any) -> float:
        try:
            out = float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return out if out == out else 0.0

    return {
        "asleep": bool(state.get("asleep", False)),
        "wake_at": round(_num(state.get("wake_at")), 1),
        "inertia_until": round(_num(state.get("inertia_until")), 1),
        "inertia_minutes_left": max(0, int(_num(state.get("inertia_minutes_left")))),
        "disturbed": bool(state.get("disturbed", False)),
        "last_sleep_hours": round(_num(state.get("last_sleep_hours")), 2),
    }


def _contract_weather(weather: Any) -> str:
    """契约里的天气：拿到真天气才有值，说明书（未取到/过期/未配置）导出空串。

    以 `snapshot()` 打在 `notice` 字段上的标志为准，而不是猜字符串——说明书文案会
    增改，字符串黑名单（`is_notice`）会漏（曾漏过「天气数据过期了」「天气还没取到」，
    被当成真天气导出，社交层照着编）。`is_notice` 仍留作兜底。
    """

    from .services.weather import is_notice

    if not isinstance(weather, dict) or weather.get("notice"):
        return ""
    env = str(weather.get("env", "") or "").strip()
    return "" if not env or is_notice(env) else env


def _contract_holiday(core) -> str:
    """契约里的节日：今天是什么节日就导出什么，没有就是空串。"""
    try:
        return str(core.clock.holiday() or "").strip()
    except Exception:
        return ""


def night_window(core: Any, cfg: Any = None) -> dict:
    """她的夜窗口：**唯一一份推导**，实现在 `state_snapshot.night()` → `clock.night_window()`。

    优先级：日程里推出来的睡段（`soma.scheduled_night()`，老 Core 退回
    `biological_night()`）→ 配置窗口；夜间模式关掉、或窗口长度为 0（`start == end`）
    就是**没有夜**：`span` 为 0、`source` 为 "none"。

    旧实现在这里自己算 `span = end - start if end > start else (24.0 - start) + end`：
    `start == end`（面板上把起止设成同一时刻 = 关掉这个窗口）会算出 **24 小时**，
    于是社交层收到「她一天 24 小时都在夜里」（审核 跨插件契约 7）；`max(1.0, span)`
    又把「没有夜」抬成 1 小时。现在直接用 `cfg.night_span_hours`（它把这两种情况都算成 0）。

    这层薄壳留着只为不改调用方（`api.clock()` 与既有测试都调 `link.night_window`）；
    推导本身只有一份，见 `state_snapshot.night()` 的 docstring（四处答案收敛的那段历史）。
    """
    return _snapshot.night(core, cfg)


def _routine_block(core, cfg, slots, window: Optional[dict] = None) -> dict:
    """作息：导出的是**她实际的**生物钟夜，不是配置里那个写死的窗口。

    社交层读的就是 `night_start_hour` / `night_end_hour` 判断「她这会儿该不该睡」。字段名
    不动、契约版本不动，值改成从她今天的日程推出来的窗口——夜猫子人格凌晨四点睡，社交层
    就该按凌晨四点算，而不是按配置里的 23:00 硬判。日程还没生成或没排睡眠段时才退回配置
    窗口，并用 `night_source` 说清是哪来的（`schedule` / `config` / `none`）。
    """
    if window is None:
        window = night_window(core, cfg)
    return {
        "night_start_hour": round(float(window["start"]) % 24.0, 2),
        "night_end_hour": round(float(window["end"]) % 24.0, 2),
        "night_span_hours": round(float(window["span"]), 2),
        "night_source": window["source"],
        "configured_night": [cfg.night_start_hour, cfg.night_end_hour],
        "sleep_need_hours": cfg.sleep_need_hours,
        "wake_at": schedule_wake_text(slots),
        "sleep_spans": [
            f"{s.get('start')}-{s.get('end')} {s.get('event')}" for s in sleep_spans(slots)
        ],
    }


def signals_path(data_root: str | os.PathLike[str] | None, must_exist: bool = True) -> Path | None:
    if not data_root:
        return None
    path = Path(data_root).joinpath("plugin_data", SOCIAL_PLUGIN_DIR_NAME, SIGNALS_FILE_NAME)
    # must_exist=True（所有旧调用方的口径）：文件不在就当没有这个对接。
    if not must_exist or path.exists():
        return path
    return None


def social_data_dir(data_root: str | os.PathLike[str] | None) -> Path | None:
    """社交层自己的数据目录。拿它区分「没装那个插件」与「装了但还没写过信号」。"""
    if not data_root:
        return None
    return Path(data_root).joinpath("plugin_data", SOCIAL_PLUGIN_DIR_NAME)


class SocialSignals:
    """只读社交层写回的信号。

    v3.0.3（联动 跨插件契约 2）：**「读不到」不再伪装成「没有」**。以前文件不存在、
    过期、坏 JSON 一律返回空字典，调用方分不清「写方说没有了」和「我不知道」——
    两者对 `open_items` 是相反的结论（前者该清账，后者绝不能清）。现在读不到时返回
    一个带标记的块：`{"available": False, "reason": "stale"|"corrupt"|"missing"|
    "no_data_root"}`；读得到时就是写方原本那份字典（没有 `available` 键，与旧版同形）。
    `status()` 与各 accessor 都按这个标记分支。
    """

    def __init__(self, data_root_provider: Callable[[], Any], time_source: Callable[[], float] = time.time) -> None:
        self._root = data_root_provider
        self._time = time_source
        self._fingerprint: tuple[int, int] | None = None
        self._payload: dict[str, Any] = {}
        # 写方在信号文件里声明的保鲜期（秒）；0 表示没声明，用默认值。
        self._declared_ttl = 0.0
        # 这一份文件到底读出来了吗。`read()` 读不到时返回的是空表（或上一次的有效数据），
        # 调用方分不清「我读不到」和「写方说没有了」——`status()` 靠这个字段把它们分开。
        self._unreadable = False

    def _ttl(self) -> float:
        """当前采信的保鲜期。

        写方声明的优先（它知道自己的心跳间隔），夹在 [下限, 上限] 之间；
        没声明就用默认值。原来这里是个写死的 900 秒，与可配的心跳间隔对不上。
        """
        if self._declared_ttl <= 0:
            return SIGNALS_TTL_SECONDS
        return min(
            max(SIGNALS_TTL_MIN_SECONDS, self._declared_ttl), SIGNALS_TTL_MAX_SECONDS
        )

    def _invalidate(self) -> None:
        """丢掉缓存的那一份（文件没了 / 过期了）：清指纹与 payload，**保鲜期留着**。

        只清 `_payload` 不清 `_fingerprint`，文件再出现时指纹没变就直接返回空 payload，
        所以这两样必须一起清。

        `_declared_ttl` 反而**不能**清：它是写方（社交层）的属性，不是这一份 payload 的。
        清了它，`status()` 判过期时会落回默认的 3 小时——而 `status()` 的顺序是先
        `read()`（这一步发现过期、顺手调用了本方法）再拿 `_ttl()` 判，于是刚判出来的
        「过期」当场被改判成「新鲜」，`is_fresh()` 在真假之间逐次跳：社交层明明停了，
        Core 却有一半的时候认为自己读到的是新鲜的。
        写方换版本、换了保鲜期也没关系：`read()` 是**先解析、再判过期**，新 payload
        声明的值在判之前就已经生效了。
        """
        self._fingerprint = None
        self._payload = {}

    def _stale_fallback(self, now: float, mtime: float) -> dict[str, Any]:
        """读坏了 / 超大：**一律不可用**（B5 矩阵 D3）。

        原来保鲜期内会 `return self._payload`——同一份坏文件，`read()` 返回上一次的
        好数据（不带任何标记，消费方看起来与新鲜数据一样），而 `status()` 因为
        `_unreadable` 报 `unreadable`：同一个对象对同一份文件给出两个答案。
        「上一次的事实」不是「这一次的读取结果」，沿用等于把过期值伪装成当前值。

        返回的是**带标记的不可用块**，不是空字典：空字典在 `open_items` 那里等于
        「写方说全结了」，会把她的未完事项整本清空。
        """
        self._invalidate()
        return _contract.unavailable(_contract.REASON_CORRUPT)

    def read(self) -> dict[str, Any]:
        now = self._time()
        try:
            path = signals_path(self._root())
        except Exception:
            # 获取 data_root 失败：这是「不知道」，不是「没有」。
            return _contract.unavailable(_contract.REASON_NO_DATA_ROOT)
        if path is None:
            self._invalidate()
            return _contract.unavailable(_contract.REASON_NO_DATA_ROOT)
        try:
            stat = path.stat()
        except OSError:
            # 文件不存在或无法访问（exists() 与 stat() 之间被删掉也走这里）。
            self._unreadable = True
            return _contract.unavailable(_contract.REASON_MISSING)
        fingerprint = (stat.st_mtime_ns, stat.st_size)
        if fingerprint == self._fingerprint:
            payload = self._payload
        else:
            if stat.st_size > SIGNALS_MAX_BYTES:
                # 读到一半才炸不如根本不读：见 `SIGNALS_MAX_BYTES`。
                self._unreadable = True
                return self._stale_fallback(now, stat.st_mtime)
            try:
                parsed = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                # JSON 解析失败或读取错误：报不可用（B5 矩阵 D3，不再沿用上一次的值）
                self._unreadable = True
                return self._stale_fallback(now, stat.st_mtime)
            except Exception:
                # 其他未预期异常同样报不可用，理由同上
                self._unreadable = True
                return self._stale_fallback(now, stat.st_mtime)
            if not isinstance(parsed, dict):
                self._unreadable = True
                return self._stale_fallback(now, stat.st_mtime)
            self._fingerprint = fingerprint
            self._payload = parsed
            self._unreadable = False
            # 写方声明的保鲜期。第一次读之前用的是默认值（足够宽），读到之后按声明的来。
            try:
                declared = float(parsed.get("ttl_seconds") or 0.0)
            except (TypeError, ValueError):
                declared = 0.0
            self._declared_ttl = declared if declared > 0 else 0.0
            payload = parsed
        if now - stat.st_mtime > self._ttl():
            # 社交层停了：它写的那份「刚主动找过谁」不该继续影响身体。
            #
            # 顺序是**先解析、再判过期**，为的是用「手上这一份」自己声明的保鲜期来判。
            # 原来先判过期、用的是上一次读到的 `_declared_ttl`：一旦过期，
            # `_invalidate()` 会把它清成 0，下一次读就落回默认的 3 小时，于是「过期」
            # 与「新鲜」逐次交替，写方声明的保鲜期实际只在第一次生效——社交层停了以后
            # Core 还会断断续续拿它那份「刚主动找过谁」去改自己的好感。
            self._invalidate()
            return _contract.unavailable(_contract.REASON_STALE)
        return payload

    def _scope_of(self, payload: dict[str, Any], role_id: str = "") -> dict[str, Any]:
        """信号文件里属于**这个角色**的那一份。

        social 那边是这么写的（`signals.py`）：单 Bot 时写顶层，多 Bot 时**只写
        `by_bid[bid]`**——顶层留给跨角色共用的那部分（`ignored_streak`、心跳时间）。

        后果是这里原来只 `payload.get("last_proactive_at")`，在多 Bot 部署下**永远
        读不到**，于是 `note_proactive()` 永远不被调用、`social_desire` 只涨不泄：
        社交层每发一条，Core 那边一点反应都没有，联动单边断。
        """
        if role_id:
            by_bid = payload.get("by_bid")
            if isinstance(by_bid, dict) and by_bid:
                # v3.0.0（联动 P2-7/P2-27）：**分片存在时绝不回落顶层**。
                # 顶层是多 Bot 时跨角色共用的部分（或单 Bot 的整份），回落过去等于把
                # 别人的数据读成本角色的：第二角色会读到第一角色的「刚主动找过谁 /
                # 被冷落几次」，错误泄 social_desire、错误扣好感；open_items 更糟——
                # 顶层恒为 [] 时会把本角色的未完事项账本整体清空。
                # 本角色分片缺失 = 「不知道」，返回空 dict 让调用方降级。
                row = by_bid.get(str(role_id))
                return row if isinstance(row, dict) else {}
        return payload

    def last_proactive(self, role_id: str = "") -> tuple[str, float]:
        """社交层最近一次主动开口：(对象 uid, epoch)。没有则 ("", 0.0)。"""
        row = self._scope_of(self.read(), role_id)
        try:
            at = float(row.get("last_proactive_at", 0.0) or 0.0)
        except (TypeError, ValueError):
            at = 0.0
        target = str(row.get("last_target_uid", "") or "")
        return target, at

    def last_proactive_text(self, role_id: str = "") -> str:
        """社交层最近一次主动开口的**原文**（v2.28.2 起由社交层发布）。

        为什么需要它：主动消息走 `context.send_message` → `platform.send_by_session`，
        不经过 respond 阶段、不产生消息事件，Core 收不到任何事件。
        社交层把原话写进信号文件，Core 在下一轮取信号时就能把这句话补记进
        `mood.spoke_log`，并推进这个人的 `her_said_at`（v3.0.0 起；**不再**推进
        `last_interaction`——那个键的兼容语义是「TA 上次说话」，联动 P2-3）。

        旧版社交插件没有这个字段：缺键 / 坏值一律返回空串，调用方按「只知道她开过口、
        不知道说了什么」降级——不会因为字段缺失把整条信号读坏。
        """
        row = self._scope_of(self.read(), role_id)
        try:
            text = str(row.get("last_proactive_text", "") or "")
        except Exception:
            return ""
        return text[:200]

    def ignored_streak(self, role_id: str = "") -> int:
        """被冷落的角色级计数。传了 role_id 就按角色取——多 Bot 时顶层那个是冻结的。"""
        try:
            data = self.read()
        except Exception:
            return 0
        row = self._scope_of(data, role_id) if role_id else data
        if isinstance(row, dict):
            try:
                return max(0, int(row.get("ignored_streak", 0) or 0))
            except (TypeError, ValueError):
                return 0
        return 0

    def ignored_by_uid(self, uid: str, role_id: str = "") -> int:
        """社交层记的「这个人累计没理我几次」。

        原来只有角色级的 `ignored_streak`——说得清「她最近有点冷清」，说不清是谁。
        而好感是用户级的，没有这个数就没法把被冷落换算成「对 TA 的好感下降」。
        """
        try:
            data = self.read()
        except Exception:
            return 0
        for slot in self._slots(data, role_id):
            if not isinstance(slot, dict):
                continue
            by = slot.get("ignored_by")
            if isinstance(by, dict):
                try:
                    return max(0, int(by.get(str(uid or "")) or 0))
                except (TypeError, ValueError):
                    return 0
        return 0

    def ignored_by_map(self, role_id: str = "") -> dict[str, int]:
        """整个 `ignored_by` 表（uid -> 累计次数），**一次读取**。

        `ignored_by_uid` 是「问一个人」，每问一次都要走一遍 `read()`（一次路径拼装 +
        一次 `stat`）。`_apply_ignored_affection` 按用户全量问一遍，7062 个用户就是
        7062 次 `read()`——实测 1.5 秒，而那段代码跑在**事件循环**上，整台 AstrBot
        陪它一起停：消息不回、LLM 不调，看门狗日志里就是 `Event loop stalled for 30s`。

        这里反过来：读一次，把表整个拿走。调用方仍然按自己的用户集合去取，
        所以「只给本角色认识的人记账」这条没有变。
        """
        try:
            data = self.read()
        except Exception:
            return {}
        out: dict[str, int] = {}
        for slot in self._slots(data, role_id):
            if not isinstance(slot, dict):
                continue
            by = slot.get("ignored_by")
            if isinstance(by, dict):
                for uid, raw in by.items():
                    try:
                        count = int(raw or 0)
                    except (TypeError, ValueError):
                        continue
                    if count > 0:
                        out[str(uid or "")] = count
                # 与 `ignored_by_uid` 同口径：只看第一个带表的槽，后面的不叠。
                return out
        return out

    def open_items(self, role_id: str = "") -> Optional[list[dict[str, Any]]]:
        """社交层发布的「她手上还开着的事」：`[{"text","uid","at","src"}, ...]`。

        这是 Core 唯一能拿到「跨轮未完事项」的通道。会话历史只留十几轮、超了就被压缩，
        压缩之后模型手上只剩用户刚说的那一句，于是每轮都绕着最后一句转；这份列表是
        **不被压缩影响**的那一份索引。

        返回值有**三种**，调用方必须分得开（见 `life_log.merge_open`）：

        * `None` → 文件不存在 / 过期 / 读不到 / 写方根本没写这个键 / **多 Bot 时本角色
          分片缺失**（v3.0.0，联动 P2-27：分片存在就不回落顶层，缺失按「不知道」处理，
          绝不能被顶层的 `[]` 误当成「全结了」把账本清空）。表示「**不知道**」，
          不能据此清掉已有账本；
        * `[]` → 文件新鲜，写方明确写了一个空表。表示「**确实没有了**」；
        * 非空 → 就是还开着的那几条。

        以前这三种全返回空表，于是「写方没写这个键」被当成「全结了」——换成一个还没
        写 `open_items` 的旧版社交层，Core 每轮都会把她的未完事项整本清空一次。
        字段只加不改：认不出的键、认不出的元素一律丢掉，写方换版本也不会把 Core 读崩。
        """
        try:
            data = self.read()
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        row = self._scope_of(data, role_id)
        if not isinstance(row, dict):
            return None
        if "open_items" not in row:
            return None
        return parse_open(row.get("open_items"))

    def _slots(self, data: Any, role_id: str = "") -> list:
        """社交层（signals.py）多 Bot 时写在 `by_bid[bid]` 下，单 Bot 才写顶层。

        **角色分片存在时只用分片，不再回落顶层**：顶层是跨角色共用的那部分
        （`ignored_streak`、心跳时间），回落过去等于把别人的数据读成本角色的——
        多 Bot 同进程时几个 Bot 会读到同一个数，再各自拿它去改自己的好感。
        口径与 `_scope_of` 保持一致。

        注意键名是 `by_bid`，不是 `roles`：以前这里读错键，于是 `ignored_by_uid`
        恒返回 0（而同一文件的 `_scope_of` 读的是 `by_bid`，是对的）。
        """
        if isinstance(data, dict):
            if role_id:
                by_bid = data.get("by_bid")
                if isinstance(by_bid, dict) and by_bid:
                    # v3.0.0（联动 P2-27）：分片存在时不再回落顶层——顶层在多 Bot 时是
                    # 跨角色数据，回落等于把别人的「被冷落几次」读成本角色的。
                    role = by_bid.get(str(role_id))
                    return [role] if isinstance(role, dict) else []
            return [data]
        return []

    def is_fresh(self) -> bool:
        """信号文件现在是不是新鲜的（社交层活着、那份数据没过期）。

        和 `read()` 只差一件事，但很要紧：`read()` 在文件缺失或过期时返回 `{}`，
        调用方分不清「写方说没有了」和「**我读不到**」。删除类的动作只该在前者发生时做——
        `open_items()` 的空表因此必须配着这个方法一起用，不然社交层一停，
        她记着的事就会在无人知晓的情况下被清空。
        """
        try:
            return self.status().get("state") == "fresh"
        except Exception:
            return False

    def status(self) -> dict[str, Any]:
        """对接到底卡在哪一步。只说「没读到」会把三种完全不同的情况当成一种。

        * `not_installed` —— 连社交层的数据目录都没有：那个插件没装（这不是故障）。
        * `never_written` —— 装了，但 `humanoid_signals.json` 还不存在。
        * `stale` —— 文件在，但超过 TTL 没刷新：社交层停了、被禁用，或卡住了。
        * `unreadable` —— 文件在、时间也新鲜，但**读不出内容**（JSON 坏了、编码不对、
          没权限、是个目录、大到不像信号文件）。这一种必须与 `fresh` 分开：两者都会让
          `open_items()` 返回空表，而 `fresh` 的空表意思是「写方说没有了」、
          `unreadable` 的空表意思是「我不知道」——前者会让 Core 把她的账本清空。
        * `fresh` —— 读到并使用中。
        每种都带上它去看的那个路径，排查时不用再猜目录名。
        """
        now = self._time()
        try:
            root = self._root()
        except Exception:
            root = None
        path = signals_path(root, must_exist=False)
        if path is None:
            return {"state": "no_data_root", "path": "", "age": None, "payload": {}}
        if not path.exists():
            directory = social_data_dir(root)
            state = "never_written" if (directory and directory.exists()) else "not_installed"
            return {"state": state, "path": str(path), "age": None, "payload": {}}
        payload = self.read()
        try:
            age = max(0.0, now - path.stat().st_mtime)
        except OSError:
            age = None
        # v3.0.3：判据从「payload 空不空」改成读 `read()` 带回来的标记。空字典不再
        # 是「读不到」的同义词——它是写方明确写的空表（fresh 的空表）。
        reason = _contract.unavailable_reason(payload)
        if reason in (_contract.REASON_CORRUPT, _contract.REASON_UNREADABLE) or self._unreadable:
            return {"state": "unreadable", "path": str(path), "age": age, "payload": {}}
        if reason == _contract.REASON_STALE:
            return {"state": "stale", "path": str(path), "age": age, "payload": {}}
        if reason == _contract.REASON_MISSING:
            return {"state": "never_written", "path": str(path), "age": age, "payload": {}}
        if reason == _contract.REASON_NO_DATA_ROOT:
            return {"state": "no_data_root", "path": "", "age": None, "payload": {}}
        return {"state": "fresh", "path": str(path), "age": age, "payload": payload}
