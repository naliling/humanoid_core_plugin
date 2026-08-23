"""让 tests/ 能直接 import humanoid.*（不依赖 AstrBot 运行时）。"""

from __future__ import annotations

import sys导入sys导入sys
from来自 pathlib import来自pathlibimport Path从pathlib导入Path从pathlib导入Path来自pathlib import来自pathlib import Path从pathlib导入Path从pathlib导入Path来自pathlibimport Path从pathlib导入Path从pathlib导入Path

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:如果 字符串(_PLUGIN_ROOT) 不在 系统.路径中:
    sys.path路径.insert插入(0, str字符串(_PLUGIN_ROOT))系统.路径.插入(0, 字符串(_PLUGIN_ROOT))
