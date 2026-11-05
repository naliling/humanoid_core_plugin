"""今天要办的事 - 从对话里抽出来的、她今天确实要做的事（v3.0.10）。

日程生成原来只看得到人设、身体、天气和最近几段日程。可她今天要陪人去复查、要交稿、
要去看牙——这些只存在于对话里。于是模型给她排的永远是她一个人在屋里绕圈，跟「今天」
没有任何关系。

这个模块补的是这一层，但只补**事实**，不补想象。抽取有四道门，缺一不可：

1. **日期门**：这句话里必须明确说了「今天」（今天/今晚/今早/今日/今儿，或者落到今天的
   周X、X月X日、X号）。说了明天、后天、下周的不要——那不是今天要办的事。
2. **主语门**：必须是**她**的事。「你今天记得吃药」「她下午要去复查」「我们今天去看电影」
   算；「我今天要去医院」是对方自己的安排，记进来就是替她编了一件事。
3. **语气门**：疑问句、否定句（不打算/不想/别）不算计划。
4. **原话门**：只存这句话的原文片段（截到 40 字），不改写、不补细节、不推断地点时间。

宁可漏抽，也不能把对方的安排记成她的：漏抽只是少一条素材，记错就是日程在编造。
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Iterable

PLANS_KEY = "today_plans"
PLANS_DATE_KEY = "today_plans_date"

MAX_PLANS = 3
PLAN_MAX_CHARS = 40

# 主语：她（第二人称「你」在对话里指的就是她）、第三人称、以及共同出行。
_SUBJECTS = ("你", "她", "我们", "咱们")

# 计划动词：一句里没有这类词，多半只是陈述或感叹。
_PLAN_VERBS = (
    "要", "得", "打算", "准备", "约", "答应", "计划", "需要", "说好", "定好",
    "会", "去", "办", "拿", "交", "见", "开", "送", "买", "做", "看", "陪",
    "接", "跑", "复习", "整理", "收拾", "学", "练",
)

# 否定/放弃：这些词出现时，同一句多半不是「要去做」。
_NEGATIONS = ("不打算", "不想", "不去", "不用", "不要", "没打算", "没说要", "别", "算了")

# 分隔符要留下来：只按分隔符切会把「？」本身丢掉，于是「你今天要去医院？」切成
# 「你今天要去医院」——疑问句再也认不出来，被当成计划存进日程。所以用捕获组切，
# 让每个分句知道自己是拿什么标点结束的。
_CLAUSE_SPLIT = re.compile(r"([。！？!?；;\n])")

_DAY_WORDS = (
    ("大后天", 3),
    ("后天", 2),
    ("明天", 1), ("明早", 1), ("明晚", 1), ("明儿", 1),
    ("今天", 0), ("今晚", 0), ("今早", 0), ("今儿", 0), ("今日", 0),
)

_WEEKDAY_INDEX = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def _weekday_offsets(clause: str, today: date) -> set[int]:
    offs: set[int] = set()
    for m in re.finditer(r"下(?:周|星期|礼拜)([一二三四五六日天])", clause):
        target = _WEEKDAY_INDEX[m.group(1)]
        offs.add((target - today.weekday()) % 7 + 7)
    for m in re.finditer(r"(?<!下)(?:周|星期|礼拜)([一二三四五六日天])", clause):
        target = _WEEKDAY_INDEX[m.group(1)]
        offs.add((target - today.weekday()) % 7)
    return offs


def _day_of_month(clause: str, today: date) -> set[int]:
    """「5 号」「11 月 5 日」这类绝对日期，只保留能解析出来的。"""
    offs: set[int] = set()
    pairs: list[tuple[int, int]] = []
    for m in re.finditer(r"(\d{1,2})月(\d{1,2})[日号]", clause):
        pairs.append((int(m.group(1)), int(m.group(2))))
    for month, day in pairs:
        try:
            target = date(today.year, month, day)
        except ValueError:
            continue
        if target < today:
            try:
                target = date(today.year + 1, month, day)
            except ValueError:
                continue
        offs.add((target - today).days)
    if pairs:
        return offs
    for m in re.finditer(r"(?<!\d)(\d{1,2})[号日]", clause):
        day = int(m.group(1))
        try:
            target = today.replace(day=day)
        except ValueError:
            continue
        if target < today:
            nxt = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
            try:
                target = nxt.replace(day=day)
            except ValueError:
                continue
        offs.add((target - today).days)
    return offs


def day_offsets(clause: str, today: date) -> set[int]:
    """这句话提到了哪几天（相对今天的偏移）。"""
    body = str(clause or "")
    offs = {off for word, off in _DAY_WORDS if word in body}
    offs |= _weekday_offsets(body, today)
    offs |= _day_of_month(body, today)
    return offs


def _is_plan(clause: str, today: date) -> bool:
    body = str(clause or "").strip()
    if len(body) < 4:
        return False
    if not any(mark in body for mark in _SUBJECTS):
        return False
    if 0 not in day_offsets(body, today):
        return False
    if any(word in body for word in _NEGATIONS):
        return False
    if body.endswith(("吗", "呢", "?")) or "？" in body:
        return False
    return any(verb in body for verb in _PLAN_VERBS)


def _clauses(text: str):
    """把一段话切成 (分句, 结束它的标点)；最后一句可能没有标点。"""
    parts = _CLAUSE_SPLIT.split(text)
    for index in range(0, len(parts), 2):
        clause = parts[index]
        sep = parts[index + 1] if index + 1 < len(parts) else ""
        yield clause, sep


def extract_plans(text: str, today: date, limit: int = MAX_PLANS) -> list[str]:
    """从一句话里抽出「她今天要办的事」：原话片段，按出现顺序，最多 limit 条。"""
    body = str(text or "")
    out: list[str] = []
    for raw, sep in _clauses(body):
        if sep in ("？", "?"):
            # 疑问句整句跳过：对方在问「你今天要去医院？」，不是在替她安排。
            continue
        clause = " ".join(raw.split())
        if not _is_plan(clause, today):
            continue
        clause = clause.strip(" \t\u3000，,、:：")
        if len(clause) > PLAN_MAX_CHARS:
            clause = clause[:PLAN_MAX_CHARS]
        if clause and clause not in out:
            out.append(clause)
        if len(out) >= max(1, int(limit)):
            break
    return out


def _today_str(today: date) -> str:
    return today.strftime("%Y-%m-%d")


def load_plans(scope: Any, today: date | str) -> list[str]:
    """读今天的计划（不是今天存的就当没有：昨天的事不该进今天的日程）。"""
    want = _today_str(today) if isinstance(today, date) else str(today or "")
    try:
        if str(scope.get_self(PLANS_DATE_KEY, "") or "") != want:
            return []
        stored = scope.get_self(PLANS_KEY, [])
    except Exception:
        return []
    if not isinstance(stored, list):
        return []
    return [str(item)[:PLAN_MAX_CHARS] for item in stored if str(item).strip()]


def note_plans(scope: Any, text: str, today: date) -> list[str]:
    """从这句话里抽计划并存下（只存当天）；返回这次新增的条目。"""
    found = extract_plans(text, today)
    if not found:
        return []
    existing = load_plans(scope, today)
    merged = list(dict.fromkeys(existing + found))[-MAX_PLANS:]
    try:
        scope.update_self(
            **{PLANS_KEY: merged, PLANS_DATE_KEY: _today_str(today)}
        )
        scope.mark_dirty()
    except Exception:
        return []
    return [item for item in found if item in merged]


def render_plans(items: Iterable[str]) -> str:
    """渲染成 prompt 里的缩进列表（空表返回空串，调用方连标题都不给）。"""
    rows = ["  - " + str(item) for item in items if str(item).strip()]
    return "\n".join(rows)
