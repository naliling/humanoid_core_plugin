"""编排层兼容层：保持与旧版接口兼容，同时路由到 RoleManager。"""

from __future__ import annotations

import json
import re
from typing import Any

from . import __version__
from .clock import lookup_city_time
from .config import ConfigBox, HumanoidConfig
from .diagnostics import build_report
from .role_manager import RoleManager

LOG_PREFIX = "[humanoid_core]"


class HumanoidEngine:
    """兼容层，保持旧版接口可用。"""

    def __init__(
        self,
        context: Any,
        config: Any,
        data_dir: str,
        logger: Any,
        fetch_json: Any,
        role_manager: RoleManager,
    ) -> None:
        self._context = context
        self._log = logger
        self._fetch_json = fetch_json
        self.role_manager = role_manager
        # 统一通过 ConfigBox 读：传入 callable 也可以，取一次固定下来。
        if callable(config):
            self._config_provider = config
        elif isinstance(config, HumanoidConfig):
            self._config_provider = lambda: config
        else:
            box = ConfigBox(config)
            self._config_provider = box

    @property
    def config(self) -> HumanoidConfig:
        return self._config_provider()

    def get_config(self) -> HumanoidConfig:
        return self.config

    def reload_config(self, raw_config: Any = None) -> HumanoidConfig:
        provider = self._config_provider
        if isinstance(provider, ConfigBox):
            return provider.reload(raw_config)
        self._log.info(f"{LOG_PREFIX} 配置由外部持有，本次重载未生效")
        return self.config

    def environment_allows(self, is_private: bool) -> bool:
        mode = self.config.environment_mode
        if mode == "private":
            return is_private
        if mode == "group":
            return not is_private
        return True

    def is_admin(self, sender_id: str, astrbot_admin: bool = False) -> bool:
        return bool(astrbot_admin) or self.config.is_admin(sender_id)

    def city_time_text(self, city: str) -> str | None:
        result = lookup_city_time(city)
        if result is None:
            return None
        text = f"📍 {result.display_city} 当前时间: {result.text}（星期{result.weekday}）"
        if result.note:
            text += f"\n⚠ {result.note}"
        return text

    def diagnostics_text(self, core=None) -> str:
        if core is None:
            instances = self.role_manager.get_all()
            core = instances[0] if instances else None
        if core is None:
            return "没有活跃的角色实例。"

        resolver = getattr(core, "resolver", None)
        gateway = getattr(core, "gateway", None)
        if resolver is None or gateway is None:
            return "诊断信息暂不可用。"

        # 获取过程状态
        process_status = core.process.current() if hasattr(core, "process") else {}
        body_status = None
        if hasattr(core, "soma"):
            target, at = core.signals.last_proactive()
            now = core.soma.now
            body_status = {
                "soma": core.soma.snapshot(),
                "energy": float(core.energy.energy),
                "cycle_day": core.energy.cycle_day,
                "contract": core._scope.get_self("contract"),
                "biological_night": getattr(core.soma, "biological_night", lambda: None)(),
                "signals": {
                    "found": bool(at > 0),
                    "last_proactive_age": max(0.0, now - at) if at > 0 else -1.0,
                    "last_target_uid": target,
                    "ignored_streak": core.signals.ignored_streak(),
                    # 对接到底卡在哪一步（没装 / 没写过 / 过期 / 在用）与它去看的路径。
                    **{k: v for k, v in core.signals.status().items() if k != "payload"},
                },
            }

        return build_report(
            cfg=self.config,
            resolver=resolver,
            gateway=gateway,
            schedule_status=core.schedule.status(),
            process_status=process_status,
            body_status=body_status,
            inject_estimate=self._inject_estimate(core),
            zone_status=self._zone_status(core),
            version=__version__,
        )

    @staticmethod
    def _zone_status(core) -> dict:
        clock = getattr(core, "clock", None)
        state = getattr(clock, "zone_state", None)
        if state is None:
            return {}
        try:
            return {"zone": state(), "moment": clock.now()}
        except Exception:
            return {}

    @staticmethod
    def _inject_estimate(core) -> dict:
        """量一下当前角色实际会追加多大的上下文——拿真状态算，不拿常量猜。"""
        from .prompt_builder import estimate_tokens

        try:
            text = core.build_injection("status-probe", is_group=False)
        except Exception:
            return {}
        return {
            f"{core.config.inject_activity_context}（实测）": estimate_tokens(text),
            "Token 预算": int(core.config.inject_token_budget),
        }

    def parse_affection_batch(self, raw: str) -> tuple[list[tuple[str, float]], int]:
        """解析批量导入：返回（可用的 QQ-数值对, 一眼就看错了多少条）。

        坏条目单独报数而不是静默丢掉：有人粘了一整片文本进去，一句「已设置 1 个」
        会把「其余根本没被认出来」一并掩盖掉。"""
        text = (raw or "").strip()
        if not text:
            return [], 0
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                out: list[tuple[str, float]] = []
                bad = 0
                for item in parsed:
                    if isinstance(item, dict) and "qq" in item and "value" in item:
                        try:
                            out.append((str(item["qq"]).strip(), float(item["value"])))
                            continue
                        except (TypeError, ValueError):
                            pass
                    bad += 1
                if out:
                    return out, bad
        except (ValueError, TypeError):
            pass

        pairs: list[tuple[str, float]] = []
        bad = 0
        for part in re.split(r"[,，\s]+", text):
            if not part:
                continue
            if ":" not in part and "：" not in part:
                bad += 1
                continue
            key, _, value = part.replace("：", ":").partition(":")
            try:
                pairs.append((key.strip(), float(value.strip())))
            except (TypeError, ValueError):
                bad += 1
        return pairs, bad

    def reset_state(self):
        return 80.0, 100.0, 1