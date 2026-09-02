"""humanoid_core 的业务内核。

v3 变更：支持多角色独立 Core 状态 + 过程系统。
"""

from __future__ import annotations

__version__ = "2.12.1"

LOG_PREFIX = "[humanoid_core]"

from .core_instance import HumanoidCoreInstance
from .role_manager import RoleManager
from .role_scope import RoleScope
from .services.process import ProcessService

__all__ = [
    "LOG_PREFIX",
    "__version__",
    "HumanoidCoreInstance",
    "RoleManager",
    "RoleScope",
    "ProcessService",
]
