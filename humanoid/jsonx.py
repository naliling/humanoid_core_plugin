"""从大模型回复里抠出 JSON。

两个要点：
1. 括号配对时跳过字符串字面量 —— 活动名里出现 `[` / `{` 不会把扫描带偏；
2. 数组与对象各有独立入口，都会依次尝试「整体解析 → ```json 围栏 → 括号配对」。
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE_PATTERN = re.compile(r"```(?:json|JSON)?\s*(.+?)\s*```", re.DOTALL)


def _scan_balanced(text: str, open_ch: str, close_ch: str) -> str | None:
    """返回第一个配对完整的括号片段，扫描时忽略字符串内的括号。"""
    start = text.find(open_ch)
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
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
        elif char == open_ch:
            depth += 1
        elif char == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _candidates(raw: str) -> list[str]:
    text = (raw or "").strip()
    if not text:
        return []
    out = [text]
    out.extend(match.strip() for match in _FENCE_PATTERN.findall(text))
    return out


def extract_json_array(raw: str) -> list[Any] | None:
    """抠出一个 JSON 数组。找不到或解析失败返回 None。"""
    for candidate in _candidates(raw):
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            for key in ("schedule", "slots", "data", "items", "result"):
                inner = parsed.get(key)
                if isinstance(inner, list):
                    return inner
        fragment = _scan_balanced(candidate, "[", "]")
        if fragment:
            try:
                parsed = json.loads(fragment)
            except (ValueError, TypeError):
                continue
            if isinstance(parsed, list):
                return parsed
    return None


def extract_json_object(raw: str) -> dict[str, Any] | None:
    """抠出一个 JSON 对象。找不到或解析失败返回 None。"""
    for candidate in _candidates(raw):
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            return parsed
        fragment = _scan_balanced(candidate, "{", "}")
        if fragment:
            try:
                parsed = json.loads(fragment)
            except (ValueError, TypeError):
                continue
            if isinstance(parsed, dict):
                return parsed
    return None
