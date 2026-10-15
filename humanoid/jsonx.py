"""从大模型回复里抠出 JSON。"""

from __future__ import annotations

import json
import re
from typing import Any

from astrbot.api import logger as _LOG

_FENCE_PATTERN = re.compile(r"```(?:json|JSON)?\s*(.+?)\s*```", re.DOTALL)

_OPENERS = {"[": "]", "{": "}"}
_CLOSERS = {close: open_ch for open_ch, close in _OPENERS.items()}

# 对象包数组的常见外壳键：`{"schedule": [...]}` 也算拿到了数组。
_ARRAY_WRAPPER_KEYS = ("schedule", "slots", "data", "items", "result")


def _debug(message: str) -> None:
    try:
        _LOG.debug(f"[humanoid_core] JSON 提取: {message}")
    except Exception:
        pass


def _containers(text: str) -> list[tuple[str, int, int]]:
    """扫出文本里所有**配对完整**的容器，返回 (片段, 外面还套着几层, 起始下标)。

    带字符串与转义识别：`"看剧 [第2季]"` 里的方括号不是容器。
    「外面还套着几层」= 0 的那些就是**根节点**候选——这一点是必需的：
    原来 `_scan_balanced` 只从**第一个**开括号扫，于是正文里先出现一个 `[注]` / `{要求}`
    就能让整个解析失败或返回错片段。
    """
    out: list[tuple[str, int, int]] = []
    stack: list[tuple[str, int]] = []
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in _OPENERS:
            stack.append((char, index))
        elif char in _CLOSERS:
            if stack and stack[-1][0] == _CLOSERS[char]:
                opener, start = stack.pop()
                out.append((text[start : index + 1], len(stack), start))
    return out


def _candidates(raw: str) -> list[str]:
    """候选文本：**fenced block 优先**，整段文本兜底。

    模型规范地用 ```json 包裹时，fence 里的内容是最可信的候选。原来把整段（含散文）
    排在最前面：白一次全扫，而且可能先从散文里的括号扫出一个**错片段并成功解析**
    （`prefix [1] then real [{"a":2}]` 就返回了 `[1]`）。
    """
    text = (raw or "").strip()
    if not text:
        return []
    out = [match.strip() for match in _FENCE_PATTERN.findall(text)]
    out.append(text)
    return out


def _pick_root(text: str, open_ch: str) -> Any:
    """取「根节点」上、类型为 `open_ch` 的完整容器并解析；没有就返回 None。

    两条规矩：

    1. **只认根节点类型**。`[{"a":1},{"b":2}]` 是一个数组响应——它里面的 `{"a":1}`
       虽然也是完整对象，但被数组套着，不是根节点，所以 `extract_json_object` 必须
       返回 None，而不是把它截成第一个元素当成「成功」。
    2. 根节点有多个时取**最后一个**：散文里常先出现一个 `[注]` 之类的括号，
       真正的载荷写在最后（`prefix [1] then real [{"a":2}]` → 取 `[{"a":2}]`）。
    """
    for fragment, depth, _start in reversed(_containers(text)):
        if depth != 0 or not fragment.startswith(open_ch):
            continue
        try:
            return json.loads(fragment)
        except (ValueError, TypeError) as exc:
            _debug(f"根节点片段解析失败（{exc}）：{fragment[:80]!r}")
            continue
    return None


def extract_json_array(raw: str) -> list[Any] | None:
    for candidate in _candidates(raw):
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError) as exc:
            _debug(f"整体解析失败（{exc}），改扫根节点：{candidate[:80]!r}")
            parsed = None
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            for key in _ARRAY_WRAPPER_KEYS:
                inner = parsed.get(key)
                if isinstance(inner, list):
                    return inner
        picked = _pick_root(candidate, "[")
        if isinstance(picked, list):
            return picked
    _debug(f"没提取到数组：{str(raw or '')[:100]!r}")
    return None


def extract_json_object(raw: str) -> dict[str, Any] | None:
    for candidate in _candidates(raw):
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError) as exc:
            _debug(f"整体解析失败（{exc}），改扫根节点：{candidate[:80]!r}")
            parsed = None
        if isinstance(parsed, dict):
            return parsed
        picked = _pick_root(candidate, "{")
        if isinstance(picked, dict):
            return picked
    _debug(f"没提取到对象：{str(raw or '')[:100]!r}")
    return None
