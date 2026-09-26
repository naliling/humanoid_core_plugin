"""消息合并的判定内核：这一条消息说完了吗？

参考 `astrbot_plugin_debounce` 的整体做法（buffer 攒消息 → 判定完整性 → 合并成一次回复），
但**不用它的 BERT 模型**。它那套要 `onnxruntime + transformers + modelscope` 四个依赖、
运行时再从 ModelScope 拉模型（本机实测装这几个包跑了 5 分钟没装完，模型拉不到就静默失效）。
这里改成纯规则：零依赖、零延迟、没有"下不到模型就悄悄不防抖"这个失败模式。

判定分三档，够用就行：

* **确定说完** —— 句末有终结标点或语气词，且文本不短。直接放行。
* **肯定没说完** —— 明显是半句（结尾是逗号/顿号/省略号、或是"那个""就是""然后"这类悬空词）。
  继续等。
* **拿不准** —— 没有标点、长度中等。交给 `timeout` 或下一条消息来决定；多等的那条一到，
  就把它们一起当成一句发出去。

这样"用户打字中间停顿"不会被切开（停顿 3 秒也不会发），而"说完了没人再来"也不会被
永远卡住（到 `message_merge_timeout_seconds` 强制发）。

规则层的意图是**尽量少等**：宁可等超时那 0.5~1 秒，也不要把一句话切成两条分别回复。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Tuple

# 句末终结标点：出现任一个就认为这句话说完了。
_TERMINATORS = "。！？!?…；;~～"
# 句末语气词：中文口语里这些出现基本就是收尾了（「我今天好累啊」「你说得对呢」）。
_TAIL_PARTICLES = ("吧", "呢", "吗", "啊", "哦", "噢", "嘛", "啦", "咯", "喽", "哈", "喔", "呗")
# 结尾明显还没说完：逗号类停顿 + 省略号。
_MID_PAUSE = "，,、；;：:—-"
# 悬空收尾：说完这个词后面本来该有内容。
_DANGLING = (
    "那个", "这个", "就是", "然后", "因为", "所以", "但是", "如果", "要是",
    "我觉得", "你知道", "怎么说", "反正", "比如", "而且", "不过",
)
# 本身就是一句完整应答的短句（不能当成半句去等）。
_SHORT_REPLIES = frozenset({
    "好", "好的", "好呀", "好吧", "行", "行吧", "嗯", "嗯嗯", "嗯呐", "哦", "噢", "喔",
    "在", "在的", "知道", "知道了", "明白", "明白了", "收到", "谢谢", "谢了",
    "ok", "OK", "okay", "Okay", "yes", "no", "yes.", "OK.", "1", "6", "666", "233",
})

# 低于这个长度一律当作"还没说完"（哪怕有句号）——「嗯。」「哦。」不是在传达信息。
MIN_COMPLETE_CHARS = 4
# 到了这个长度还没标点，也算说完了：中文口语里这么长不带标点基本是长句而非半句。
UNPUNCTUATED_IS_COMPLETE_CHARS = 24
# 结尾是悬空词时，多长都不算说完。
_DANGLING_TAIL_CHARS = 6

_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    """把一条消息收拾干净：去首尾空白、合并连续空行与多个空格。"""
    return _WS.sub(" ", str(text or "")).strip()


@dataclass
class Verdict:
    """判定结果。`complete` 决定放不放行；`why` 只给日志看。

    `sure` 区分两种「说完了」：
    * `sure=True` —— 句末有终结标点/语气词，或已经足够长。这是真的说完了，立即发。
    * `sure=False` —— 只是「攒够 N 条了，按一句处理」。这不算数：用户可能正在一个字一个字地
      连发（「你」「今天」「我好累啊」），攒够两条就发等于把它切成两半。要再等一个短的
      确认窗口看看还有没有后续（见 `Debouncer.hold`）。
    """

    complete: bool
    why: str
    sure: bool = True


def judge(text: str, *, buffer_len: int = 0) -> Verdict:
    """判断 buffer 里的这些消息合起来算不算说完。

    `buffer_len` 是攒到的消息条数。已经有两条以上时，多半是用户连着补的半句
    （"今天真的好累" + "被你气到了"），这时不再追问完整不完整——它们本来就是一句，
    但也只是「凑够了」，所以 `sure=False`，交给确认窗口再拍一次板。
    """
    body = normalize(text)
    if not body:
        return Verdict(False, "空消息", True)

    if buffer_len >= 2:
        return Verdict(True, f"已攒 {buffer_len} 条，按一句处理", False)

    tail = body[-1]

    if tail in _TERMINATORS:
        return Verdict(True, "句末终结标点")

    if body.endswith(_TAIL_PARTICLES):
        return Verdict(True, "句末语气词")

    # 短应答：「好的」「嗯嗯」「在」这类本身就是一句完整的应答。长度检查放在尾词判定**之后**，
    # 否则「在吗」这种两三个字但有收尾词的消息会被当成半句，白等一个超时。
    if body in _SHORT_REPLIES:
        return Verdict(True, "短应答")

    if len(body) < MIN_COMPLETE_CHARS:
        return Verdict(False, "太短，还不像一句话")

    # 省略号结尾：中文里"嗯……""算了……"常常就是句末，但也可能是话没说完。
    # 一律当作没说完，交给超时——宁可多等一拍。
    if body.endswith("…") or body.endswith("..."):
        return Verdict(False, "省略号收尾，可能还没说完")

    if tail in _MID_PAUSE:
        return Verdict(False, "停在停顿标点上")

    head = body[-_DANGLING_TAIL_CHARS:] if len(body) >= _DANGLING_TAIL_CHARS else body
    if any(head.endswith(word) for word in _DANGLING):
        return Verdict(False, "以悬空词收尾")

    if len(body) >= UNPUNCTUATED_IS_COMPLETE_CHARS:
        return Verdict(True, "够长且没有停顿痕迹")

    return Verdict(False, "无标点且不长，等下一条或超时")


def join(messages: List[str], *, limit: int = 6) -> str:
    """把 buffer 里的几条原话拼成一条发出去。

    按原样用空格连接，不加分隔符：模型看到的是用户连着打的那几句话本身。
    """
    limit = max(1, int(limit))
    parts = [normalize(m) for m in (messages or []) if normalize(m)]
    if not parts:
        return ""
    return " ".join(parts[-limit:])
