"""跨插件契约的「三态」工具：确定的值 / 确定没有 / 不知道。

为什么要有这个模块
------------------
v3.0.2 及以前，契约与信号文件里「这一项没在跑」「这一项拿不到」都被编码成**中性
数值**：`soma_enabled=false` 时导出 `hunger=0.0`、`arousal=50.0`、`asleep=false`；
时区算不出来时导出 `utc_offset_minutes=0`；社交层文件读坏了、过期了，读回来的是
上一次的旧数据或空表。这些形状与「真实值恰好是 0 / 50 / 醒着 / UTC」**完全同形**，
接收方按算术读，读出来就是「她状态正常」「她那边没有时差」——而事实是「这一项我
不知道」。审计里跨插件契约 2、模型与导出 10/11/13/14、时钟与天气 4/5 这些条目是
同一个根：**契约没有「不可用」的表达位**。

本模块只提供表达位与判定，不持有任何状态：

* `UNKNOWN` —— Python 侧的「不知道」哨兵。用 `is UNKNOWN` 判断，不要用真值判断
  （`bool(UNKNOWN)` 是 False，那是给「忘了判断」的代码一个安全的方向）。
* `unavailable(reason)` —— 造一个带标记的「不可用」字典（契约/信号里用
  `{"available": false, "reason": ...}`）。
* `available(payload)` / `unavailable_reason(payload)` —— 读那个标记。
* `tri(payload, key)` —— 三态读一个键：有值 → 值；不可用 / 缺键 / 值是 null →
  `UNKNOWN`。

为什么用 `None` 而不是哨兵写进契约
----------------------------------
契约是 JSON，没有哨兵这种类型。JSON 的「没有这个值」只有 `null`，所以**线上表达**
一律是 `null` + 块级 `available:false`；`UNKNOWN` 只是 Python 侧的读法，让调用方
没法把 `null` 直接当成 0 去算（`None + 1` 会抛，而 `0 + 1` 不会）。
"""

from typing import Any, Dict, Final


class _Unknown:
    """「不知道」的哨兵。只有一个实例：`UNKNOWN`。"""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 只用于调试输出
        return "UNKNOWN"

    def __bool__(self) -> bool:
        # 故意为 False：忘了写 `is UNKNOWN` 的代码会走进「没有」分支，
        # 而「没有」在注入层表现为「这句不说」——安全的方向。
        return False

    def __copy__(self) -> "_Unknown":
        return self

    def __deepcopy__(self, memo: Any) -> "_Unknown":
        return self


UNKNOWN: Final = _Unknown()

#: 不可用的原因。只用这几个，接收方按它分支，不要再自造同义词。
REASON_MISSING: Final = "missing"        # 文件/键根本不存在
REASON_STALE: Final = "stale"            # 有数据但过期了
REASON_CORRUPT: Final = "corrupt"        # 有文件但读不出来（坏 JSON / 超大 / 是目录）
REASON_NO_DATA_ROOT: Final = "no_data_root"  # 连数据根目录都拿不到
REASON_DISABLED: Final = "disabled"      # 这个功能被配置关掉了（不是故障）
REASON_UNREADABLE: Final = "unreadable"  # 保留：旧调用方用的词，等同 CORRUPT


def unavailable(reason: str) -> Dict[str, Any]:
    """造一个「不可用」块。`reason` 只写常量，别写中文句子——那是给日志的。"""
    return {"available": False, "reason": str(reason or REASON_MISSING)}


def available(payload: Any) -> bool:
    """这一块到底可不可用。

    **形状不对就是不可用**（B5 矩阵 D1）：原来 `None` / 字符串 / 列表一律判「可用」，
    于是「缺块」与「一切正常」同形，消费方拿到的 `available: true` 其实是「我不知道」。

    * 不是 dict（`None`、字符串、列表……）→ 不可用；
    * 显式 `available: false` → 不可用；
    * 其余字典 → 可用：v3.0.2 之前写出来的文件没有这个键，不能因此把老数据判坏。
    """
    if not isinstance(payload, dict):
        return False
    return payload.get("available") is not False


def unavailable_reason(payload: Any) -> str:
    """不可用的原因；可用时返回空串。

    与 `available()` 同一套判定：不是 dict 也要给得出原因（`None` = 根本没有这个块，
    其他 = 有东西但形状不对），否则消费方只能拿到「不可用」而说不出为什么。
    """
    if not isinstance(payload, dict):
        return REASON_MISSING if payload is None else REASON_CORRUPT
    if payload.get("available") is False:
        return str(payload.get("reason") or REASON_MISSING)
    return ""


def as_block(payload: Any, missing: str = REASON_MISSING) -> Dict[str, Any]:
    """把「可能没有 / 可能坏了」的来源规整成一块：字典原样返回，其余转成不可用块。

    缺块与坏块分开报：`None` = 根本没有（`missing`），其他非字典 = 有东西但形状不对
    （`corrupt`）。两者都不许被 `{}` 顶替——空字典在 `open_items` 那里等于「写方说
    全结了」，会把她的未完事项整本清空。
    """
    if isinstance(payload, dict):
        return payload
    return unavailable(REASON_CORRUPT if payload is not None else missing)


def tri(payload: Any, key: str, default: Any = UNKNOWN) -> Any:
    """三态读一个键。

    * 块不可用 → `UNKNOWN`（整块都不知道，不必再看键）；
    * 键不在 / 值是 `null` → `default`（默认 `UNKNOWN`）；
    * 否则 → 原值。
    """
    if not available(payload):
        return default
    if not isinstance(payload, dict) or key not in payload:
        return default
    value = payload[key]
    if value is None:
        return default
    return value


def is_known(value: Any) -> bool:
    """`tri()` 的返回值是不是一个真实的确定值。"""
    return value is not UNKNOWN and value is not None


def tri_bool(payload: Any, key: str) -> Any:
    """读一个三态布尔：`True` / `False` / `UNKNOWN`。

    不要用 `bool(tri(...))`：那会把 `UNKNOWN` 变成 `False`，等于又回到「不知道
    就是没有」——这正是本模块要消掉的那个错。
    """
    value = tri(payload, key)
    if value is UNKNOWN:
        return UNKNOWN
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        # 认不出的字符串是「不知道」，不是「假」：原来一行 `in (...)` 把 "maybe" /
        # "unknown" / "关" 全判成已知的 False，与 `tri_number` 对坏值的口径相反。
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off", ""):
            return False
        return UNKNOWN
    return UNKNOWN


def tri_number(payload: Any, key: str) -> Any:
    """读一个三态数值：`float` / `UNKNOWN`。坏值（字符串、NaN）也算不知道。"""
    value = tri(payload, key)
    if value is UNKNOWN:
        return UNKNOWN
    try:
        number = float(value)
    except (TypeError, ValueError):
        return UNKNOWN
    if number != number:  # NaN
        return UNKNOWN
    return number
