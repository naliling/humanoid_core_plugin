"""让 tests/ 能直接 import humanoid.*（不依赖 AstrBot 运行时）。"""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

# 业务模块的 logger 一律 `from astrbot.api import logger`（插件上架规则：logger 必须且
# 只能从 astrbot.api 取，严禁内置 logging），所以测试进程里必须**先**把 astrbot 的最小
# 替身装进 sys.modules：否则 import humanoid.* 会在第一行就 ModuleNotFoundError。
# 装了之后，这些模块走的就是与真实 AstrBot 完全相同的那条 import 路径。
from .astrbot_stub import install as _install_astrbot_stub  # noqa: E402

_install_astrbot_stub()
