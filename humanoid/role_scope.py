"""数据作用域：封装对 state.json 中特定角色数据的读写。"""

from __future__ import annotations

from typing import Any, Callable, Optional


class RoleScope:
    """封装对 state.json 中特定角色数据的读写。"""

    def __init__(self, state_data: dict, role_id: str, mark_dirty: Optional[Callable[[], None]] = None):
        self._state_data = state_data
        self._role_id = str(role_id)
        self._mark_dirty: Optional[Callable[[], None]] = mark_dirty

        roles = self._state_data.setdefault("roles", {})
        existed = self._role_id in roles
        self._root = roles.setdefault(self._role_id, {"self": {}, "users": {}})
        if not existed:
            # 新角色必须标脏，否则这个 `roles[新id]` 只存在于内存里，插件重载或崩溃
            # 就整个没了。`mark_dirty` 必须由构造函数传入——`set_mark_dirty` 是构造之后
            # 才调的，在这里读到的一直是 None，标脏从未真正发生过。
            if callable(self._mark_dirty):
                self._mark_dirty()

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

    def peek_user(self, user_id: str, key: str, default=None):
        """**只读**，而且不因为读而建出这个用户的条目。

        `get_user` 走 `user_state()`，后者会 `setdefault` 建一条空 `{}`——于是「群聊里
        有人说过话」这件事本身就给每个人留了个永久空壳：它不含任何会被过期清理的字段，
        `prune_expired` 直接跳过，于是 state.json 随群规模一直涨。
        注入层那些「只是查一下有没有称呼」的地方一律走这里。
        """
        # 必须和 `user_state`/`get_user`/`set_user`/`delete_user` 一样先 `str()`：
        # 数据是按字符串 id 存的，`peek_user(123, ...)` 会去查 `users[123]` 而数据在
        # `users["123"]`——不报错，只是**静默返回默认值**（数字 id 的调用方永远查不到）。
        entry = self._root.get("users", {}).get(str(user_id))
        if isinstance(entry, dict):
            return entry.get(key, default)
        return default

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