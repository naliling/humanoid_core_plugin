"""各领域服务。"""

from __future__ import annotations

from .energy import EnergyService
from .mood import MoodService
from .process import ProcessService
from .schedule import ScheduleService
from .social import SocialEnergyService
from .weather import WeatherService

__all__ = [
    "EnergyService",
    "MoodService",
    "ProcessService",
    "ScheduleService",
    "SocialEnergyService",
    "WeatherService",
]
