"""每个角色的一套基础信息——名字、年龄、生日。

为什么要有这个模块：用户原话是「这个插件 没有给每个角色可以拥有一套基础信息
如年龄 且也是会变得」。在此之前，年龄只出现在人设原文里，而且只有 `ccb.py` 为了
判「是否成年」扫过一眼「N 岁」——日程提示词、聊天提示词都不知道她今年多大，更
不会随年份变化。

两条设计规矩：

1. **年龄是算出来的，不是存下来的。** 状态里只存 `age` + `age_as_of`（这份档案是
   哪一天登记的那个年龄），或者 `birthday` + `birth_year`。每次读的时候按「今天」
   现算过一次生日没有——于是跨年、过生日它自己就变了，不需要任何定时任务去改状态。
   存一个「今年 23 岁」的字段才是会过期的写法。
2. **只从人设里能读到的东西来，不编。** 人设没写年龄就留空，宁可提示词里没有这
   一段，也不能替她生出一个年龄——「替她编身份」这条线在素材池那边已经踩过。

这份档案是状态（`self["basics"]`）而不是配置：每个角色一份，随角色走。
"""

from __future__ import annotations

import re
from typing import Any, Mapping

#: 状态里的键名（role_scope 的 self 区）。
PROFILE_KEY = "basics"

_BIRTHDAY = re.compile(r"生日[^0-9]{0,6}(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
_BIRTH_YEAR = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]\s*出生")
_BORN_IN = re.compile(r"出生于\s*(\d{4})\s*年(?:\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号])?")
_AGE = re.compile(r"(\d{1,3})\s*岁")
_NAME = re.compile(
    r"(?:名字|姓名|叫作|叫做)\s*(?:叫作|叫做|名叫|名为|是|为|叫|：|:)?\s*([^\s，,。；;、！!？?]{1,16})"
)
#: 生日只写月日时，允许的最大年龄（防人设里「3 岁」这类玩笑数字被当成成年人档案）。
AGE_MIN = 1
AGE_MAX = 200


def _as_date(text: str) -> tuple[int, int, int] | None:
    """把 `YYYY-MM-DD` 解析成 (年, 月, 日)；格式不对返回 None。"""
    body = str(text or "").strip()
    parts = body.split("-")
    if len(parts) != 3:
        return None
    try:
        year, month, day = (int(part) for part in parts)
    except ValueError:
        return None
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return year, month, day


def parse_basics(
    persona_text: str,
    *,
    name: str = "",
    today: str = "",
) -> dict[str, Any]:
    """从人设原文里认出能用的基础信息；认不出的键直接不出现。

    `today` 是 `YYYY-MM-DD`：给了就把「登记这天的年龄」记下来
    （`age_as_of`），年龄从此按周年自动增长。
    """
    body = str(persona_text or "")
    profile: dict[str, Any] = {}

    known_name = str(name or "").strip()
    if known_name:
        profile["name"] = known_name
    else:
        hit = _NAME.search(body)
        if hit:
            profile["name"] = hit.group(1).strip()

    birthday = ""
    birth_year = 0
    hit = _BIRTH_YEAR.search(body)
    if hit:
        birth_year = int(hit.group(1))
        birthday = f"{int(hit.group(2)):02d}-{int(hit.group(3)):02d}"
    else:
        hit = _BIRTHDAY.search(body)
        if hit:
            birthday = f"{int(hit.group(1)):02d}-{int(hit.group(2)):02d}"
    if birthday:
        profile["birthday"] = birthday
    if not birth_year:
        hit = _BORN_IN.search(body)
        if hit:
            birth_year = int(hit.group(1))
            if not birthday and hit.group(2) and hit.group(3):
                # 「出生于 2003 年 4 月 9 日」：出生年与生日一起给，年龄才算得准。
                birthday = f"{int(hit.group(2)):02d}-{int(hit.group(3)):02d}"
                profile["birthday"] = birthday
    if birth_year:
        profile["birth_year"] = birth_year

    hit = _AGE.search(body)
    if hit:
        age = int(hit.group(1))
        if AGE_MIN <= age <= AGE_MAX:
            profile["age"] = age
            if str(today or "").strip():
                profile["age_as_of"] = str(today).strip()
    # 有出生年就不用再存「登记年龄」：那个数字一旦写死就会和生日冲突。
    if profile.get("birth_year"):
        profile.pop("age", None)
        profile.pop("age_as_of", None)
    return profile


def age_at(basics: Mapping[str, Any], today: str) -> int:
    """她在 `today`（`YYYY-MM-DD`）这一天多大。算不出来返回 0。

    两条路：
    * 有出生年 + 生日：按今年生日过没过算；
    * 只有登记年龄：按 `age_as_of` 那天到今天的**周年数**加上去——所以过了周年
      自己就长一岁，不需要谁去改状态。
    """
    moment = _as_date(today)
    if moment is None:
        return 0
    year, month, day = moment
    birth_year = int(basics.get("birth_year") or 0)
    birthday = str(basics.get("birthday") or "")
    if birth_year > 0:
        age = year - birth_year
        if birthday:
            try:
                bmonth, bday = (int(part) for part in birthday.split("-"))
            except ValueError:
                bmonth, bday = 1, 1
            if (month, day) < (bmonth, bday):
                age -= 1
        # 只知道出生年、不知道生日：按年份差算，误差最多一岁——比不写好。
        return max(0, age)
    base = int(basics.get("age") or 0)
    if base <= 0:
        return 0
    since = _as_date(str(basics.get("age_as_of") or ""))
    if since is None:
        return base
    syear, smonth, sday = since
    grown = year - syear
    if (month, day) < (smonth, sday):
        grown -= 1
    return max(0, base + max(0, grown))


def render_basics(basics: Mapping[str, Any], today: str) -> str:
    """一句话的基础信息（给提示词用）。什么都没有时返回空串。"""
    if not isinstance(basics, Mapping) or not basics:
        return ""
    bits: list[str] = []
    name = str(basics.get("name") or "").strip()
    if name:
        bits.append(f"名字是{name}")
    age = age_at(basics, today)
    if age > 0:
        bits.append(f"今年 {age} 岁")
    birthday = str(basics.get("birthday") or "").strip()
    if birthday:
        try:
            month, day = (int(part) for part in birthday.split("-"))
            bits.append(f"生日 {month} 月 {day} 日")
        except ValueError:
            pass
    if not bits:
        return ""
    return "，".join(bits) + "。"


def merge_missing(current: Any, parsed: Mapping[str, Any]) -> dict[str, Any]:
    """把新认出来的键补进已有档案：已有的值一个都不覆盖（用户的改动优先）。"""
    merged: dict[str, Any] = dict(current) if isinstance(current, Mapping) else {}
    for key, value in (parsed or {}).items():
        if value in ("", 0, None):
            continue
        if merged.get(key) in ("", 0, None):
            merged[key] = value
    return merged
