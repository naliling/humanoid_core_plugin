"""Regression tests for persona-scoped user relations."""
from __future__ import annotations

import unittest

from humanoid.persona import EMPTY, Persona, PersonaSource, persona_relation_identity
from humanoid.role_scope import RoleScope
from tests.fakes import FakeContext, FakeConversationManager, FakePersonaManager, fake_persona


class PersonaRelationScopeTest(unittest.TestCase):
    def test_same_persona_shares_peer_data_but_self_state_stays_per_bot(self):
        data = {"roles": {}, "persona_relations": {}, "persona_legacy_migrations": {}}
        bot1 = RoleScope(data, "bot1")
        bot1.bind_persona(persona_relation_identity(Persona(name="Ａlice")), "Ａlice")
        bot1.update_user("42", nickname="peer", relationship_stage=2)
        bot1.set_self("timezone_city", "Tokyo")

        bot2 = RoleScope(data, "bot2")
        bot2.bind_persona(persona_relation_identity(Persona(name="Alice")), "Alice")
        self.assertEqual(bot2.get_user("42", "nickname"), "peer")
        self.assertEqual(bot2.get_user("42", "relationship_stage"), 2)
        self.assertIsNone(bot2.get_self("timezone_city"))
        bot2.update_user("42", relationship_stage=3)
        self.assertEqual(bot1.get_user("42", "relationship_stage"), 3)

        different = RoleScope(data, "bot3")
        different.bind_persona("bob", "Bob")
        self.assertIsNone(different.get_user("42", "nickname"))

    def test_unknown_persona_keeps_legacy_user_data_local(self):
        data = {
            "roles": {"old": {"self": {}, "users": {"42": {"nickname": "local"}}}},
            "persona_relations": {},
            "persona_legacy_migrations": {},
        }
        scope = RoleScope(data, "new")
        scope.bind_persona("", "", {"old": ("", "")})
        self.assertEqual(scope.get_user("42", "nickname"), None)
        self.assertEqual(data["roles"]["old"]["users"]["42"]["nickname"], "local")
        self.assertEqual(data["persona_relations"], {})
        self.assertEqual(data["persona_legacy_migrations"]["old"], "")

    def test_legacy_merge_keeps_max_top_level_interaction_and_earliest_first_met(self):
        data = {
            "roles": {
                "bot1": {"self": {}, "users": {"42": {
                    "nickname": "older", "first_met": 20, "last_interaction": 100,
                    "last_message": {"ts": 300, "dir": "in", "text": "hello"},
                }}},
                "bot2": {"self": {}, "users": {"42": {
                    "nickname": "newer", "first_met": 10, "last_interaction": 200,
                }}},
                "bot3": {"self": {}, "users": {}},
            },
            "persona_relations": {},
            "persona_legacy_migrations": {},
        }
        scope = RoleScope(data, "bot3")
        owners = {"bot1": ("alice", "Alice"), "bot2": ("alice", "Alice")}
        scope.bind_persona("alice", "Alice", owners)
        peer = scope.user_state("42")
        self.assertEqual(peer["nickname"], "older")
        self.assertEqual(peer["last_interaction"], 200)
        self.assertEqual(peer["first_met"], 10)
        self.assertEqual(data["roles"]["bot1"]["users"], {})
        self.assertEqual(data["roles"]["bot2"]["users"], {})
        snapshot = repr(data)
        self.assertFalse(scope.bind_persona("alice", "Alice", owners))
        self.assertEqual(repr(data), snapshot)


class PersonaEventBindingTest(unittest.IsolatedAsyncioTestCase):
    async def test_exact_umo_is_resolved_and_empty_umo_stays_unbound(self):
        context = FakeContext(
            persona_manager=FakePersonaManager(
                personas={"a": fake_persona("Alice", "Persona A"),
                          "b": fake_persona("Bob", "Persona B")},
                default=fake_persona("Default", "default"),
            ),
            conversation_manager=FakeConversationManager({"umo-a": "a", "umo-b": "b"}),
        )
        source = PersonaSource(context)
        alice = await source.persona_for_event("bot1", "umo-a")
        bob = await source.persona_for_event("bot1", "umo-b")
        self.assertEqual(alice.name, "Alice")
        self.assertEqual(bob.name, "Bob")
        self.assertEqual(source.cached_persona_for_umo("umo-a").name, "Alice")
        self.assertEqual(source.cached_persona("bot1").name, "Bob")
        self.assertEqual(await source.persona_for_event("bot2", ""), EMPTY)
        self.assertEqual(source.umo_for("bot2"), "")
        self.assertEqual(persona_relation_identity(EMPTY), "")


    async def test_legacy_umo_refresh_bypasses_warm_persona_cache(self):
        persona_manager = FakePersonaManager(
            personas={"a": fake_persona("Alice", "Persona A"),
                      "b": fake_persona("Bob", "Persona B")}
        )
        conversation_manager = FakeConversationManager({"umo-legacy": "a"})
        source = PersonaSource(FakeContext(
            persona_manager=persona_manager, conversation_manager=conversation_manager
        ))

        first = await source.persona_for_umo("umo-legacy")
        self.assertEqual(first.name, "Alice")
        conversation_manager.binding["umo-legacy"] = "b"
        self.assertEqual((await source.persona_for_umo("umo-legacy")).name, "Alice")
        refreshed = await source.persona_for_umo("umo-legacy", refresh=True)
        self.assertEqual(refreshed.name, "Bob")
        self.assertEqual(persona_manager.calls, 2)


if __name__ == "__main__":
    unittest.main()
