"""编排层兼容层：保持与旧版接口兼容，同时路由到 RoleManager。"""

from __future__ import annotations

import json
import re
from typing import Any

from . import __version__
from .clock import lookup_city_time
from .config import HumanoidConfig
from .diagnostics import build_report
from .role_manager import RoleManager

LOG_PREFIX = "[humanoid_core]"


class HumanoidEngine:
    """兼容层，保持旧版接口可用。"""

    def __init__(
        self,
        context: Any,
        raw_config: Any,
        data_dir: str,
        logger: Any,
        fetch_json: Any,
        role_manager: RoleManager,
    ) -> None:
        self._context = context
        self._raw_config = raw_config
        self._log = logger
        self._fetch_json = fetch_json
        self.role_manager = role_manager

    @property
    def config(self) -> HumanoidConfig:
        return HumanoidConfig.from_raw(self._raw_config)

    def get_config(self) -> HumanoidConfig:
        return self.config

    @property
    def config_version(self) -> int:
        return 1

    def reload_config(self, raw_config: Any = None) -> HumanoidConfig:
        if raw_config is not None:
            self._raw_config = raw_config
        self._log.info(f"{LOG_PREFIX} 配置已重载")
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
        return f"📍 {result.display_city} 当前时间: {result.text}（星期{result.weekday}）"

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

        return build_report(
            cfg=self.config,
            resolver=resolver,
            gateway=gateway,
            schedule_status=core.schedule.status(),
            process_status=process_status,
            version=__version__,
        )

    def parse_affection_batch(self, raw: str) -> list[tuple[str, float]]:
        text = (raw or "").strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                out: list[tuple[str, float]] = []
                for item in parsed:
                    if isinstance(item, dict) and "qq" in item and "value" in item:
                        try:
                            out.append((str(item["qq"]).strip(), float(item["value"])))
                        except (TypeError, ValueError):
                            pass
                if out:
                    return out
        except (ValueError, TypeError):
            pass

        pairs: list[tuple[str, float]] = []
        for part in re.split(r"[,，\s]+", text):
            if ":" not in part and "：" not in part:
                continue
            key, _, value = part.replace("：", ":").partition(":")
            try:
                pairs.append((key.strip(), float(value.strip())))
            except (TypeError, ValueError):
                pass
        return pairs

    def reset_state(self):
        return 80.0, 100.0, 1
