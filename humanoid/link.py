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

from .life_log import parse_open
from .services.schedule import (
    day_phrases,
    schedule_wake_text,
    sleep_spans,
)

CONTRACT_VERSION = 2
#: 结构标识。社交层拿它和 `fields_hash` 一起做一致性校验（只告警 + 降级，不抛错）。
CONTRACT_SCHEMA = "humanoid.contract/2"
#: 这一版契约提供的能力。社交层按能力判断走哪条路，不靠版本号猜。
CONTRACT_CAPABILITIES = ("body", "feelings", "form", "activity", "day", "ongoing", "routine")

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
    offset = now.utcoffset()
    zone = getattr(now.tzinfo, "key", "") or ""
    contract = {
        "v": CONTRACT_VERSION,
        "core_version": getattr(core, "version", "") or "",
        "generated_at": round(now.timestamp(), 1),
        "time": {
            "local": now.strftime("%Y-%m-%d %H:%M"),
            "hour": now.hour,
            "weekday": snap.get("weekday", ""),
            "city": snap.get("city", ""),
            "is_night": bool(
                cfg.night_mode_enabled
                and core.soma.in_scheduled_night(now.hour + now.minute / 60.0)
            ),
            "is_deep_sleep": bool(
                cfg.night_mode_enabled
                and core.soma.in_scheduled_deep_sleep(now.hour + now.minute / 60.0)
            ),
            # 社交层跟 Core 跑在同一台机器上，但它自己的时段判断用的是本机时钟。
            # 给出偏移量比给出一个时刻更可靠：拿它把 epoch 换算成她的本地小时即可。
            # 算不出来时必须是 None 而不是 0：当成 0 等于把她的城市当 UTC，错得静悄悄。
            "utc_offset_minutes": int(offset.total_seconds() // 60) if offset is not None else None,
            # 生效的 IANA 名；空串意味着她其实没拿到真时区（缺 tzdata 或城市认不出）。
            "tz": zone,
        },
        "body": {
            "energy": round(float(snap["energy"]["value"]), 1),
            "energy_text": snap["energy"]["text"],
            "sleep_pressure": soma.get("sleep_pressure"),
            "sleep_debt": soma.get("sleep_debt"),
            "hunger": soma.get("hunger"),
            "discomfort": soma.get("discomfort"),
            "arousal": soma.get("arousal"),
            "asleep": bool(soma.get("asleep")),
            "last_sleep_hours": soma.get("last_sleep_hours"),
            "social_energy": round(float(snap["social_energy"]["value"]), 1),
            "social_desire": soma.get("social_desire"),
            "cycle_day": int(core.energy.cycle_day),
            "cycle_phase": (snap.get("cycle") or "").strip(),
        },
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
        "day": {
            "doing": str(day.get("doing") or ""),
            "done": [str(x) for x in (day.get("done") or []) if str(x).strip()],
            "next": [str(x) for x in (day.get("next") or []) if str(x).strip()],
        },
        # 日程是按哪个 AstrBot 人设排的：社交层靠它确认两边用的是同一个人。
        "persona": str(core.schedule.status().get("persona", "") or ""),
        # 作息：社交层靠它知道她睡够没睡够，诊断靠它提示两套时间不一致。
        "routine": _routine_block(core, cfg, slots),
        # 天气只递真天气：没配 Key 时那句「为什么没有」是给她主人看的，社交层拿到只会照着说。
        "weather": _contract_weather(snap.get("weather")),
        # 今天是什么节日（没有则空串）。社交层拿它派「节日」由头/破冰话题——
        # 节日是可预测的客观事实，不该让模型现编。
        "holiday": _contract_holiday(core),
        # 契约里没有按用户的数据（1000 人时会把文件撑大）；这些路径是稳定承诺。
        "paths": {
            "user_affection": "roles.<bid>.users.<uid>.mood.affection",
            "user_nickname": "roles.<bid>.users.<uid>.nickname",
            "user_last_interaction": "roles.<bid>.users.<uid>.last_interaction",
            "user_said": "roles.<bid>.users.<uid>.said",
        },
    }
    # ─── 握手三键（契约 v2）：只加不改 ───────────────────
    #
    # 社交层以前只能靠 `v >= 1` 猜「对面是不是新版本」，字段对不上时静默拿到 None。
    # 这三键把「猜」变成「校验」：结构不一致能留一条能搜到的日志，而不是行为悄悄变了。
    #
    # `fields_hash` 只覆盖**结构**（键名），不含取值——取值随时在变，那是 `generated_at`
    # 的活。所以它稳定、可缓存，只有真的增删字段时才会变。
    contract["schema"] = CONTRACT_SCHEMA
    contract["capabilities"] = list(CONTRACT_CAPABILITIES)
    contract["fields_hash"] = _fields_hash(contract)
    return contract


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


def _routine_block(core, cfg, slots) -> dict:
    """作息：导出的是**她实际的**生物钟夜，不是配置里那个写死的窗口。

    社交层读的就是 `night_start_hour` / `night_end_hour` 判断「她这会儿该不该睡」。字段名
    不动、契约版本不动，值改成从她今天的日程推出来的窗口（`soma.biological_night()`）——
    夜猫子人格凌晨四点睡，社交层就该按凌晨四点算，而不是按配置里的 23:00 硬判。
    日程还没生成或没排睡眠段时才退回配置窗口，并用 `night_source` 说清是哪来的。
    """
    night = None
    try:
        candidate = core.soma.biological_night()
    except Exception:
        candidate = None
    if candidate:
        # `(0.0, 0.0)` 这种退化返回值以前会被当成有效的夜：start = end = 0，
        # 跨度算出 24 小时，于是社交层收到「她一天 24 小时都在夜里」。
        # 跨度不足半小时就退回配置窗口（`night_source` 也会如实写 config）。
        try:
            start_h, end_h = float(candidate[0]), float(candidate[1])
        except (TypeError, ValueError, IndexError):
            start_h, end_h = 0.0, 0.0
        if (end_h - start_h) % 24.0 > 0.5:
            night = (start_h, end_h)
    source = "schedule" if night else "config"
    start, end = night if night else (float(cfg.night_start_hour), float(cfg.night_end_hour))
    span = end - start if end > start else (24.0 - start) + end
    return {
        "night_start_hour": round(start % 24.0, 2),
        "night_end_hour": round(end % 24.0, 2),
        "night_span_hours": round(max(1.0, span), 2),
        "night_source": source,
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
    """只读社交层写回的信号。文件不存在或过期时一切返回空。"""

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

    def read(self) -> dict[str, Any]:
        now = self._time()
        try:
            path = signals_path(self._root())
        except Exception:
            # 获取 data_root 失败时返回空，避免抛出异常中断调用方
            return {}
        if path is None:
            self._invalidate()
            return {}
        try:
            stat = path.stat()
        except OSError:
            # 文件不存在或无法访问时返回空
            self._unreadable = True
            return {}
        fingerprint = (stat.st_mtime_ns, stat.st_size)
        if fingerprint == self._fingerprint:
            payload = self._payload
        else:
            if stat.st_size > SIGNALS_MAX_BYTES:
                # 读到一半才炸不如根本不读：见 `SIGNALS_MAX_BYTES`。
                self._unreadable = True
                return self._payload or {}
            try:
                parsed = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                # JSON 解析失败或读取错误时保持上一次的有效数据
                self._unreadable = True
                return self._payload or {}
            except Exception:
                # 其他未预期异常也返回上一次的有效数据
                self._unreadable = True
                return self._payload or {}
            if not isinstance(parsed, dict):
                self._unreadable = True
                return self._payload or {}
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
            return {}
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
            if isinstance(by_bid, dict):
                row = by_bid.get(str(role_id))
                if isinstance(row, dict):
                    return row
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

        * `None` → 文件不存在 / 过期 / 读不到 / 写方根本没写这个键。表示「**不知道**」，
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
                if isinstance(by_bid, dict):
                    role = by_bid.get(str(role_id))
                    if isinstance(role, dict):
                        return [role]
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
        if self._unreadable:
            return {"state": "unreadable", "path": str(path), "age": age, "payload": {}}
        if not payload and age is not None and age > self._ttl():
            return {"state": "stale", "path": str(path), "age": age, "payload": {}}
        return {"state": "fresh", "path": str(path), "age": age, "payload": payload}
