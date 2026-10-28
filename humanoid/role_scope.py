"""数据作用域：封装对 state.json 中特定角色数据的读写。"""

from __future__ import annotations

from typing import Callable, Optional


class RoleScope:
    """封装对 state.json 中特定角色数据的读写。"""

    def __init__(self, state_data: dict, role_id: str, mark_dirty: Optional[Callable[[], None]] = None):
        self._state_data = state_data
        self._role_id = str(role_id)
        self._mark_dirty: Optional[Callable[[], None]] = mark_dirty

        roles = self._state_data.setdefault("roles", {})
        if not isinstance(roles, dict):
            # 顶层 roles 被打坏（手改/旧版本写坏）时不能把整条作用域链带崩。
            roles = {}
            self._state_data["roles"] = roles
            self.mark_dirty()
        root = roles.get(self._role_id)
        if not isinstance(root, dict):
            # 新角色或坏角色条目：换成合法骨架并标脏，否则这个 `roles[新id]` 只存在于
            # 内存里，插件重载或崩溃就整个没了。`mark_dirty` 必须由构造函数传入——
            # `set_mark_dirty` 是构造之后才调的，在这里读到的一直是 None。
            root = {"self": {}, "users": {}}
            roles[self._role_id] = root
            self.mark_dirty()
        changed = False
        for key in ("self", "users"):
            if not isinstance(root.get(key), dict):
                root[key] = {}
                changed = True
        if changed:
            self.mark_dirty()
        self._root = root

    def set_mark_dirty(self, callback: Callable[[], None]):
        self._mark_dirty = callback

    @property
    def role_id(self) -> str:
        return self._role_id

    @property
    def self_state(self) -> dict:
        state = self._root.get("self")
        if not isinstance(state, dict):
            state = {}
            self._root["self"] = state
            self.mark_dirty()
        return state

    def user_state(self, user_id: str) -> dict:
        """取（必要时建出）这个用户的状态字典。

        P1-5：用户条目必须是 dict。坏值（int/str/None…）在这里**就地重写**成空档案
        并标脏——只靠 `StateStore._migrate` 不够（RoleScope 也可能建在裸数据上），
        而把坏值原样返回会让 `get_user` 抛 AttributeError、`set_user` 抛 TypeError，
        这个用户此后永久不涨好感、不记精力，重新 load 也修不好。
        """
        users = self._root.get("users")
        if not isinstance(users, dict):
            users = {}
            self._root["users"] = users
            self.mark_dirty()
        key = str(user_id)
        missing = object()
        entry = users.get(key, missing)
        if entry is missing:
            # 老行为：读路径建空壳，不标脏（真正写入时会由 set_user 等标脏）。
            entry = {}
            users[key] = entry
            return entry
        if not isinstance(entry, dict):
            entry = {}
            users[key] = entry
            self.mark_dirty()
        return entry

    def all_user_ids(self) -> list[str]:
        users = self._root.get("users")
        if not isinstance(users, dict):
            return []
        return list(users.keys())

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
        users = self._root.get("users")
        if not isinstance(users, dict):
            return default
        entry = users.get(str(user_id))
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
        users = self._root.get("users")
        if not isinstance(users, dict):
            return
        if str(user_id) in users:
            del users[str(user_id)]
            self.mark_dirty()