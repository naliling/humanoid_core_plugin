"""角色管理器：管理所有 AI/机器人 的独立 Core 实例。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .core_instance import HumanoidCoreInstance
from .state import StateStore

LOG_PREFIX = "[humanoid_core]"

#: 启动失败后隔多久再试一次。太短会在「日程服务还没就绪」时反复重建循环，
#: 太长则一个偶发失败要等一整个间隔。
RETRY_SECONDS = 30.0


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
        # 启动失败的实例留在 _instances 里（消息还得能用它读状态），但后台循环没跑：
        # 这里记下失败时刻，下一次 get_or_create 在退避期后重试 start()。
        self._failed_at: dict[str, float] = {}

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
                self._try_start(role_id)
        elif role_id in self._failed_at:
            # 上一次启动失败了：实例还在缓存里（消息仍能读到她的状态），但后台循环
            # 一个都没跑。退避期过后重试——**不清实例**，调用方不需要处理 None。
            # 原来这里什么都不做：失败一次之后这个角色永远不再尝试启动，
            # 日程/身体/过程循环直到插件重载都不会跑，日志里也只有一条 warning。
            if time.monotonic() - self._failed_at[role_id] >= RETRY_SECONDS:
                del self._failed_at[role_id]
                self._try_start(role_id)
        return self._instances[role_id]

    def _try_start(self, role_id: str) -> None:
        """起一个角色的启动任务（失败会被记进 _failed_at，等下次重试）。"""
        instance = self._instances.get(role_id)
        if instance is None:  # pragma: no cover - 只在 stop() 与 get_or_create 交错时发生
            return
        task = asyncio.create_task(
            instance.start(),
            name=f"humanoid-role-start-{role_id}",
        )
        self._start_tasks.add(task)
        task.add_done_callback(lambda done, rid=role_id: self._on_start_task_done(done, rid))

    def failed_roles(self) -> dict[str, float]:
        """启动失败、正在等重试的角色（诊断页用）：{role_id: 失败时刻(monotonic)}。"""
        return dict(self._failed_at)

    def _on_start_task_done(self, task: asyncio.Task, role_id: str | None = None) -> None:
        """启动任务退出时读一次结局。

        原来只挂 `self._start_tasks.discard`：`instance.start()` 抛出的任何异常都没人
        取用，于是「新角色起来了」和「根本起不来」在日志里长得一模一样。
        """
        self._start_tasks.discard(task)
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:  # pragma: no cover - 竞态：取消落在 cancelled() 之后
            return
        if exc is not None:
            self._log.warning(
                f"{LOG_PREFIX} 角色启动任务 {task.get_name()} 异常退出: {exc!r}"
            )
            if role_id is not None:
                self._failed_at[role_id] = time.monotonic()
            return
        try:
            started = task.result()
        except Exception:  # pragma: no cover - 上面已经取过 exception()
            return
        if started is False:
            # `HumanoidCoreInstance.start()` 在已收到停止信号时明确返回 False。
            self._log.warning(
                f"{LOG_PREFIX} 角色启动任务 {task.get_name()} 未能启动（已收到停止信号）"
            )
            if role_id is not None:
                self._failed_at[role_id] = time.monotonic()
            return
        if role_id is not None:
            self._failed_at.pop(role_id, None)

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
        # 实例都没了，失败记录也没意义：留着会让下次 get_or_create 误以为要退避重试。
        self._failed_at.clear()
        # 「正在启动」的角色任务不在 `_instances` 的遍历范围里，必须单独取消并 await，
        # 否则 stop() 返回后它们还在启动。
        starting = [t for t in self._start_tasks if not t.done()]
        for task in starting:
            task.cancel()
        if starting:
            results = await asyncio.gather(*starting, return_exceptions=True)
            # `return_exceptions=True` 是为了不让一个角色的失败拖住其余角色，但**不能
            # 顺手把异常丢掉**：停止阶段出的错（写盘失败、取消超时）本该有人知道。
            for task, result in zip(starting, results):
                if isinstance(result, BaseException) and not isinstance(
                    result, asyncio.CancelledError
                ):
                    self._log.warning(
                        f"{LOG_PREFIX} 取消启动任务 {task.get_name()} 时出错: {result!r}"
                    )
        self._start_tasks.clear()
        if pending:
            results = await asyncio.gather(
                *[inst.stop() for inst in pending], return_exceptions=True
            )
            for inst, result in zip(pending, results):
                if isinstance(result, BaseException) and not isinstance(
                    result, asyncio.CancelledError
                ):
                    self._log.warning(
                        f"{LOG_PREFIX} 角色 {inst.role_id} 停止时出错: {result!r}"
                    )
        # 角色停了就不该还有未落盘的状态。调用方随后还会 stop 一次 StateStore，
        # 多这一次 flush 代价为零，却保证单独用 RoleManager 的代码也不丢数据。
        await self._state_store.flush()