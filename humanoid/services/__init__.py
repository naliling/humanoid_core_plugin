"""各领域服务。

刻意不做 eager re-export：每个服务都从自己的模块里导入
（`from .services.schedule import ScheduleService`），避免任何一个服务的导入错误
连带拖垮整包。
"""

from __future__ import annotations

__all__: list[str] = []
