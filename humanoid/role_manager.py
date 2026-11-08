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

#: 巡检间隔：多久查一次「该起的角色没起」「循环死了没」。
#: 取和 RETRY_SECONDS 一样的值，让失败重试最迟半分钟一次。
SUPERVISE_SECONDS = 30.0


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
        # 巡检协程：进程活着、插件也活着，但角色循环悄悄死掉时，只有它能兜住。
        self._supervisor: asyncio.Task | None = None

    def _new_instance(self, role_id: str) -> HumanoidCoreInstance:
        """建一个角色实例并放进缓存（**不启动**）。"""
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
        return instance

    def _known_role_ids(self) -> list[str]:
        """状态里出现过的角色 id（冷启动预热用）。"""
        try:
            roles = (self._state_store.data or {}).get("roles") or {}
        except Exception:
            return []
        ids: list[str] = []
        for rid in roles:
            rid = str(rid or "")
            if rid and rid not in ids:
                ids.append(rid)
        return ids

    def get_or_create(self, role_id: str) -> HumanoidCoreInstance:
        if role_id not in self._instances:
            self._new_instance(role_id)
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

    async def _supervise(self) -> None:
        """定期巡检：该起没起的角色重试启动，死掉的后台循环重新拉起来。

        原来这两件事都只发生在「恰好又收到这个角色的消息」时：启动失败要等
        下一条消息才重试，循环异常退出则**永远**不重启。进程活着、插件活着、
        状态文件正常，但没有任何循环在跑——而且没有任何东西会把它修好。
        """
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=SUPERVISE_SECONDS)
                return
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise
            try:
                self._sweep()
            except Exception as exc:
                self._log.warning(f"{LOG_PREFIX} 角色巡检出错: {exc!r}")

    def _sweep(self) -> None:
        """跑一轮巡检（同步、不 await：只做 create_task 级别的重启）。"""
        if not self._started:
            return
        now = time.monotonic()
        for role_id, failed_at in list(self._failed_at.items()):
            if now - failed_at < RETRY_SECONDS:
                continue
            if role_id not in self._instances:
                self._failed_at.pop(role_id, None)
                continue
            self._failed_at.pop(role_id, None)
            self._log.info(f"{LOG_PREFIX} 重试启动角色 {role_id}")
            self._try_start(role_id)
        for role_id, instance in list(self._instances.items()):
            try:
                restarted = instance.restart_dead_loops()
            except Exception as exc:
                self._log.warning(
                    f"{LOG_PREFIX} 角色 {role_id} 检查后台循环时出错: {exc!r}"
                )
                continue
            if restarted:
                self._log.warning(
                    f"{LOG_PREFIX} 角色 {role_id} 的后台循环异常退出，已重新拉起: "
                    + "、".join(restarted)
                )

    async def start(self):
        self._stop_event.clear()
        self._started = True
        # 冷启动预热（v3.1.12）：后台循环原来只有一个启动入口——「收到该角色的消息」
        # （`main._core` → `get_or_create`）。插件加载时 `_instances` 是空的，于是
        # 重启之后只要没人在这几个角色下说过话，她的日程/身体/过程/天气/社交循环
        # 一个都不会跑：`last_tick` 冻在关机那一刻，`today_date` 停在前天，
        # `schedule_source` 永远是 template。实测线上一个角色就这样静了 4 天，
        # 还被误判成「一直在睡」。状态里出现过的角色都是真实在用的角色，加载时
        # 就该把循环起起来，而不是等人来敲一下。
        for role_id in self._known_role_ids():
            if role_id not in self._instances:
                self._new_instance(role_id)
        for inst in list(self._instances.values()):
            try:
                await inst.start()
            except Exception as exc:
                # 一个角色起不来不该拖垮整排角色：记下失败时刻交给巡检重试，
                # 其余角色照常跑。（原来这里直接上抛，`initialize()` 整体回滚，
                # 于是所有角色一起不跑。）
                self._failed_at[str(inst.role_id)] = time.monotonic()
                self._log.warning(
                    f"{LOG_PREFIX} 角色 {inst.role_id} 启动失败，稍后重试: {exc!r}"
                )
        if self._supervisor is None or self._supervisor.done():
            self._supervisor = asyncio.create_task(
                self._supervise(), name="humanoid-role-supervisor"
            )

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
        # 巡检协程必须在这里停：它还持有 `_instances` 的引用，晚一步就可能对着
        # 正在被停的实例拉新循环。
        if self._supervisor is not None:
            self._supervisor.cancel()
            try:
                await self._supervisor
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                self._log.warning(f"{LOG_PREFIX} 角色巡检任务退出时出错: {exc!r}")
            self._supervisor = None
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