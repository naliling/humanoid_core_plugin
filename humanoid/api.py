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

## API_VERSION 3（v3.0.3）

v2 有一条自相矛盾的承诺：`不猜`，但 `self_state.asleep` 恒为 `bool`、数值恒为数字——
「不知道」被压成了 `false` / `0` / `50`。v3 起这些位置可以是 `null`：

* `self_state.available` / `self_state.reason`：这一块可不可用、为什么（`soma_disabled`）；
* `self_state.asleep`：`true` / `false` / `null`（`null` = 不知道，**不是**醒着）；
* `wake.available` / `wake.source` / `wake.stale`：醒没醒这一项从哪来、是不是旧快照；
* `intimacy.status`：`ok` / `denied`（白名单拒绝，原来与「拿不到」同为 `{}`）；
* `user.known` / `relationship.known`：这个用户到底有没有档案（`false` 时那些数字是
  配置初始值，不是关于他的事实）。

`null` 对按算术读的旧消费方是破坏性的，所以 `API_VERSION` 抬到 3；旧消费方按
`API_VERSION < 3` 可以继续按「数字都是真的」读。
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from typing import Any, Dict, List, Optional

from . import contract as _contract
from . import link as _link
from .state_snapshot import aggression_default as _aggression_default

API_VERSION = 3

#: 门面能力清单。社交层按这个判断「要不要走门面」，不靠版本号猜。
#: 只加不改：`dreams` / `wake` 是 v2.28.0 新增的只读能力，API_VERSION 保持 2。
#: `intimacy`（v2.28.6）补的是 CHANGELOG v2.27.13 声称「契约快照多导出亲密次数/
#: 上次时间/上头值」但实际从未实现的那一条——契约仍然**不带** per-user 数据
#: （1000 人会把 state.json 撑大），改由门面按需现取。
#: v2.28.9 再补两个只读能力（API_VERSION 仍保持 2，只加不改）：
#: `identity`（她是谁：参照名/人设标签）与 `user`（按人一份的情境：称呼/名片名/
#: 情绪/注意力/说过的话/亲密档案）。社交层主动开口缺的就是这两块原料。
#: v3.0.3 起 `self_state` / `wake` / `intimacy` / `user` 里的「不知道」用 `null` +
#: 块级 `available:false` 表达，不再压成 0/false/{}。**三态不单列为一条 capability**：
#: 这个元组的语义是「每个名字要么是门面方法、要么有对应的 brief 块」（测试逐条断言），
#: 三态是读法而不是接口。判「对面懂不懂三态」请看 `API_VERSION >= 3`。
CAPABILITIES = (
    "brief", "clock", "events", "fingerprint", "dreams", "wake", "intimacy",
    "identity", "user",
)


def _s(value: Any) -> str:
    return str(value or "").strip()


def _num(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _item_id(prefix: str, text: str, at: float = 0.0) -> str:
    """稳定 id：同一件事跨轮、跨进程都是同一个 id。

    社交层要用它做「这条由头用过了」的记账。以前只能拿文本比，于是「嗯嗯」这种
    两个字的回复和另一句「嗯嗯」分不开——id 让「是不是同一件事」有确定答案。

    `at` 是**这件事发生/被观察到的时刻**（epoch 秒）。只按文本算 id 时，同一句话
    发生第二次会拿到同一个 id，社交层就以为「这条用过了」而永远不再用它——「她又
    被同一件事逗笑了」这种重复发生的事只能当由头一次（审核 跨插件契约 14）。给了
    时刻就把它拼进摘要；拿不到时刻（0.0）仍只按文本，保持稳定。
    """
    raw = _s(text).encode("utf-8")
    if at and at > 0:
        raw = f"{int(at)}|".encode("utf-8") + raw
    return f"{prefix}:" + hashlib.blake2b(raw, digest_size=6).hexdigest()


class CoreFacade:
    """某个角色的只读视图。由 `main.get_core_facade()` 交给别的插件。

    **实例代次（P1-B）**：门面绑定创建它的那个插件实例代次。插件重载/卸载后，旧的
    门面对象可能还被别的插件缓存着（社交层就缓存它）；它再被调用时必须立刻失效，
    返回 `None`/`{}`/`[]`，不能让对方继续对着已经 `stop()` 的 RoleManager 读数据。
    校验通过 `is_current` 回调在**每次调用**时做，不是构造时判一次。
    """

    def __init__(self, roles: Any, logger: Any = None, *, generation: int = 0,
                 is_current: Any = None) -> None:
        self._roles = roles
        self._log = logger
        self._generation = int(generation or 0)
        #: 返回 bool 的回调：创建它的插件实例是否仍是当前登记的实例。
        #: None = 不绑定（单测直接构造、老调用方），退化为旧行为。
        self._is_current = is_current

    def _alive(self) -> bool:
        """这个门面背后的插件实例是否还是当前实例。没有绑定校验时一律视为有效。"""
        checker = self._is_current
        if checker is None:
            return True
        try:
            return bool(checker())
        except Exception:
            return False

    def is_current(self) -> bool:
        """公开的存活探测：别的插件可以用它在缓存失效后重建门面（P1-B）。"""
        return self._alive()

    # ─── 元信息 ─────────────────────────────────────────

    def api_version(self) -> Optional[int]:
        """协议版本。门面已失效时返回 None：让对方能看出「这个门面不能用了」。"""
        if not self._alive():
            return None
        return API_VERSION

    def capabilities(self) -> List[str]:
        if not self._alive():
            return []
        return list(CAPABILITIES)

    def role_ids(self) -> List[str]:
        if not self._alive():
            return []
        try:
            return [str(inst.role_id) for inst in self._roles.get_all()]
        except Exception:
            return []

    # ─── 取实例 ─────────────────────────────────────────

    def _core(self, role_id: str) -> Optional[Any]:
        """**只查已有实例，不新建**。

        门面是只读的：`RoleManager.get_or_create()` 会建角色、起后台循环、写一条日志。
        社交层拿一个错的 role_id 来问一次，不该因此在 Core 里凭空多出一个角色。

        插件实例被重载/卸载后，旧门面在这里返回 None：所有依赖 `_core` 的方法
        自动退化成 None/空 dict/空 list（P1-B）。
        """
        if not self._alive():
            return None
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

        偏移带着「它是谁的」：`offset_source` 为 `zone`（她的城市）/ `host`（这台
        机器，城市没解析出来）/ `unknown`（naive 时钟，此时 `utc_offset_minutes`
        是 `None`）；`note` 原样带出退化原因（例如「认不出城市」）。**调用方必须
        按 `None` 处理**，不要再 `int(x or 0)`——那等于把她的城市当成 UTC。
        """
        core = self._core(role_id)
        if core is None:
            return {}
        try:
            now = core.clock.now()
            # 退化原因只是「附加信息」：`zone_state()` 自己抛异常时不该把整块时钟一起丢掉
            # （旧实现它在同一个 try 里，一抛 `clock()` 就返回 `{}`，调用方连 epoch 都拿不到）。
            try:
                zone = core.clock.zone_state()
            except Exception:
                zone = None
            # 偏移与「它是谁的」只有一份推导（`link.utc_offset`）：契约 time 块读同一个函数。
            # 旧代码 `int(getattr(zone, "offset_minutes", 0) or 0)` 把「不知道偏移」压成 0——
            # 门面承诺「拿不到就是拿不到」，这里却把她的城市当 UTC（审核 跨插件契约 6）。
            offset_info = _link.utc_offset(core, now)
            # 「她实际的夜」只有一份推导（`link.night_window`）：`is_night` / `is_deep_sleep`
            # 与 `night_window` 读同一个窗口。以前 `is_night` 走 `clock.is_night`（配置窗口
            # 23:00-7:00），`night_window` 走落盘的契约（日程推出来的 4:00-11:30），同一份
            # 返回里两个答案（审核 跨插件契约 8）；而且契约快照最多旧 60 秒、被改坏就跟着错。
            window = _link.night_window(core, getattr(core, "config", None))
            hour_of_day = now.hour + now.minute / 60.0
            out = {
                "epoch": float(core.now_epoch()),
                # None = 不知道（naive 时钟），不是 0：调用方必须按「不知道」处理。
                "utc_offset_minutes": offset_info["minutes"],
                # `zone`（她的城市）/ `host`（这台机器）/ `unknown`：与契约 time 块同一份推导。
                "offset_source": offset_info["source"],
                # 退化原因（城市认不出 / 缺 tzdata）：以前 `clock()` 把它整个丢掉，调用方只看到
                # 一个偏移数字，没法知道它其实是宿主机偏移（审核 跨插件契约 6）。
                "note": _s(getattr(zone, "note", "")),
                "city": _s(getattr(zone, "city", "")),
                "hour": int(now.hour),
                "minute": int(now.minute),
                "weekday": int(now.weekday()),
                "is_night": _link.in_night_window(hour_of_day, window),
                "is_deep_sleep": _link.in_deep_window(
                    hour_of_day, window,
                    getattr(getattr(core, "config", None), "night_deep_sleep_ratio", 0.6),
                ),
                "date": now.strftime("%Y-%m-%d"),
                # 她实际的作息夜（现算）：安静时段该按这个，而不是配置里写死的 23-7。
                # `night_source` 说清这段是哪来的：`schedule`（她日程里的睡段）/
                # `config`（还没生成日程，退回配置窗口）/ `none`（夜间模式关掉，或窗口长度为 0）。
                "night_window": [
                    round(float(window["start"]) % 24.0, 2),
                    round(float(window["end"]) % 24.0, 2),
                ],
                "night_source": window["source"],
            }
        except Exception as exc:
            self._debug(f"clock 失败: {exc}")
            return {}
        return out

    # ─── 情境摘要 ───────────────────────────────────────

    def _contract_body(self, core: Any) -> Any:
        """取落盘契约与它的 `body` 块；**坏形状转成不可用块**（B5 矩阵 D1/D2）。

        原来 `contract.get("body") if isinstance(..., dict) else {}`：缺块、坏块、
        空块一律变成 `{}`，而 `_self_state` 对 `{}` 判 `available: true`——「契约不存在」
        与「身体一切正常」同形。`_wake` 也因此绕过了自己 docstring 里「身体轴没在跑就
        不读 `wake_state()`」的承诺，读出一个上次开启时留下的 live 值。

        返回 `(contract, body)`：契约本身缺失 → body 是 `missing`；契约在但 body
        缺失/不是字典/是空字典 → `corrupt`（有东西，但读不出身体块）。
        """
        raw = core.scope.get_self("contract")
        contract = raw if isinstance(raw, dict) else {}
        if not contract:
            return {}, _contract.as_block(None)
        body = contract.get("body")
        if isinstance(body, dict) and body:
            return contract, body
        return contract, _contract.as_block(body)

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

        v2.28.9（只加不改）起**所有 task** 都另外带 `her`（她是谁）/ `outfit`（穿着）
        / `weather` / `holiday` / `routine`；给了 `user_id` 时再带按人一份的 `user`
        块（称呼回退链、情绪、注意力、TA 说过的话、亲密档案）。社交层的主动消息
        以前拿不到这些，于是叫不出名字、也说不出任何关于对方的事。

        `text` 是用户刚说的那句话。这一版**不拿它做筛选**（筛选是 M17 的活，
        走 `humanoid/relevance.py`），但参数先留着，免得以后改签名。
        """
        core = self._core(role_id)
        if core is None:
            return None
        task = _s(task) or "chat"
        try:
            contract, body = self._contract_body(core)
            day = contract.get("day") if isinstance(contract.get("day"), dict) else {}

            # 醒来状态先算一次：`self_state.asleep` 要复用它，否则同一份 payload 里
            # 会出现两个互相矛盾的「是否睡着」（联动 模型与导出 10）。
            wake = self._wake(core, contract, body)
            out: Dict[str, Any] = {
                "v": API_VERSION,
                "task": task,
                "role_id": str(role_id),
                "generated_at": float(core.now_epoch()),
                # 她是谁（v2.28.9，只加不改）：社交层主动开口前要知道用哪个名字/哪副人设。
                # 现算优先、契约兜底；两处都拿不到就是空串，绝不编。
                "her": self._her(core, contract),
                # 她此刻穿着（v2.28.9）：拿不到给空 dict，社交层按「不知道」处理。
                "outfit": self._outfit(core, contract),
                # 外部事实：契约里那份（天气/节日/作息不挂在身体推进上，过期风险低）。
                "weather": _s(contract.get("weather")),
                "holiday": _s(contract.get("holiday")),
                "routine": self._routine(contract),
                "persona_framing": self._framing(core, char_name, user_name),
                "self_state": self._self_state(core, contract, body, wake),
                "day": {
                    "doing": _s(day.get("doing")),
                    "done": [_s(x) for x in (day.get("done") or []) if _s(x)],
                    "next": [_s(x) for x in (day.get("next") or []) if _s(x)],
                    "wants": [_s(x) for x in (contract.get("wants") or []) if _s(x)],
                    # 今天最近一段睡眠的梦（最多 3 条）：旧契约没有这个键 → 空列表。
                    "dreams": [_s(x) for x in (day.get("dreams") or []) if _s(x)][:3],
                },
                # 醒来状态：优先现算（门面的意义就是「不过期」），拿不到再退回契约里那份；
                # 与 self_state.asleep 同一来源，且带 available/source/stale。
                "wake": wake,
                "ongoing": self._ongoing(core),
            }
            if task in ("proactive", "status"):
                out["events"] = self._events(core, limit=5)
            # 按人一份的情境（v2.28.9，只加不改）：**所有 task** 都带。
            # 以前只有 `status` 有 relationship，社交层跑 proactive 时手里连
            # 「怎么称呼 TA、她对他什么感觉、他刚说过什么」都没有——主动消息于是
            # 只能从天气说起，还叫不出名字（用户的原话：「拿不到名称，根本不会叫人」）。
            if _s(user_id):
                out["user"] = self._user(core, str(role_id), user_id)
            # v3.0.0（联动 P2-2）：**所有 task** 都带 `relationship`。
            # 以前只有 status 才给，而社交层只调 proactive——`turns` 等字段永远拿不到。
            # 保留旧字段名与形状（只加不改），只把「什么时候给」放宽。
            out["relationship"] = self._relationship(core, user_id)
            # 指纹与 `state_fingerprint()` **同一份实现**（同一个 payload 构造器）：
            # 原来这里对整份 `out` 内联拼哈希、`state_fingerprint()` 另拼一份，
            # 两份口径不同，同一个角色的「状态有没有变」能有两个答案（审核 模型与导出 9）。
            out["fingerprint"] = self._fingerprint(
                core, self._fingerprint_payload(core, contract, body)
            )
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
            # 与 `brief()` 同一个 payload 构造器：两份哈希必须相等，否则社交层按哪个
            # 判缓存都是错的（审核 模型与导出 9）。以前这里传 `{}` 当契约，于是身体轴
            # 一律按「不可用」算，与 `brief()` 里那份带真实契约的 payload 永远不等。
            contract, body = self._contract_body(core)
            return self._fingerprint(core, self._fingerprint_payload(core, contract, body))
        except Exception:
            return ""

    # ─── 关系与亲密（按用户，只读）──────────────────────

    def intimacy(self, role_id: str, user_id: str) -> Dict[str, Any]:
        """她对某个人的亲密档案：到哪一档、有过几次、上一次什么时候、上头多少，
        以及**她这会儿在不在状态**（在不在场景里、今天几次、余韵/冷却、能不能进、
        被哪道闸挡着）。

        存在的理由：`CHANGELOG` v2.27.13 写着「契约快照多导出亲密次数 / 上次时间 /
        上头值，供社交层读」，可契约里从来没有这三样（`link.py` 的 `paths` 只有
        好感/称呼/上次互动/说过的话）。社交层据此判断「要不要对这个人开口」——
        这条链路一直是断的。v2.28.7 又补了「在不在场景里」：社交层只看得到次数，
        看不出她是不是正处在那一场里，主动开口就会踩在错误的时间点上。

        **为什么不加进契约**：契约是角色级快照，按用户的数据放进去，1000 个用户
        会把 state.json 撑大（`link.py` 对这件事已有明确注释）。门面本来就是为了
        「按需现算、不过期」而存在的，这里才是它该待的地方。

        约束同本模块头三条：只读、不抛异常、不猜。数据全部经 `core.intimacy_status()`
        的只读快照（`peek_user`），不会因为社交层问一句就给陌生人建一条永久空壳。
        **唯一的写动作**是快照内部的按天结账（`ccb.roll_day`）：跨天时把昨天的
        当日次数清零，每次跨天只发生一次；不结的话社交层读到的永远停在昨天
        （用户的原话就是「以前留下的计数不会变」）。

        返回 `{}` 表示「拿不到」（角色不存在 / 没有这个用户 / 超出可解释范围），
        调用方按「不知道」处理，不要当成「没发生过」。

        v3.0.3（联动 模型与导出 11）：**白名单拒绝不再与「拿不到」同形**。原来两者都
        返回 `{}`，调用方分不清「她拒绝」和「我不知道」——前者不该重试，后者可以等一会
        再问。现在拒绝返回 `{"status": "denied"}`，拿得到时多一个 `"status": "ok"`；
        「拿不到」仍是 `{}`（空字典 = 不知道，旧消费方 `if not intimacy` 照常成立）。
        """
        core = self._core(role_id)
        if core is None:
            return {}
        uid = _s(user_id)
        if not uid:
            return {}
        try:
            snap = core.intimacy_status(uid)
        except Exception as exc:
            self._debug(f"intimacy 失败: {exc}")
            return {}
        if not isinstance(snap, dict) or not snap:
            return {}
        if snap.get("allowed") is False:
            # 白名单：这个人不在开放名单里。**不能返回空字典**——那是「拿不到」的形状，
            # 社交层会把「她拒绝」读成「暂时问不到」，过一会又来问。
            return {"status": "denied"}
        stage = int(_num(snap.get("stage")))
        satisfy = _num(snap.get("satisfy"))
        done = _num(snap.get("done_count"))
        last_at = _num(snap.get("last_at"))
        # 历史数据（做过几次/什么时候）与「她这会儿想不想」分开授权：
        # 前者是事实，`ccb` 开着就允许读；后者会变成一条主动消息，
        # 只有 `ccb_proactive` 打开时才给。没装社交层时它永远是空串。
        proactive = False
        try:
            proactive = bool(getattr(core.config, "ccb_proactive", False))
        except Exception:
            proactive = False
        fields = self._status_fields(snap)
        # v2.28.11：她这会儿有多想主导/主动（0~1）。社交层用它决定「她主动开口」
        # 还是「她等着」。纯读、不写状态。
        initiative_score = 0.0
        try:
            from . import ccb as _ccb
            from .state_snapshot import relation_of, tz_of
            # **只读**：mood.profile() 取不到会新建条目（门面读一眼不该留空壳），
            # 与 prompt_builder 同一个只读口径。
            prof = core._scope.peek_user(uid, "mood") or {}
            lib = float(prof.get("libido", snap.get("libido", 0.0)) or 0.0)
            raw_base = prof.get("base_libido", snap.get("base_libido", lib))
            base_lib = lib if raw_base in (None, "") else float(raw_base)
            # 攻击性默认值只有一份（state_snapshot.aggression_default 读配置）：
            # 原来这里写死 25，而面板默认是 28、prompt_builder 写 15——同一个人三个数。
            # `or 25.0` 还会把合法的 0 读成 25（审核 跨插件契约 9）。
            agg = _num(prof.get("aggression"), _aggression_default(getattr(core, "config", None)))
            aff = float(prof.get("affection", 0.0) or 0.0)
            try:
                arousal = float(core.soma.snapshot().get("arousal", 50.0) or 50.0)
            except Exception:
                arousal = 50.0
            # 关系阶段只按好感算是旧口径：相处够久的人（聊过几百轮）在注入里
            # 早已是「熟到可以直说」，门面却还算成「还在熟悉」（审核 状态与人设 1）。
            # 现在走 state_snapshot 的同一份算法。
            try:
                rel_index = int(
                    relation_of(prof, core.now_epoch(), tz=tz_of(core)).stage_id
                )
            except Exception:
                rel_index = 0
            cfg = getattr(core, "config", None)
            initiative_score = _ccb.initiative(
                libido=lib,
                base_libido=base_lib,
                arousal=arousal,
                aggression=agg,
                relstage_index=rel_index,
                affection=aff,
                affection_min=float(getattr(cfg, "ccb_affection_min", 50.0) or 50.0),
                use_arousal=bool(getattr(cfg, "ccb_use_arousal", True)),
            )
        except Exception:
            initiative_score = 0.0
        initiative_label = (
            "想主动" if initiative_score >= 0.65
            else ("一般" if initiative_score >= 0.35 else "不想")
        )
        if done <= 0 and last_at <= 0 and stage <= 0:
            # 一条记录都没有：不是「拿不到」，是「确实还没有」。给调用方一个能
            # 判断的确定答案，而不是让它去区分空 dict 的两种含义。
            # 没有 ccb 记录 ≠ 没有这个人：情绪档案在时 `libido` 是实测值（照给），
            # 档案也不在时它只是**配置初始值**（34.0），与 `_user`/`_relationship`
            # 一样标 `known=false` 并置空，否则社交层会拿一个没人测过的数去做
            # 「她这会儿想不想」的判定（B5 矩阵 D5）。
            has_record = bool(self._read_only_profile(core, uid))
            return {"status": "ok", "known": has_record, "stage": 0, "satisfy": 0.0,
                    "done_count": 0.0, "last_at": 0.0, "want_line": "",
                    "initiative": round(initiative_score, 2),
                    "initiative_label": initiative_label, **fields,
                    "libido": fields["libido"] if has_record else None,
                    "base_libido": fields["base_libido"] if has_record else None}
        return {
            "status": "ok",
            "known": True,
            "stage": stage,
            "satisfy": round(satisfy, 1),
            "done_count": done,
            "last_at": last_at,
            "want_line": (
                self._intimacy_want(core, uid, stage, satisfy) if proactive else ""
            ),
            "initiative": round(initiative_score, 2),
            "initiative_label": initiative_label,
            **fields,
        }

    def _status_fields(self, snap: Dict[str, Any]) -> Dict[str, Any]:
        """从 `core.intimacy_status()` 的快照里摘出「她这会儿在不在状态」那一组字段。

        `intimacy()` 与 `_relationship()` 共用一份口径：两个出口各拼一套的话，
        社交层从不同接口拿到的「她在不在场景里」会对不上——那比没有这个字段更糟。
        """
        blockers = snap.get("blockers")
        # 两个布尔走三态（B5 矩阵 D6）：`bool(snap.get(...))` 把「快照里没这个键」
        # 读成 `False`——「不知道她在不在场景里」于是与「她明确不在」同形，社交层
        # 会据此判定「可以开口」。缺键 = `None`，不是 `False`。
        in_scene = _contract.tri_bool(snap, "in_scene")
        can_enter = _contract.tri_bool(snap, "can_enter")
        return {
            "in_scene": None if in_scene is _contract.UNKNOWN else bool(in_scene),
            "stage_name": _s(snap.get("stage_name")),
            "turns_today": int(_num(snap.get("turns_today"))),
            "cooldown_left": round(max(0.0, _num(snap.get("cooldown_left"))), 1),
            "aftercare_left": round(max(0.0, _num(snap.get("aftercare_left"))), 1),
            "libido": round(_num(snap.get("libido")), 1),
            "base_libido": round(_num(snap.get("base_libido")), 1),
            "can_enter": None if can_enter is _contract.UNKNOWN else bool(can_enter),
            "blockers": (
                [_s(item) for item in blockers if _s(item)]
                if isinstance(blockers, list) else []
            ),
        }

    def _read_only_profile(self, core: Any, user_id: str) -> Dict[str, Any]:
        """只读取情绪档案：**不建条目、不 mark_dirty、不写 state**（P2-21）。

        `mood.profile()` 对没档案的人会现建一份并 mark_dirty——门面是「每轮都可能被
        问一句」的只读入口，用它等于把读接口变成常驻写盘源，还给每个问过的人都留一条
        永久空壳。优先级：

        1. `mood.peek_profile(uid)`（v3.0.0 新增的只读快照，B/服务层提供；内部 _repair
           只作用于副本）；
        2. 旧版没有该方法时退回 `scope.peek_user(uid, 'mood')`——同样只读、不建壳。

        没有档案时返回 `{}`：调用方按面板初始值/「不知道」处理，绝不在这里替人建档案。
        """
        mood = getattr(core, "mood", None)
        for name in ("peek_profile", "profile_readonly", "snapshot_profile"):
            reader = getattr(mood, name, None) if mood is not None else None
            if not callable(reader):
                continue
            try:
                record = reader(user_id)
            except Exception:
                return {}
            return record if isinstance(record, dict) else {}
        try:
            record = core.scope.peek_user(user_id, "mood")
        except Exception:
            return {}
        return record if isinstance(record, dict) else {}

    def _intimacy_want(self, core: Any, user_id: str, stage: int, satisfy: float) -> str:
        """一句「她这会儿想不想」——**只读、只描述状态，不下指令**。

        措辞纪律照抄 `emotion.py`：说状态，不说行为约束；不写器官、不写情节。
        社交层拿它当主动开口的由头，要不要说、怎么说由它自己定。
        拿不到就空串（不编）。
        """
        profile = self._read_only_profile(core, user_id)
        if not profile:
            return ""
        try:
            libido = float(profile.get("libido", 0.0) or 0.0)
            base = float(profile.get("base_libido", libido) or libido)
        except (TypeError, ValueError):
            return ""
        if satisfy >= 60.0:
            return "她这会儿还带着点他的余温，说话会软一点"
        if libido - base >= 6.0 and libido >= 38.0:
            return "她这会儿有点想他，忍着没说"
        if stage >= 2:
            return "她记得上一次，还没过去"
        return ""

    # ─── 内部拼装（全部 try 兜住）───────────────────────

    def _framing(self, core: Any, char_name: str, user_name: str) -> str:
        """与普通回复**同一句**框架句。人格一致性从「两边写得像」变成结构保证。"""
        try:
            from .prompt_builder import build_framing

            return build_framing(char_name or core.char_name(), user_name)
        except Exception:
            return ""

    # ─── 她是谁 / 穿什么 / 作息（v2.28.9，全 task 共用）────────

    def _her(self, core: Any, contract: Dict[str, Any]) -> Dict[str, Any]:
        """她是谁：`char_name`（参照名）+ `persona_label`（哪副人设）。

        契约里有 `identity` 就先用它（热路径少碰一次人设层），没有/为空再现算——
        门面的意义是「不过期」，人设换了两边都要能马上反映出来。
        两处都拿不到 → 空串。**绝不拿 role_id 顶上**：那是内部标识，社交层会念出来。
        """
        identity = contract.get("identity")
        identity = identity if isinstance(identity, dict) else {}
        name = _s(identity.get("char_name"))
        label = _s(identity.get("persona_label"))
        if not name:
            try:
                name = _s(core.char_name())
            except Exception:
                name = ""
        if not label:
            try:
                label = _s(core.persona_label())
            except Exception:
                label = ""
        sp_label = _s(identity.get("species"))
        raw_traits = identity.get("body_traits")
        sp_traits = [
            _s(t) for t in (raw_traits if isinstance(raw_traits, list) else []) if _s(t)
        ][:4]
        if not sp_label:
            try:
                prof = core.species_profile()
            except Exception:
                prof = {}
            if isinstance(prof, dict):
                try:
                    conf = float(prof.get("confidence") or 0.0)
                except (TypeError, ValueError):
                    conf = 0.0
                if conf >= 0.65:
                    sp_label = _s(prof.get("label"))
                    sp_traits = [
                        _s(t) for t in (prof.get("traits") or []) if _s(t)
                    ][:4]
        return {
            "char_name": name,
            "persona_label": label,
            "species": sp_label,
            "body_traits": sp_traits,
        }

    def _outfit(self, core: Any, contract: Dict[str, Any]) -> Dict[str, Any]:
        """她此刻穿着：`{"enabled","text","source"}`。

        现算优先（穿搭跟天气/场景/物种绑定，契约那份可能停在上一分钟），拿不到退回
        契约；都没有 → 空 dict，调用方按「不知道她穿什么」处理。`kind` 是面板用的
        内部细节，不导出（跨插件接口只给真实要用的三样）。
        """
        try:
            info = core.outfit_info()
        except Exception:
            info = None
        if isinstance(info, dict) and info:
            return {
                "enabled": bool(info.get("enabled")),
                "text": _s(info.get("text")),
                "source": _s(info.get("source")),
            }
        raw = contract.get("outfit")
        if isinstance(raw, dict) and raw:
            return {
                "enabled": bool(raw.get("enabled")),
                "text": _s(raw.get("text")),
                "source": _s(raw.get("source")),
            }
        return {}

    def _routine(self, contract: Dict[str, Any]) -> Dict[str, Any]:
        """作息：从契约取（拿不到给空 dict）。

        不现算：作息跟着当天日程走、一天才变一次，门面每轮都可能被问一遍，没必要
        为它多读一次日程与睡眠段。返回深拷贝——调用方是别的插件，把内部字典的引用
        交出去等于把 Core 的 state 内存递给它改。
        """
        raw = contract.get("routine")
        if not isinstance(raw, dict) or not raw:
            return {}
        try:
            return copy.deepcopy(raw)
        except Exception:
            return dict(raw)

    # ─── 按人一份的情境（v2.28.9，所有 task 共用）──────────────

    def _user(self, core: Any, role_id: str, user_id: str) -> Dict[str, Any]:
        """对某个人的只读情境：她怎么叫 TA、对 TA 什么感觉、TA 说过什么。

        这是「社交层主动开口」缺的那一半信息：在没有它之前，proactive 手里只有关于
        她自己的状态，没有关于**对方**的任何东西——于是说不了「阿哲，你上次说的那个
        面试怎么样了」，也根本叫不出名字。

        **只读、不抛、不建条目**（与 `intimacy()` 同一条纪律）：

        * 情绪档案用 `peek_user` 读；没有档案就按面板初始值给（`mood.label()` 同口径），
          绝不为了拼一份摘要调用 `mood.profile()`；
        * `first_met` / `last_interaction` / `said` 全部 peek；
        * 名片名与称呼走 `mood.call_name()`（nickname → display_name → ""），
          返回值里**不编名字**：真没有就是空串，社交层应该说「TA」；
        * `attention` 见 `_attention`，没有档案 → 空 dict；
        * `intimacy` 直接复用 `self.intimacy()`（`want_line` 的授权逻辑原样不动）。

        唯一可能的写动作在 `intimacy()` 里：`ccb.roll_day` 的按天结账（跨天一次），
        那是既有行为、也是「计数不能停在昨天」的必要代价。
        """
        uid = _s(user_id)
        if not uid:
            return {}
        try:
            now = float(core.now_epoch())
        except Exception:
            now = 0.0
        try:
            scope = core.scope
        except Exception:
            scope = None

        def peek(key: str, default: Any = None) -> Any:
            if scope is None:
                return default
            try:
                return scope.peek_user(uid, key, default)
            except Exception:
                return default

        mood = getattr(core, "mood", None)

        def mood_read(name: str, default: Any = "") -> Any:
            fn = getattr(mood, name, None) if mood is not None else None
            if not callable(fn):
                return default
            try:
                return fn(uid)
            except Exception:
                return default

        record = peek("mood")
        record = record if isinstance(record, dict) else {}
        cfg = getattr(core, "config", None)

        def cfg_num(name: str, default: float) -> float:
            try:
                return float(getattr(cfg, name, default))
            except (TypeError, ValueError):
                return float(default)

        # v3.0.3（联动 模型与导出 14）：模块头承诺「不猜」，但这里原来在无档案时把
        # **配置初始值**当成这个人的好感/欲望/脾气返回，形状与真实档案完全一样。
        # 现在标出来：`known=false` + `affection_source="config_default"`。数字照给
        # （只加不改，旧消费方还能读），但消费方可以分清「这是他的档案」和「这是默认值」。
        if record:
            affection = round(
                _num(record.get("affection"), cfg_num("mood_initial_affection", 50.0)), 1
            )
            base_affection = round(_num(record.get("base_affection"), affection), 1)
            libido = round(_num(record.get("libido"), cfg_num("mood_initial_libido", 25.0)), 1)
            aggression = round(
                _num(record.get("aggression"), cfg_num("mood_initial_aggression", 15.0)), 1
            )
            known = True
            affection_source = "record"
        else:
            # 没档案 = 还没聊过：给的是**配置初始值**（与 `mood.label()` 对无档案用户的
            # 口径一致），不是关于这个人的事实。`known=false` 就是这个意思。
            affection = base_affection = round(cfg_num("mood_initial_affection", 50.0), 1)
            libido = round(cfg_num("mood_initial_libido", 25.0), 1)
            aggression = round(cfg_num("mood_initial_aggression", 15.0), 1)
            known = False
            affection_source = "config_default"

        first_met = 0.0
        try:
            first_met = float(peek("first_met", 0.0) or 0.0)
        except (TypeError, ValueError):
            first_met = 0.0
        if first_met <= 0 and record:
            # 老档案没有 user 级 first_met 时，`mood._repair` 也是拿 last_interaction 补的。
            first_met = _num(record.get("first_met", 0.0))
            if first_met <= 0:
                first_met = _num(record.get("last_interaction", 0.0))
        known_days = int((now - first_met) // 86400.0) if first_met > 0 and now > first_met else 0

        last_interaction = _num(peek("last_interaction", 0.0))
        if last_interaction <= 0 and record:
            last_interaction = _num(record.get("last_interaction", 0.0))
        # v3.0.0（联动 P2-3）：TA 说话 / 她说话分开记，社交层才分得清「对方沉默多久」。
        peer_said_at = _num(peek("peer_said_at", last_interaction))
        her_said_at = _num(peek("her_said_at", 0.0))

        raw_said = peek("said", [])
        said: List[Dict[str, Any]] = []
        for item in raw_said if isinstance(raw_said, (list, tuple)) else []:
            if not isinstance(item, dict):
                continue
            text = _s(item.get("said")) or _s(item.get("text"))
            if not text:
                continue
            said.append({"text": text, "at": round(_num(item.get("at")), 1)})
        said.sort(key=lambda row: -float(row.get("at") or 0.0))
        said = said[:5]  # 最近 5 条，新的在前

        try:
            intimacy = self.intimacy(role_id, uid)
        except Exception:
            intimacy = {}
        if not isinstance(intimacy, dict):
            intimacy = {}

        return {
            "user_id": uid,
            # 有没有这个人的档案。false 时下面的好感/欲望/脾气是配置初始值，
            # 不是关于他的事实（联动 模型与导出 14）。
            "known": known,
            "affection_source": affection_source,
            "nickname": _s(mood_read("nickname")),
            "nickname_source": _s(mood_read("nickname_source")),
            "display_name": _s(mood_read("display_name")),
            "call_name": _s(mood_read("call_name")),
            "name_source": _s(mood_read("name_source")),
            "affection": affection,
            "base_affection": base_affection,
            "affection_delta": round(affection - base_affection, 1),
            "label": _s(mood_read("label")),
            "mood_tag": _s(mood_read("tag")),
            "aggression": aggression,
            "libido": libido,
            "attention": self._attention(core, uid, now, record),
            "first_met": round(first_met, 1),
            "known_days": known_days,
            "last_interaction": round(last_interaction, 1),
            "peer_said_at": round(peer_said_at, 1),
            "her_said_at": round(her_said_at, 1),
            "said": said,
            "intimacy": intimacy,
        }

    def _attention(self, core: Any, user_id: str, now: float,
                   record: Any = None) -> Dict[str, float]:
        """注意力三轴 `{"care","focus","spare"}`（0~1），拿不到/没档案 → 空 dict。

        **只读拼装**：不直接调 `behavior.interest_state()`——它内部的 `care()` 默认
        `persist=True`，会把在意度回写 state；门面是「每轮都可能被问一次」的入口，
        那样等于把只读接口变成常驻写盘源（本模块头三条约束）。这里用同一套只读组件
        读出同样的值：`care(persist=False)` / `_focus` / `_spare`。

        没情绪档案（从没聊过的人）→ 空 dict：注意力是「有真实关系」才有意义的量，
        不能为了算它去 `mood.profile()` 给陌生人建条目。任何异常同样 → 空 dict。
        """
        if not isinstance(record, dict) or not record:
            return {}
        try:
            behavior = core.behavior
        except Exception:
            return {}
        try:
            care = float(behavior.care(user_id, now, persist=False))
            focus = float(behavior._focus(user_id, "", record))
            spare = float(behavior._spare(now))
        except Exception:
            return {}
        return {
            "care": round(care, 3),
            "focus": round(focus, 3),
            "spare": round(spare, 3),
        }

    def _self_state(self, core: Any, contract: Dict[str, Any],
                    body: Dict[str, Any],
                    wake: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """她此刻的身体状态：**先说这一块可不可用，再说数值**。

        v3.0.3（联动 跨插件契约 2 / 模型与导出 10/13）：`soma_enabled=false` 时契约里的
        `body` 不再填中性数值，而是 `available:false` + 数值 `null`。这里如实透传：

        * `available` / `reason`：整块可不可用、为什么不可用；
        * `asleep`：`True` / `False` / `None`（不知道）。**不再把 null 压成 false**——
          那会让社交层以为她醒着，然后在她睡着时发一条「在忙吗」；
        * 数值键只在确实知道时才出现。

        `asleep` 优先取同一份 payload 里 `wake` 的值（现算的那一份），保证「同一份
        payload 里两个是否睡着」不可能互相矛盾——原来一个读契约快照（最多旧 60 秒）、
        一个现算，两边可以一个说睡着、一个说醒着。
        """
        out: Dict[str, Any] = {
            "available": _contract.available(body),
            "reason": _contract.unavailable_reason(body),
            "energy_text": _s(body.get("energy_text")),
            "asleep": None,
            "feelings": [],
        }
        asleep = _contract.tri_bool(body, "asleep")
        if asleep is _contract.UNKNOWN and isinstance(wake, dict):
            asleep = _contract.tri_bool(wake, "asleep")
        out["asleep"] = None if asleep is _contract.UNKNOWN else bool(asleep)
        for key in ("energy", "social_energy", "sleep_pressure", "hunger", "discomfort"):
            value = _contract.tri_number(body, key)
            if value is not _contract.UNKNOWN:
                out[key] = _num(value)
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
                    # v3.0.3（联动 模型与导出 13）：这个数是**按名次推算的近似值**，
                    # 不是 Core 内部那个真实显著度（那个没有导出）。标出来，免得
                    # 消费方拿它做阈值判定（「只推 salience>0.8 的」会推错）。
                    "salience_source": "rank_estimate",
                })
        return out

    #: 契约快照的保鲜期，与 `core_instance.CONTRACT_MAX_AGE_SECONDS` 同值。
    #: 门面只拿一个 duck-typed `core`、不 import 实现模块，所以这里独立写一份；
    #: 它唯一的用途是给 `wake.stale` 一个判据（「这份醒没醒是不是旧快照」）。
    CONTRACT_MAX_AGE_SECONDS = 60.0

    def _wake(self, core: Any, contract: Dict[str, Any],
              body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """醒来状态：现算优先，拿不到退回契约那份，都没有就「不知道」。

        v3.0.3（联动 模型与导出 10）：同一份 payload 里**只能有一个「是否睡着」**。
        原来 `self_state.asleep` 读契约快照（最多旧 60 秒）、这里现算，两边可以一个
        说睡着、一个说醒着。现在 `self_state` 优先取这里的值（见 `_self_state`），
        两者不可能再矛盾。

        另外三件事如实标出来，不再让消费方猜：

        * `available`：这一项知不知道（`False` 时 `asleep` 是 `None`）；
        * `source`：`live`（现算）/ `contract`（契约快照）/ `unavailable`；
        * `stale`：来自契约时，那份快照比 `CONTRACT_MAX_AGE_SECONDS` 旧——消费方该
          知道「醒没醒」可能是过期事实，而不是把它当成此刻的状态。

        身体轴没在跑时**不读** `wake_state()`：那是上次开启时留下的旧值，读出来就是
        「她醒着」，与「这一项没在跑」是两回事。
        """
        # 块不可用（显式 false / 缺块 / 形状坏）一律不读 live：身体轴没在跑时
        # `soma.wake_state()` 是上次开启留下的旧值，读出来就是「她醒着」。
        if not _contract.available(body):
            return {
                "available": False,
                "source": "unavailable",
                "reason": _contract.unavailable_reason(body) or _contract.REASON_MISSING,
                "stale": False,
                "asleep": None,
            }
        try:
            state = core.soma.wake_state()
        except Exception:
            state = None
        source = "live"
        if not (isinstance(state, dict) and state):
            state = contract.get("wake")
            source = "contract"
        if not (isinstance(state, dict) and state):
            return {
                "available": False,
                "source": "unavailable",
                "reason": _contract.REASON_MISSING,
                "stale": False,
                "asleep": None,
            }
        stale = False
        if source == "contract":
            try:
                generated = float(contract.get("generated_at") or 0.0)
                if generated > 0:
                    stale = (float(core.now_epoch()) - generated) > self.CONTRACT_MAX_AGE_SECONDS
            except Exception:
                stale = False
        asleep = _contract.tri_bool(state, "asleep")
        return {
            "available": True,
            "source": source,
            "reason": "",
            "stale": bool(stale),
            "asleep": None if asleep is _contract.UNKNOWN else bool(asleep),
            "wake_at": _num(state.get("wake_at")),
            "inertia_until": _num(state.get("inertia_until")),
            "inertia_minutes_left": max(0, int(_num(state.get("inertia_minutes_left")))),
            "disturbed": bool(state.get("disturbed", False)),
            "last_sleep_hours": _num(state.get("last_sleep_hours")),
        }

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
        """近期事件。两种来源，按时间倒序：

        1. **刚发生的事**（``kind="recent"``）：日程层那个事件机制——她被夸了/
           被骂了/被敷衍了。契约里 `event` 块 v2.28.5 起就有（联动 P2-28：旧注释
           误写成从未发布过的 v2.29.0），3.0.0 起继续提供；这里转成同一形状。
        2. **还没结束的事**（``kind="open"``）：`life_log` 的未完成项。

        社交层拿它们当主动开口的由头。旧 Core 两个都没有 → 空列表，
        按「没有由头」处理即可。
        """
        out: List[Dict[str, Any]] = []
        # 1. 刚刚发生的事（只读，不取走——取了这一轮决定就用不上了）
        try:
            pending = core.schedule.pending_event()
        except Exception:
            pending = {}
        if isinstance(pending, dict) and pending:
            phrase = _s(pending.get("phrase"))
            if phrase:
                at = core.now_epoch() - _num(pending.get("age_seconds"))
                out.append({
                    "id": _item_id("recent", phrase, at),
                    "kind": "recent",
                    "text": phrase,
                    "at": round(at, 1),
                    "ttl": _num(pending.get("ttl")),
                })
        # 2. 还没做完的事
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
        """给社交层的「这段关系」摘要：**所有 task 都返回**（v3.0.0，联动 P2-2）。

        以前只有 `task=status` 才带 `relationship`，而社交层只调 `task=proactive`——
        v2.28.17 招牌的 `relationship.turns` 对唯一消费方永远不可见，它只能继续自己数消息。

        读路径全程只读（P2-21）：情绪档案走 `peek_profile`/`peek_user`（不建条目、不
        mark_dirty），用户级键也走 `peek_user`（`user_state()` 会 setdefault 给陌生人留空壳）。
        没档案时给面板初始值，但会标 `known=false` + `affection_source="config_default"`：
        那是配置里的初始值，**不是关于这个人的事实**（联动 模型与导出 14）。
        """
        uid = _s(user_id)
        if not uid:
            return {}
        profile = self._read_only_profile(core, uid)

        cfg = getattr(core, "config", None)

        def cfg_num(name: str, default: float) -> float:
            try:
                return float(getattr(cfg, name, default))
            except (TypeError, ValueError):
                return float(default)

        def peek(key: str, default: Any = None) -> Any:
            try:
                return core.scope.peek_user(uid, key, default)
            except Exception:
                return default

        initial_affection = cfg_num("mood_initial_affection", 50.0)
        if profile:
            affection = _num(profile.get("affection"), initial_affection)
            base_affection = _num(profile.get("base_affection"), affection)
        else:
            affection = base_affection = initial_affection
        # 亲密三键（P0-5）与 `intimacy()` 同源：CHANGELOG v2.27.13 声称契约导出了
        # 它们，实际没有；`status` 这条路上补一份，社交层不必多调一次门面。
        # v2.28.7 起直接读同一份 `intimacy_status()` 快照，多出来的「在不在场景里」
        # 那一组字段与 `intimacy()` 逐字段一致——两处各拼一套迟早对不上。
        try:
            snap = core.intimacy_status(user_id)
        except Exception as exc:
            self._debug(f"relationship 快照失败: {exc}")
            snap = {}
        if not isinstance(snap, dict):
            snap = {}
        if snap:
            done = _num(snap.get("done_count"))
            satisfy = _num(snap.get("satisfy"))
            last_at = _num(snap.get("last_at"))
            stage = int(_num(snap.get("stage")))
            fields = self._status_fields(snap)
        else:  # 兜底：快照拿不到时，退回原来的三个 peek 键（老 Core / 内部异常）
            try:
                done = _num(core.scope.peek_user(user_id, "ccb_done_count", 0.0))
                satisfy = _num(core.scope.peek_user(user_id, "ccb_satisfy", 0.0))
                last_at = _num(core.scope.peek_user(user_id, "ccb_last_at", 0.0))
                stage = int(_num(core.scope.peek_user(user_id, "ccb_stage", 0.0)))
            except Exception:
                done, satisfy, last_at, stage = 0.0, 0.0, 0.0, 0
            fields = self._status_fields({})
        if snap.get("allowed") is False:
            # 名单外的人：亲密三键一律当「没有」，免得社交层拿一个本不该
            # 开放的数字去决定主动不主动。
            done, satisfy, last_at, stage = 0.0, 0.0, 0.0, 0
            fields = self._status_fields({})
        last_interaction = _num(peek("last_interaction", 0.0))
        peer_said_at = _num(peek("peer_said_at", last_interaction))
        first_met = _num(peek("first_met", 0.0))
        if first_met <= 0 and profile:
            first_met = _num(profile.get("first_met", 0.0))
        return {
            "known": bool(profile),
            "affection_source": "record" if profile else "config_default",
            "affection": round(affection, 1),
            "base_affection": round(base_affection, 1),
            "nickname": _s(peek("nickname", "")),
            # v3.0.0（联动 P2-3）：`last_interaction` 保持兼容语义 = **TA 上次说话**
            #（旧版在外面还把它在「她主动开口」时往前推，社交层据此误判对方刚说过话）。
            # 她说话的时间另记 `her_said_at`；`peer_said_at` 是同一语义的显式别名，
            # 新社交层用它可以不依赖 last_interaction 的历史含义。
            "last_interaction": round(last_interaction, 1),
            "peer_said_at": round(peer_said_at, 1),
            "her_said_at": round(_num(peek("her_said_at", 0.0)), 1),
            # 对话轮数与相识时间：主动层判断「这段关系够不够近」时不用再自己数消息，
            # 也不会把「好感被 override 钉成 100、涨幅为 0」的人误判成生人。
            "turns": int(_num(profile.get("turn_count"), 0.0) or 0),
            "first_met": round(first_met, 1),
            "intimacy_done_count": done,
            "intimacy_satisfy": round(satisfy, 1),
            "intimacy_last_at": last_at,
            "stage": stage,
            **fields,
        }

    def _fingerprint_payload(self, core: Any, contract: Dict[str, Any],
                             body: Dict[str, Any]) -> Dict[str, Any]:
        """指纹只认这一份 payload：`brief()` 与 `state_fingerprint()` 都从这里取。

        **只放角色级、且两边都拿得到的东西**。用户级（好感/关系阶段）不在里面：
        `state_fingerprint(role_id)` 没有 `user_id`，塞进去会让两份哈希必然不等。
        """
        day = contract.get("day") if isinstance(contract.get("day"), dict) else {}
        wake = self._wake(core, contract, body)
        return {
            "self_state": self._self_state(core, contract, body, wake),
            "wake": wake,
            "day": {
                "doing": _s(day.get("doing")),
                "done": [_s(x) for x in (day.get("done") or []) if _s(x)],
                "next": [_s(x) for x in (day.get("next") or []) if _s(x)],
                "dreams": [_s(x) for x in (day.get("dreams") or []) if _s(x)][:3],
            },
            "ongoing": self._ongoing(core),
        }

    def _fingerprint(self, core: Any, payload: Dict[str, Any]) -> str:
        """状态指纹：状态没变，社交层的缓存就不该失效。

        **时间不是状态**。原来最后一项是 `str(int(core.now_epoch()) // 60)`，于是指纹
        每 60 秒**必然**变一次——社交层拿它做缓存，等于每 60 秒整份失效，「状态没变就
        不失效」这个前提从来没成立过（审核 模型与导出 8）。现在只取一组固定字段，
        缺的按空值补齐，规范化 JSON（`sort_keys`）后取 sha256 前 16 位。

        反过来，原来漏掉的字段（`asleep` / `energy` / `wake`）现在也在里面：
        她睡着了、精力掉了一档，指纹都该跟着变。
        """
        try:
            self_state = payload.get("self_state") or {}
            wake = payload.get("wake") or {}
            day = payload.get("day") or {}
            ongoing = payload.get("ongoing") or []
            facts = {
                "self_available": self_state.get("available"),
                "self_reason": _s(self_state.get("reason")),
                "asleep": self_state.get("asleep"),
                "energy": self_state.get("energy"),
                "energy_text": _s(self_state.get("energy_text")),
                "feelings": [
                    _s(item.get("text") if isinstance(item, dict) else item)
                    for item in (self_state.get("feelings") or [])
                ],
                "wake_available": wake.get("available"),
                "wake_source": _s(wake.get("source")),
                "wake_asleep": wake.get("asleep"),
                "doing": _s(day.get("doing")),
                "done": [_s(x) for x in (day.get("done") or [])],
                "next": [_s(x) for x in (day.get("next") or [])],
                # 梦变了（睡了一觉/换了日程）也是状态变化：社交层缓存该失效。
                "dreams": [_s(x) for x in (day.get("dreams") or [])],
                "ongoing": [
                    _s(item.get("text") if isinstance(item, dict) else item)
                    for item in ongoing
                ],
            }
            raw = json.dumps(facts, sort_keys=True, ensure_ascii=False, default=str)
            return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        except Exception:
            return ""

    def _debug(self, message: str) -> None:
        if self._log is None:
            return
        try:
            self._log.debug(f"[humanoid_core] 门面: {message}")
        except Exception:
            pass


def get_core_facade(roles: Any = None, logger: Any = None, *, generation: int = 0,
                    is_current: Any = None) -> Optional[CoreFacade]:
    """给别的插件拿门面。宿主按 `star_cls.__module__` 找过来，这里必须可调用。

    没有角色管理器（插件还没起来）时返回 None——对方会退回读 state.json。

    `generation` / `is_current` 由 `main.get_core_facade()` 传入（P1-B）：门面绑定插件
    实例代次，每次调用校验它是否仍是当前实例；旧门面调用一律退化成空值。
    不传时退化为旧行为（单测/直接构造）。
    """
    if roles is None:
        return None
    return CoreFacade(roles, logger, generation=generation, is_current=is_current)
