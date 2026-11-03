"""Shared SQLite/PostgreSQL preservation contract for migration #415."""
from __future__ import annotations

from typing import Any

from tests.notification_migration_contract import preserved_state


def normalized_state(connection: Any) -> dict[str, list[dict[str, Any]]]:
    """Only the removed cache nonce is intentionally not lossless."""
    state = preserved_state(connection)
    for session in state["sessions"]:
        session.pop("offline_cache_nonce", None)
        if "offline_cache_generation" in session:
            session["transition_version"] = session.pop("offline_cache_generation")
    return state


def assert_session_schema(test: Any, connection: Any, *, upgraded: bool) -> None:
    row = connection.execute("SELECT * FROM sessions").fetchone()
    if upgraded:
        test.assertIn("transition_version", row)
        test.assertNotIn("offline_cache_generation", row)
        test.assertNotIn("offline_cache_nonce", row)
        test.assertEqual(row["transition_version"], 7)
    else:
        test.assertNotIn("transition_version", row)
        test.assertEqual(row["offline_cache_generation"], 7)
        test.assertGreaterEqual(len(row["offline_cache_nonce"]), 32)
