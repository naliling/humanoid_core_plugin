"""角色管理器：管理所有 AI/机器人 的独立 Core 实例。"""

from __future__ import annotations

import asyncio
from typing import Any

from .core_instance import HumanoidCoreInstance
from .state import StateStore

LOG_PREFIX = "[humanoid_core]"


class RoleManager:
    """管理所有角色实例。"""

    def __init__(
        self,
        state_store: StateStore,
        config_provider,
        logger: Any,
        resolver,
        gateway,
        fetch_json=None,
    ):
        self._state_store = state_store
        self._config_provider = config_provider
        self._log = logger
        self._resolver = resolver
        self._gateway = gateway
        self._fetch_json = fetch_json
        self._instances: dict[str, HumanoidCoreInstance] = {}
        self._stop_event = asyncio.Event()

    def get_or_create(self, role_id: str) -> HumanoidCoreInstance:
        if role_id not in self._instances:
            instance = HumanoidCoreInstance(
                role_id=role_id,
                state_store=self._state_store,
                config_provider=self._config_provider,
                logger=self._log,
                stop_event=self._stop_event,
                resolver=self._resolver,
                gateway=self._gateway,
                fetch_json=self._fetch_json,
            )
            self._instances[role_id] = instance
            self._log.info(f"{LOG_PREFIX} 创建角色实例: {role_id}")
        return self._instances[role_id]

    def get_all(self) -> list[HumanoidCoreInstance]:
        return list(self._instances.values())

    async def start(self):
        self._stop_event.clear()
        for inst in self._instances.values():
            await inst.start()

    async def stop(self):
        self._stop_event.set()
        tasks = [inst.stop() for inst in self._instances.values()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._instances.clear()