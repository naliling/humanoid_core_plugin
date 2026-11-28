"""数据作用域：封装对 state.json 中特定角色数据的读写。"""

from __future__ import annotations

import copy
import math
from contextvars import ContextVar
from typing import Callable, Optional


class RoleScope:
    """封装对 state.json 中特定角色数据的读写。"""

    def __init__(self, state_data: dict, role_id: str, mark_dirty: Optional[Callable[[], None]] = None):
        self._state_data = state_data
        self._role_id = str(role_id)
        self._mark_dirty: Optional[Callable[[], None]] = mark_dirty
        self._active_persona: ContextVar[str] = ContextVar(
            f"humanoid-persona-scope-{id(self)}", default=""
        )

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

    @staticmethod
    def _record_stamp(record: dict) -> float:
        candidates = [record.get("last_interaction")]
        mood = record.get("mood")
        if isinstance(mood, dict):
            candidates.append(mood.get("last_interaction"))
        message = record.get("last_message")
        if isinstance(message, dict):
            candidates.append(message.get("ts"))
        stamps = []
        for value in candidates:
            try:
                stamp = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(stamp) and stamp > 0:
                stamps.append(stamp)
        return max(stamps, default=0.0)

    @classmethod
    def _merge_user_records(cls, current: dict, incoming: dict) -> dict:
        if cls._record_stamp(incoming) > cls._record_stamp(current):
            winner, fallback = incoming, current
        else:
            winner, fallback = current, incoming
        merged = copy.deepcopy(winner)
        for key, value in fallback.items():
            merged.setdefault(key, copy.deepcopy(value))
        last_interactions = []
        for record in (current, incoming):
            try:
                stamp = float(record.get("last_interaction"))
            except (TypeError, ValueError):
                continue
            if math.isfinite(stamp) and stamp > 0:
                last_interactions.append(stamp)
        if last_interactions:
            merged["last_interaction"] = max(last_interactions)
        first_met = []
        for record in (current, incoming):
            try:
                stamp = float(record.get("first_met"))
            except (TypeError, ValueError):
                continue
            if math.isfinite(stamp) and stamp > 0:
                first_met.append(stamp)
        if first_met:
            merged["first_met"] = min(first_met)
        return merged

    def bind_persona(
        self,
        persona_id: str,
        display_name: str = "",
        legacy_owners: Optional[dict[str, object]] = None,
    ) -> bool:
        """Bind this task to a named persona and migrate only identified legacy roles."""
        active_id = str(persona_id or "").strip()
        self._active_persona.set(active_id)
        changed = False
        relations = self._state_data.get("persona_relations")
        if not isinstance(relations, dict):
            relations = {}
            self._state_data["persona_relations"] = relations
            changed = True
        migrations = self._state_data.get("persona_legacy_migrations")
        if not isinstance(migrations, dict):
            migrations = {}
            self._state_data["persona_legacy_migrations"] = migrations
            changed = True

        def ensure_bucket(identity: str, name: str) -> dict:
            nonlocal changed
            bucket = relations.get(identity)
            if not isinstance(bucket, dict):
                bucket = {"name": name or identity, "users": {}}
                relations[identity] = bucket
                changed = True
            else:
                if name and bucket.get("name") != name:
                    bucket["name"] = name
                    changed = True
                if not isinstance(bucket.get("users"), dict):
                    bucket["users"] = {}
                    changed = True
            return bucket

        if active_id:
            ensure_bucket(active_id, str(display_name or active_id))

        roles = self._state_data.get("roles")
        if not isinstance(roles, dict):
            roles = {}
        for source_role, raw_owner in sorted((legacy_owners or {}).items()):
            source_role = str(source_role)
            if source_role in migrations or raw_owner is None:
                continue
            if isinstance(raw_owner, (tuple, list)):
                owner_id = str(raw_owner[0] or "").strip() if raw_owner else ""
                owner_name = str(raw_owner[1] or "") if len(raw_owner) > 1 else ""
            else:
                owner_id = str(raw_owner or "").strip()
                owner_name = owner_id
            if not owner_id:
                migrations[source_role] = ""
                changed = True
                continue
            bucket = ensure_bucket(owner_id, owner_name)
            role_data = roles.get(source_role)
            source_users = role_data.get("users") if isinstance(role_data, dict) else None
            if isinstance(source_users, dict) and source_users:
                target_users = bucket["users"]
                for user_id, incoming in source_users.items():
                    if not isinstance(incoming, dict):
                        continue
                    current = target_users.get(str(user_id))
                    if isinstance(current, dict):
                        target_users[str(user_id)] = self._merge_user_records(current, incoming)
                    else:
                        target_users[str(user_id)] = copy.deepcopy(incoming)
                role_data["users"] = {}
                changed = True
            migrations[source_role] = owner_id
            changed = True

        if changed:
            self.mark_dirty()
        return changed

    def _users(self) -> dict:
        persona_id = self._active_persona.get()
        if not persona_id:
            users = self._root.get("users")
            if not isinstance(users, dict):
                users = {}
                self._root["users"] = users
                self.mark_dirty()
            return users
        relations = self._state_data.get("persona_relations")
        if not isinstance(relations, dict):
            relations = {}
            self._state_data["persona_relations"] = relations
            self.mark_dirty()
        bucket = relations.get(persona_id)
        if not isinstance(bucket, dict):
            bucket = {"name": persona_id, "users": {}}
            relations[persona_id] = bucket
            self.mark_dirty()
        users = bucket.get("users")
        if not isinstance(users, dict):
            users = {}
            bucket["users"] = users
            self.mark_dirty()
        return users

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
        users = self._users()
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
        return list(self._users().keys())

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
        """**只读**：读一个字段，不因为读而建出这个用户的条目。

        原来走 `user_state()`（get-or-create）：`get_user` 的调用方全是纯读
        （core_instance 的 last_interaction / last_message / her_said_at、mood 的 nickname），
        每读一次就给这个人留一条永久空壳——空壳不含任何会被过期清理的字段，
        `prune_expired` 直接跳过，于是 state.json 随群规模一直涨（审核 引擎与诊断 M7）。
        写路径仍走 `user_state()` / `ensure_user()` 显式建档。
        """
        return self.peek_user(user_id, key, default)

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
        users = self._users()
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
        users = self._users()
        if str(user_id) in users:
            del users[str(user_id)]
            self.mark_dirty()