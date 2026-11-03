"""Additional populated migration cases shared by SQLite and PostgreSQL CI."""
import json
from tests.storage_plan_migration_contract import seed, state, legacy_policy


def assert_standard_plan_mapping(test, run, connect):
    seed(run, connect, test)
    with connect() as connection:
        # No default means no current transitions. Retired noncurrent/expiry
        # actions cannot revive deletion, and Standard makes the pin redundant.
        policy_id = "33333333-3333-4333-8333-333333333333"
        connection.execute(
            "INSERT INTO lifecycle_policies(id, vault_id, name, created_at, profile_json) "
            "VALUES (%s, 2, 'Retired rules only', '2026-01-01', %s)",
            (policy_id, json.dumps({"transitions": [], "noncurrent_transitions": [
                {"days": 180, "storage_class": "DEEP_ARCHIVE"}], "expiration_days": 10})),
        )
        connection.execute(
            "INSERT INTO folder_policy_overrides(vault_id, folder_path, policy_id) "
            "VALUES (2, 'docs', %s)", (policy_id,),
        )
        connection.execute(
            "INSERT INTO lifecycle_pins(vault_id, path, is_directory, pinned_at) "
            "VALUES (2, 'only.txt', %s, '2026-01-01')", (False,),
        )
        before = state(connection)
    result = run("0053_vault_storage_plans")
    test.assertEqual(result.returncode, 0, result.stderr)
    with connect() as connection:
        test.assertEqual(state(connection), before)
        vault = connection.execute("SELECT storage_plan_json FROM vaults WHERE id=2").fetchone()
        test.assertEqual(json.loads(vault["storage_plan_json"]), {"transitions": []})


def assert_unsafe_namespaces_refused(test, run, connect):
    seed(run, connect, test)
    with connect() as connection:
        policy_id = legacy_policy(connection)
        connection.execute("UPDATE vaults SET default_lifecycle_policy_id=%s WHERE id=2", (policy_id,))
        # An empty prefix is ambiguous authority over the entire bucket.
        connection.execute("UPDATE vaults SET s3_prefix='' WHERE id=2")
        before = state(connection)
    result = run("0053_vault_storage_plans")
    test.assertNotEqual(result.returncode, 0)
    test.assertIn("Ambiguous legacy Vault namespaces", result.stderr)
    with connect() as connection:
        test.assertEqual(state(connection), before)
        vault = connection.execute("SELECT * FROM vaults WHERE id=2").fetchone()
        test.assertNotIn("storage_plan_json", vault)
