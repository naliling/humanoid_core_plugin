"""角色专属素材 - 按「她是谁」写出来的一柜子事（v3.0.10）。

内置素材池（life_material.MATERIALS）解决的是「谁过日子都可能做的那些事」：洗衣服、
下楼取快递、把晒干的衣服收进来。它让日程不再只剩睡和发呆，但它不知道她是谁——人设里
写着「她是插画师，养了只橘猫，这周在赶稿」，这三件事内置池里一件都没有。于是模型手里
永远只有通用条目，不同角色的一天看起来差不多。

这个模块补的是这一层：拿人设原文问一次模型，让它按这个人写出 20-30 条「她这样的人
这个钟点可能做的事」，带人设指纹存进 state。人设没变就不再问第二次——它不是每段日程
都要跑的东西，一个角色一生只跑一两次。

三条硬规矩：

1. **失败一律退回内置池**。素材是锦上添花，不能因为它让日程停摆：模型不可用、返回
   的不是 JSON、条数太少，都按「没有专属素材」处理，下一段照常用内置池生成。
2. **不许替她编身份**。生成的条目里如果标了 needs（工作/宠物/照护/社交）或 interest，
   而人设原文里根本没有那种生活条件，这条直接丢掉——这条教训在范例池和内置池里各踩过
   一次（给没有猫的角色递「给猫添粮」）。
3. **素材不是清单**。它只说明「她这样的人可能做这些」，选哪件、做多久还是模型结合身体
   与处境决定；这一点由 schedule.segment_prompt 的措辞保证，不在这里重复。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

from .life_material import capabilities, interests_in
from ..species import BODY_HOOF, BODY_PAW, BODY_SCALE, profile_body

# state 键：条目、人设指纹、上次尝试时间（失败退避用）
ROLE_MATERIALS_KEY = "role_materials"
ROLE_MATERIALS_FP_KEY = "role_materials_fp"
ROLE_MATERIALS_TRY_KEY = "role_materials_try"
# 上次尝试是冲着哪一版人设去的。只记时间不够用：人设变了要立刻重建，
# 而「这一版人设刚失败过」才该退避——两者靠这个键区分。
ROLE_MATERIALS_TRY_FP_KEY = "role_materials_try_fp"

ROLE_MATERIALS_COUNT = 24
ROLE_MATERIALS_MIN = 8
ROLE_MATERIALS_RETRY_SECONDS = 1800.0

ROLE_BANDS = ("life", "interest", "duty", "social", "rest", "rare")
ROLE_SLOTS = ("凌晨", "早上", "上午", "中午", "下午", "傍晚", "夜里")
ROLE_NEEDS = ("job", "pet", "care", "social")
ROLE_INTERESTS = (
    "draw", "music", "read", "game", "sport", "craft",
    "cook", "plant", "film", "write", "photo",
)

EVENT_MAX = 40
LOCATION_MAX = 12
TOPIC_MAX = 8
MINUTES_LOW = 5
MINUTES_HIGH = 120


def _clean(text: Any) -> str:
    """压掉换行与多余空白：素材会进 prompt，一条里带换行就把列表结构撑坏了。"""
    return " ".join(str(text or "").split())


#: 身体做不到的事（v3.1.38）。内置池和「按人设生成」的池都默认她是人：四蹄角色
#: 于是会拿到「整理衣柜，把不穿的收起来」「下楼把快递取上来」。这些不是人形部位，
#: 词表挡不住——只能在她**生成素材之前**就把身体条件写进去。
_BODY_LIMITS = {
    BODY_HOOF: (
        "她是四蹄动物：没有手，不穿衣服，做事靠嘴、蹄子和身体。"
        "不能写打字、写字、洗碗、系鞋带、化妆、梳头、穿脱衣服、拿筷子这类需要手指的事；"
        "也不能写熨衣服、缝扣子这种只有人才能做的居家活。"
    ),
    BODY_PAW: (
        "她是兽形：有爪没有手，不穿衣服。不能写需要手指的精细动作"
        "（打字、写字、系鞋带、化妆、穿脱衣服、拿筷子），也不写熨衣服、缝扣子这类居家活。"
    ),
    BODY_SCALE: (
        "她是鳞甲兽形：有爪、可能还有翼，不穿衣服，身上是鳞片不是皮毛。"
        "不能写需要手指的精细动作与穿衣打扮，也不要写「皮毛」「绒毛」「毛发」这类说法。"
    ),
}


def _body_of(species: Any) -> str:
    """物种参数（kind 字符串或整份物种档案）→ 身体类型；认不出按人形。"""
    if isinstance(species, Mapping):
        return profile_body(dict(species))
    return profile_body({"kind": str(species or "")})


def body_limits(species: Any) -> str:
    """她这个身体做不到什么；人形返回空串（人形不需要额外约束）。"""
    return _BODY_LIMITS.get(_body_of(species), "")


def persona_fingerprint(text: str, species: Any = None) -> str:
    """人设指纹：人设正文变了才重写素材（换了措辞也算变，宁可多问一次）。

    v3.1.38：身体类型也进指纹。面板把 `ccb_body` 从人形改成四蹄、或识别层把人鱼
    从爪改成手，旧素材整批作废重问——否则「整理衣柜」会一直留在她柜子里。
    """
    body = _clean(text)
    if not body:
        return ""
    body = f"{body}\x1f{_body_of(species)}"
    return hashlib.sha1(body.encode("utf-8")).hexdigest()[:16]


def build_prompt(
    persona_text: str, count: int = ROLE_MATERIALS_COUNT, species: Any = None
) -> str:
    """问模型要一张「她这样的人可能做的事」的表。"""
    body = str(persona_text or "").strip()
    limits = body_limits(species)
    rows = [
        "你在为一个角色写「她这样的人，一天里可能做的事」的素材表。",
        "",
        "【她是谁】",
        body,
        "",
    ]
    if limits:
        rows += [
            "【她的身体】",
            limits,
            "她做这件事的方式必须符合上面这段；做不到的事不要写。",
            "",
        ]
    rows += [
        "要求：",
        "1. 写 " + str(int(count)) + " 条，每条是她这个人在某个时段真可能做的一件事。",
        "2. 只写人设里确实有依据的事。人设里没有工作、没有宠物、没有家人、没有朋友，",
        "   就不要写上班、喂猫、接孩子、约朋友——那不是补充细节，是替她编一个身份。",
        "3. 事件要具体（在做什么、在哪），不要写「休息」「处理事情」这种空话；",
        "   一件事只占 5~120 分钟，别把半天塞进一条。",
        "4. 时段用这几个词：" + "、".join(ROLE_SLOTS) + "。",
        "5. 每条给出：event（20 字以内）、location（8 字以内）、minutes（[最短, 最长]，5~120）、",
        "   slots（她可能做这件事的时段）、band（" + "/".join(ROLE_BANDS) + " 之一）、",
        "   needs（" + "/".join(ROLE_NEEDS) + " 里她确实有的，没有就空数组）、",
        "   interest（" + "/".join(ROLE_INTERESTS) + " 之一，没有就空串）、",
        "   topic（2~4 字的题眼，用来判断今天做过没有）。",
        "6. 不要写睡觉、不要写性、不要写医疗诊断。",
        "",
        "只输出 JSON 数组，不要解释，不要代码块：",
        '[{"event": "...", "location": "...", "minutes": [20, 40], "slots": ["上午"], '
        '"band": "life", "needs": [], "interest": "", "topic": "..."}]',
    ]
    return "\n".join(rows)


def _json_body(raw: str) -> Any:
    """从模型回复里抠出 JSON：容忍代码块围栏、前后的解释句。"""
    text = str(raw or "").strip()
    if not text:
        return None
    fence = text.find("```")
    if fence >= 0:
        text = text[fence + 3:]
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
        end = text.find("```")
        if end >= 0:
            text = text[:end]
    start = text.find("[")
    stop = text.rfind("]")
    if start < 0 or stop <= start:
        return None
    try:
        return json.loads(text[start:stop + 1])
    except (TypeError, ValueError):
        return None


def _int_pair(raw: Any) -> tuple[int, int]:
    """时长区间：坏值退回一条短事的默认值，越界一律夹回 5~120。"""
    lo = hi = 0
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        try:
            lo, hi = int(raw[0]), int(raw[1])
        except (TypeError, ValueError):
            lo = hi = 0
    elif isinstance(raw, (int, float)):
        lo = hi = int(raw)
    if lo <= 0 and hi <= 0:
        return (15, 30)
    lo = max(MINUTES_LOW, min(MINUTES_HIGH, lo))
    hi = max(MINUTES_LOW, min(MINUTES_HIGH, hi))
    if hi < lo:
        lo, hi = hi, lo
    return (lo, hi)


def _slots(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str):
        raw = [raw]
    picked = [str(x).strip() for x in (raw or ()) if str(x).strip() in ROLE_SLOTS]
    # 一条素材没有任何有效时段 = 它永远发不出来，等于没写：按白天处理。
    return tuple(dict.fromkeys(picked)) or ("早上", "上午", "中午", "下午", "傍晚")


def _needs(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str):
        raw = [raw]
    return tuple(dict.fromkeys(
        str(x).strip() for x in (raw or ()) if str(x).strip() in ROLE_NEEDS
    ))


def _item(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    event = _clean(raw.get("event"))[:EVENT_MAX]
    if len(event) < 2:
        return None
    location = _clean(raw.get("location"))[:LOCATION_MAX] or "家里"
    band = str(raw.get("band") or "").strip().lower()
    if band not in ROLE_BANDS:
        band = "life"
    interest = str(raw.get("interest") or "").strip().lower()
    if interest not in ROLE_INTERESTS:
        interest = ""
    return {
        "event": event,
        "location": location,
        "minutes": _int_pair(raw.get("minutes")),
        "slots": _slots(raw.get("slots")),
        "topic": _clean(raw.get("topic"))[:TOPIC_MAX],
        "band": band,
        "needs": _needs(raw.get("needs")),
        "interest": interest,
        "wants": band == "rest",
    }


def parse_materials(raw: str, count: int = ROLE_MATERIALS_COUNT) -> list[dict[str, Any]]:
    """把模型回复解析成素材条目：坏条目单条丢弃，不因为一条坏的就整批作废。"""
    body = _json_body(raw)
    if isinstance(body, Mapping):
        for key in ("materials", "items", "list", "data"):
            if isinstance(body.get(key), list):
                body = body[key]
                break
    if not isinstance(body, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in body:
        item = _item(entry)
        if item is None:
            continue
        key = item["event"]
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= max(1, int(count)):
            break
    return out


def accepted(items: Sequence[Mapping[str, Any]], persona_text: str) -> list[dict[str, Any]]:
    """滤掉人设里没有依据的身份条件（工作/宠物/照护/社交/兴趣）。"""
    caps = capabilities(persona_text)
    interests = interests_in(persona_text)
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        needs = item.get("needs") or ()
        if isinstance(needs, str):
            needs = (needs,)
        if any(str(need) not in caps for need in needs):
            continue
        interest = str(item.get("interest") or "")
        if interest and interest not in interests:
            continue
        out.append(dict(item))
    return out


def load(scope: Any) -> list[dict[str, Any]]:
    """读出已存的专属素材（坏值一律当空：一份坏存档不该让日程报错）。"""
    try:
        stored = scope.get_self(ROLE_MATERIALS_KEY, [])
    except Exception:
        return []
    if not isinstance(stored, list):
        return []
    out = []
    for entry in stored:
        item = _item(entry)
        if item is not None:
            out.append(item)
    return out


def stored_fingerprint(scope: Any) -> str:
    try:
        return str(scope.get_self(ROLE_MATERIALS_FP_KEY, "") or "")
    except Exception:
        return ""


def store(scope: Any, fingerprint: str, items: Sequence[Mapping[str, Any]]) -> None:
    """存素材与指纹；写不进去就静默放弃（下次照常退回内置池）。"""
    try:
        scope.update_self(
            **{
                ROLE_MATERIALS_KEY: [dict(item) for item in items],
                ROLE_MATERIALS_FP_KEY: str(fingerprint or ""),
            }
        )
        scope.mark_dirty()
    except Exception:
        pass


def last_try(scope: Any) -> float:
    try:
        return float(scope.get_self(ROLE_MATERIALS_TRY_KEY, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def last_try_fingerprint(scope: Any) -> str:
    try:
        return str(scope.get_self(ROLE_MATERIALS_TRY_FP_KEY, "") or "")
    except Exception:
        return ""


def mark_try(scope: Any, now_ts: float, fingerprint: str = "") -> None:
    try:
        scope.update_self(
            **{
                ROLE_MATERIALS_TRY_KEY: float(now_ts),
                ROLE_MATERIALS_TRY_FP_KEY: str(fingerprint or ""),
            }
        )
        scope.mark_dirty()
    except Exception:
        pass


def needs_build(
    scope: Any,
    fingerprint: str,
    now_ts: float,
    retry_seconds: float = ROLE_MATERIALS_RETRY_SECONDS,
) -> bool:
    """现在该不该问模型要素材：还没有、人设变了、或者上次失败已过退避期。

    退避只对「同一版人设刚失败过」生效。人设换了就立刻重建：原来这里不看
    「上次是冲着哪版人设试的」，于是换完人设还要等满 30 分钟，这半小时里她一直
    用着上一个人设的素材。
    """
    want = str(fingerprint or "")
    if not want:
        return False
    cached = load(scope)
    if cached and stored_fingerprint(scope) == want:
        return False
    same_persona_tried = last_try_fingerprint(scope) == want
    if same_persona_tried and (float(now_ts) - last_try(scope)) < float(retry_seconds):
        return False
    return True

