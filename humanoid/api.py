"""跨插件只读门面（契约 v2 的主路径）。

## 为什么要有它

社交层（`astrbot_plugin_autonomous_social`）现在只能读 Core 写盘的 `state.json`
快照。那条路有三个治不好的毛病：

1. **会旧**：快照按落盘节奏更新，而 `time` 块为了不让契约变成常驻写盘源、刻意不参与
   重写判定（见 `core_instance.refresh_contract` 的注释）——于是时钟类信息可以静止很久；
2. **形状靠猜**：`feelings` 写的是 `list[str]`，读的一方按 `list[dict]` 解析，两边都
   不报错，只是那条由头永远不产出（v1.27.39 才修掉）；
3. **各拼一套上下文**：普通回复用 Core 的「框架句 + 此刻的她 + 事实块」，主动消息只有
   社交层自己拼的 `compact()` 摘要——同一个角色，两处拿到两套关于自己的事实。

门面把这三件事一起解决：按需现算、结构化返回、框架句只有一份。

## 约束（改这个文件前先读）

* **只读**：不许写 state、不许 mark_dirty、不许推进身体。调用方可能是每轮消息都来问。
* **不抛异常**：任何失败都退化成 `None` / `[]` / `{}`。跨插件接口抛出去，会把对方的
  消息链路一起带崩，而对方没法区分「Core 坏了」和「我自己写错了」。
* **不猜**：拿不到就是拿不到，不编默认值（`energy_text` 拿不到就是空串，不是「精神不错」）。
* **字段只加不改**：`API_VERSION` 只在**破坏性**变化时才动；加字段不动它。
"""

from __future__ import annotations

import hashlib
import math
from typing import Any, Dict, List, Optional

API_VERSION = 2

#: 门面能力清单。社交层按这个判断「要不要走门面」，不靠版本号猜。
CAPABILITIES = ("brief", "clock", "events", "fingerprint")


def _s(value: Any) -> str:
    return str(value or "").strip()


def _num(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _item_id(prefix: str, text: str) -> str:
    """稳定 id：同一件事跨轮、跨进程都是同一个 id。

    社交层要用它做「这条由头用过了」的记账。以前只能拿文本比，于是「嗯嗯」这种
    两个字的回复和另一句「嗯嗯」分不开——id 让「是不是同一件事」有确定答案。
    """
    raw = _s(text).encode("utf-8")
    return f"{prefix}:" + hashlib.blake2b(raw, digest_size=6).hexdigest()


class CoreFacade:
    """某个角色的只读视图。由 `main.get_core_facade()` 交给别的插件。"""

    def __init__(self, roles: Any, logger: Any = None) -> None:
        self._roles = roles
        self._log = logger

    # ─── 元信息 ─────────────────────────────────────────

    def api_version(self) -> int:
        return API_VERSION

    def capabilities(self) -> List[str]:
        return list(CAPABILITIES)

    def role_ids(self) -> List[str]:
        try:
            return [str(inst.role_id) for inst in self._roles.get_all()]
        except Exception:
            return []

    # ─── 取实例 ─────────────────────────────────────────

    def _core(self, role_id: str) -> Optional[Any]:
        """**只查已有实例，不新建**。

        门面是只读的：`RoleManager.get_or_create()` 会建角色、起后台循环、写一条日志。
        社交层拿一个错的 role_id 来问一次，不该因此在 Core 里凭空多出一个角色。
        """
        try:
            for inst in self._roles.get_all():
                if str(getattr(inst, "role_id", "")) == str(role_id):
                    return inst
        except Exception:
            return None
        return None

    # ─── 时钟 ───────────────────────────────────────────

    def clock(self, role_id: str) -> Dict[str, Any]:
        """她所在城市的「现在」。拿不到返回 `{}`（调用方退回自己的钟）。

        这是门面最直接的价值：契约里的 `time` 块不参与重写判定，读文件拿到的偏移
        可能静止几小时；这里每次都是现算的。
        """
        core = self._core(role_id)
        if core is None:
            return {}
        try:
            now = core.clock.now()
            zone = core.clock.zone_state()
            out = {
                "epoch": float(core.now_epoch()),
                "utc_offset_minutes": int(getattr(zone, "offset_minutes", 0) or 0),
                "city": _s(getattr(zone, "city", "")),
                "hour": int(now.hour),
                "minute": int(now.minute),
                "weekday": int(now.weekday()),
                "is_night": bool(core.clock.is_night(now)),
                "is_deep_sleep": bool(core.clock.is_deep_sleep(now)),
                "date": now.strftime("%Y-%m-%d"),
            }
        except Exception as exc:
            self._debug(f"clock 失败: {exc}")
            return {}
        # 她实际的作息夜：安静时段该按这个，而不是配置里写死的 23-7
        try:
            routine = (core.scope.get_self("contract") or {}).get("routine") or {}
            start, end = routine.get("night_start_hour"), routine.get("night_end_hour")
            if start is not None and end is not None:
                out["night_window"] = [float(start) % 24.0, float(end) % 24.0]
        except Exception:
            pass
        return out

    # ─── 情境摘要 ───────────────────────────────────────

    def brief(
        self,
        role_id: str,
        user_id: str = "",
        *,
        task: str = "chat",
        text: str = "",
        char_name: str = "",
        user_name: str = "",
    ) -> Optional[Dict[str, Any]]:
        """按**任务**给一份结构化情境。拿不到返回 `None`。

        `task` 决定信息范围（任务书 4.4 的「任务相关摘要」，全项目只在这里实现一次）：

        * `chat`：框架句 + 此刻的她 + 未完成事项（普通回复用）
        * `proactive`：框架句 + 此刻的她 + 未完成事项 + 近期事件（主动消息用）
        * `status`：全量（`/你的状态` 用）

        `text` 是用户刚说的那句话。这一版**不拿它做筛选**（筛选是 M17 的活，
        走 `humanoid/relevance.py`），但参数先留着，免得以后改签名。
        """
        core = self._core(role_id)
        if core is None:
            return None
        task = _s(task) or "chat"
        try:
            contract = core.scope.get_self("contract") or {}
            if not isinstance(contract, dict):
                contract = {}
            body = contract.get("body") if isinstance(contract.get("body"), dict) else {}
            day = contract.get("day") if isinstance(contract.get("day"), dict) else {}

            out: Dict[str, Any] = {
                "v": API_VERSION,
                "task": task,
                "role_id": str(role_id),
                "generated_at": float(core.now_epoch()),
                "persona_framing": self._framing(core, char_name, user_name),
                "self_state": self._self_state(core, contract, body),
                "day": {
                    "doing": _s(day.get("doing")),
                    "done": [_s(x) for x in (day.get("done") or []) if _s(x)],
                    "next": [_s(x) for x in (day.get("next") or []) if _s(x)],
                    "wants": [_s(x) for x in (contract.get("wants") or []) if _s(x)],
                },
                "ongoing": self._ongoing(core),
            }
            if task in ("proactive", "status"):
                out["events"] = self._events(core, limit=5)
            if task == "status":
                out["relationship"] = self._relationship(core, user_id)
            out["fingerprint"] = self._fingerprint(core, out)
            return out
        except Exception as exc:
            self._debug(f"brief 失败: {exc}")
            return None

    def events(self, role_id: str, user_id: str = "", *, limit: int = 5,
               since: float = 0.0) -> List[Dict[str, Any]]:
        core = self._core(role_id)
        if core is None:
            return []
        try:
            return self._events(core, limit=limit, since=since)
        except Exception as exc:
            self._debug(f"events 失败: {exc}")
            return []

    def state_fingerprint(self, role_id: str) -> str:
        core = self._core(role_id)
        if core is None:
            return ""
        try:
            return self._fingerprint(core, {
                "self_state": self._self_state(core, {}, {}),
                "ongoing": self._ongoing(core),
            })
        except Exception:
            return ""

    # ─── 内部拼装（全部 try 兜住）───────────────────────

    def _framing(self, core: Any, char_name: str, user_name: str) -> str:
        """与普通回复**同一句**框架句。人格一致性从「两边写得像」变成结构保证。"""
        try:
            from .prompt_builder import build_framing

            return build_framing(char_name or core.char_name(), user_name)
        except Exception:
            return ""

    def _self_state(self, core: Any, contract: Dict[str, Any],
                    body: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "energy_text": _s(body.get("energy_text")),
            "asleep": bool(body.get("asleep")),
            "feelings": [],
        }
        for key in ("energy", "social_energy", "sleep_pressure", "hunger", "discomfort"):
            if body.get(key) is not None:
                out[key] = _num(body.get(key))
        # 契约里 `feelings` 是 `list[str]`（按显著度取前几条）。这里带上 id 与显著度，
        # 让「这一条用过没有」可判定；显著度按原始顺序递减，是**近似**，不是 Core 内部
        # 那个真实分数（那个没有导出）。
        raw = contract.get("feelings")
        if isinstance(raw, list):
            total = max(1, len(raw))
            for idx, item in enumerate(raw):
                text = _s(item if isinstance(item, str) else (item or {}).get("text", ""))
                if not text:
                    continue
                out["feelings"].append({
                    "id": _item_id("f", text),
                    "text": text,
                    "kind": "body",
                    "salience": round(1.0 - idx / (total + 1), 2),
                })
        return out

    def _ongoing(self, core: Any) -> List[Dict[str, Any]]:
        """她手上还开着的事：日程侧（`ongoing_tasks`）+ 社交侧发布（`life_log`）。

        以前这两份分别进不同上下文（一份进「此刻的她」、一份进事实块），而主动消息
        一份都拿不到。现在统一成一张带 id 的表。
        """
        out: List[Dict[str, Any]] = []
        seen: set = set()
        try:
            for text in core.schedule.ongoing_tasks() or []:
                text = _s(text)
                if not text or text in seen:
                    continue
                seen.add(text)
                out.append({"id": _item_id("og", text), "text": text,
                            "at": 0.0, "ttl": 0.0, "src": "schedule"})
        except Exception:
            pass
        try:
            for entry in core.life_items() or []:
                if not isinstance(entry, dict):
                    continue
                text = _s(entry.get("text"))
                if not text or text in seen:
                    continue
                seen.add(text)
                out.append({
                    "id": _item_id("ll", text),
                    "text": text,
                    "at": _num(entry.get("at")),
                    "ttl": max(0.0, _num(entry.get("until")) - _num(entry.get("at"))),
                    "src": "social",
                })
        except Exception:
            pass
        return out[:6]

    def _events(self, core: Any, *, limit: int = 5, since: float = 0.0) -> List[Dict[str, Any]]:
        """近期事件。这一版只有「还没结束的事」这一种来源（`life_log`）。

        Core 的账本目前**没有 `kind` 字段**，也没有任何一处写「做过」条目——
        所以这里如实标成 `open`，不假装有「刚做完 X」。（社交层那条永远不命中的
        「刚做完」派生在 v1.27.39 已删除。）
        """
        out: List[Dict[str, Any]] = []
        try:
            for entry in core.life_items() or []:
                if not isinstance(entry, dict):
                    continue
                text = _s(entry.get("text"))
                at = _num(entry.get("at"))
                if not text or (since > 0 and at > 0 and at < since):
                    continue
                out.append({
                    "id": _item_id("ev", text),
                    "kind": "open",
                    "text": text,
                    "at": at,
                    "ttl": max(0.0, _num(entry.get("until")) - at),
                })
        except Exception:
            pass
        out.sort(key=lambda e: -float(e.get("at") or 0.0))
        return out[: max(0, int(limit))]

    def _relationship(self, core: Any, user_id: str) -> Dict[str, Any]:
        if not user_id:
            return {}
        try:
            profile = core.mood.profile(user_id) or {}
            user_state = core.scope.user_state(user_id) or {}
        except Exception:
            return {}
        return {
            "affection": _num(profile.get("affection")),
            "base_affection": _num(profile.get("base_affection")),
            "nickname": _s(user_state.get("nickname")),
            "last_interaction": _num(user_state.get("last_interaction")),
        }

    def _fingerprint(self, core: Any, payload: Dict[str, Any]) -> str:
        """状态指纹：状态没变，社交层的缓存就不该失效。"""
        try:
            parts = [
                _s(payload.get("self_state", {}).get("energy_text")),
                _s(payload.get("day", {}).get("doing")),
                "|".join(_s(x) for x in payload.get("day", {}).get("done", []) or []),
                "|".join(_s(x) for x in payload.get("day", {}).get("next", []) or []),
                "|".join(_s(i.get("text")) for i in payload.get("ongoing", []) or []),
                str(int(core.now_epoch()) // 60),
            ]
            raw = "\n".join(parts).encode("utf-8")
            return hashlib.blake2b(raw, digest_size=8).hexdigest()
        except Exception:
            return ""

    def _debug(self, message: str) -> None:
        if self._log is None:
            return
        try:
            self._log.debug(f"[humanoid_core] 门面: {message}")
        except Exception:
            pass


def get_core_facade(roles: Any = None, logger: Any = None) -> Optional[CoreFacade]:
    """给别的插件拿门面。宿主按 `star_cls.__module__` 找过来，这里必须可调用。

    没有角色管理器（插件还没起来）时返回 None——对方会退回读 state.json。
    """
    if roles is None:
        return None
    return CoreFacade(roles, logger)
