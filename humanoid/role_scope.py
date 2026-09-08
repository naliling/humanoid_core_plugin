"""数据作用域：封装对 state.json 中特定角色数据的读写。"""

from __future__ import annotations

from typing import Any, Callable, Optional


class RoleScope:
    """封装对 state.json 中特定角色数据的读写。"""

    def __init__(self, state_data: dict, role_id: str):
        self._state_data = state_data
        self._role_id = str(role_id)
        self._mark_dirty: Optional[Callable[[], None]] = None

        roles = self._state_data.setdefault("roles", {})
        self._root = roles.setdefault(self._role_id, {"self": {}, "users": {}})

    def set_mark_dirty(self, callback: Callable[[], None]):
        self._mark_dirty = callback

    @property
    def role_id(self) -> str:
        return self._role_id

    @property
    def self_state(self) -> dict:
        return self._root.setdefault("self", {})

    def user_state(self, user_id: str) -> dict:
        users = self._root.setdefault("users", {})
        return users.setdefault(str(user_id), {})

    def all_user_ids(self) -> list[str]:
        return list(self._root.get("users", {}).keys())

    def mark_dirty(self):
        if self._mark_dirty:
            self._mark_dirty()

    def get_self(self, key: str, default=None):
        return self.self_state.get(key, default)

    def set_self(self, key: str, value):
        self.self_state[key] = value
        self.mark_dirty()

    def update_self(self, **kwargs):
        self.self_state.update(kwargs)
        self.mark_dirty()

    def get_user(self, user_id: str, key: str, default=None):
        return self.user_state(user_id).get(key, default)

    def set_user(self, user_id: str, key: str, value):
        self.user_state(user_id)[key] = value
        self.mark_dirty()

    def update_user(self, user_id: str, **kwargs):
        self.user_state(user_id).update(kwargs)
        self.mark_dirty()

    def ensure_user(self, user_id: str) -> dict:
        return self.user_state(user_id)

    def delete_user(self, user_id: str):
        users = self._root.get("users", {})
        if str(user_id) in users:
            del users[str(user_id)]
            self.mark_dirty()