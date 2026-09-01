"""数据迁移：将旧版 state.json 迁移到 v3 多角色结构。"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

VERSION = 3

SELF_KEYS = {
    "energy", "max_energy", "current_cycle_day", "last_cycle_update",
    "social_energy", "last_social_reset", "weather_cache", "last_weather_fetch",
    "daily_schedule", "schedule_source", "schedule_generated_at",
    "mood_tags", "last_update", "today_date", "_cached_weather_obj",
    "_last_weather_fetch", "_cached_location", "_mood_decay_last_run",
    "_last_social_energy_reset_date", "_schema_migrated_to", "_state_version",
}

USER_KEYS = {
    "moods": "mood",
    "mood_logs": "mood_logs",
    "nicknames": "nickname",
    "user_last_seen": "last_interaction",
    "last_message": "last_message",
}


def needs_migration(state_data: dict) -> bool:
    return state_data.get("_version", 0) < VERSION


def migrate(state_data: dict, default_role_id: str = "default") -> dict:
    if not needs_migration(state_data):
        return state_data

    new_data = {
        "_version": VERSION,
        "roles": {
            default_role_id: {
                "self": {},
                "users": {}
            }
        }
    }

    self_state = new_data["roles"][default_role_id]["self"]
    users_state = new_data["roles"][default_role_id]["users"]

    for key in SELF_KEYS:
        if key in state_data:
            self_state[key] = state_data[key]

    for old_key, new_key in USER_KEYS.items():
        if old_key in state_data:
            old_data = state_data[old_key]
            if isinstance(old_data, dict):
                for user_id, value in old_data.items():
                    if user_id not in users_state:
                        users_state[user_id] = {}
                    users_state[user_id][new_key] = value

    for key, value in state_data.items():
        if key not in SELF_KEYS and key not in USER_KEYS and key != "_version":
            if key not in new_data:
                new_data[key] = value

    return new_data


def migrate_file(path: Path, default_role_id: str = "default") -> bool:
    if not path.exists():
        return False

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return False

    if not needs_migration(data):
        return False

    backup_path = path.with_name(f"{path.stem}.v2-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json")
    shutil.copy2(path, backup_path)

    new_data = migrate(data, default_role_id)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(new_data, f, ensure_ascii=False, indent=2)

    return True


def get_default_role_id(state_data: dict) -> str:
    roles = state_data.get("roles", {})
    if roles:
        return list(roles.keys())[0]
    return "default"