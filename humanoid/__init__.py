"""humanoid_core 的业务内核。

设计约束：本包**不 import 任何 astrbot 模块**。框架对象（Context、Provider、事件）
一律以鸭子类型的形式由 main.py 注入，从而整包可以脱离 AstrBot 做单元测试。
"""

from __future__ import annotations

__version__ = "2.11.6"

LOG_PREFIX = "[humanoid_core]"

__all__ = ["LOG_PREFIX", "__version__"]
