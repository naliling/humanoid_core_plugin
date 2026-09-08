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
        self._started = False
        self._start_tasks: set[asyncio.Task] = set()

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
            # AstrBot 启动插件时可能尚未出现任何消息，因此 start() 运行时
            # 实例列表为空。后续首次收到消息创建角色后，也必须启动该角色的
            # 日程/过程/天气/社交后台循环，否则这些功能会“看起来存在但不运行”。
            if self._started:
                task = asyncio.create_task(
                    instance.start(),
                    name=f"humanoid-role-start-{role_id}",
                )
                self._start_tasks.add(task)
                task.add_done_callback(self._start_tasks.discard)
        return self._instances[role_id]

    def get_all(self) -> list[HumanoidCoreInstance]:
        return list(self._instances.values())

    async def start(self):
        self._stop_event.clear()
        self._started = True
        for inst in self._instances.values():
            await inst.start()

    async def stop(self):
        self._stop_event.set()
        tasks = [inst.stop() for inst in self._instances.values()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._instances.clear()