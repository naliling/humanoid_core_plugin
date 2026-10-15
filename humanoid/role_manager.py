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
        data_root=None,
        persona_source=None,
    ):
        self._state_store = state_store
        self._config_provider = config_provider
        self._log = logger
        self._resolver = resolver
        self._gateway = gateway
        self._fetch_json = fetch_json
        self._data_root = data_root
        self._persona_source = persona_source
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
                data_root=self._data_root,
                persona_source=self._persona_source,
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
        """停掉所有角色实例。

        顺序有讲究，**先落状态再 await**：`get_or_create` 是同步的，只可能在这些
        await 之间插进来。只要它看到 `_started` 为假，就不会再 create_task 启动新实例——
        否则那个实例既不在被停的名单里、又已经跑起了 5 个后台循环，会一直写 state.json
        （实测过的卸载竞态）。
        """
        self._started = False
        self._stop_event.set()
        pending = list(self._instances.values())
        self._instances.clear()
        # 「正在启动」的角色任务不在 `_instances` 的遍历范围里，必须单独取消并 await，
        # 否则 stop() 返回后它们还在启动。
        starting = [t for t in self._start_tasks if not t.done()]
        for task in starting:
            task.cancel()
        if starting:
            await asyncio.gather(*starting, return_exceptions=True)
        self._start_tasks.clear()
        if pending:
            await asyncio.gather(
                *[inst.stop() for inst in pending], return_exceptions=True
            )
        # 角色停了就不该还有未落盘的状态。调用方随后还会 stop 一次 StateStore，
        # 多这一次 flush 代价为零，却保证单独用 RoleManager 的代码也不丢数据。
        await self._state_store.flush()