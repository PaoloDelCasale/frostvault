"""Shared populated PostgreSQL contract; never calls cloud storage."""
import json
from tests.notification_migration_contract import preserved_state, seed_legacy_notifications


def state(connection):
    result = preserved_state(connection)
    for row in result["vaults"]:
        row.pop("default_lifecycle_policy_id", None)
        row.pop("storage_plan_json", None)
    for row in result["archive_versions"]:
        row.pop("desired_policy_id", None)
        row.pop("applied_policy_id", None)
    return {table: sorted(rows, key=lambda row: repr(sorted(row.items()))) for table, rows in result.items()}


def seed(run, connect, test):
    result = run("0045_drop_quotas_windows")
    test.assertEqual(result.returncode, 0, result.stderr)
    with connect() as connection:
        seed_legacy_notifications(connection)
    result = run("0052_s3_sdk")
    test.assertEqual(result.returncode, 0, result.stderr)


def legacy_policy(connection, *, policy="11111111-1111-4111-8111-111111111111", days=1):
    profile = json.dumps({"transitions": [{"days": days, "storage_class": "DEEP_ARCHIVE"}], "noncurrent_transitions": [{"days": 180, "storage_class": "DEEP_ARCHIVE"}], "expiration_days": 10, "noncurrent_expiration_days": 20})
    connection.execute("INSERT INTO lifecycle_policies(id, vault_id, name, created_at, profile_json) VALUES (%s, 2, 'Legacy', '2026-01-01', %s)", (policy, profile))
    return policy


def assert_storage_plan_migration(test, run, connect):
    seed(run, connect, test)
    with connect() as connection:
        policy = legacy_policy(connection)
        connection.execute("UPDATE vaults SET default_lifecycle_policy_id=%s WHERE id=2", (policy,))
        connection.execute("INSERT INTO folder_policy_overrides(vault_id, folder_path, policy_id) VALUES (2, 'docs', %s)", (policy,))
        before = state(connection)
    for target, command in [("0053_vault_storage_plans", "upgrade"), ("0052_s3_sdk", "downgrade"), ("0053_vault_storage_plans", "upgrade")]:
        result = run(target, command)
        test.assertEqual(result.returncode, 0, result.stderr)
        with connect() as connection:
            test.assertEqual(state(connection), before)
            vault = connection.execute("SELECT * FROM vaults WHERE id=2").fetchone()
            if command == "upgrade":
                test.assertNotIn("default_lifecycle_policy_id", vault)
                test.assertEqual(json.loads(vault["storage_plan_json"]), {"transitions": [{"days": 1, "storage_class": "DEEP_ARCHIVE"}]})
                version = connection.execute("SELECT * FROM archive_versions LIMIT 1").fetchone()
                test.assertNotIn("desired_policy_id", version)
                # Legacy prefixes remain real cloud identities, not invented UUID
                # paths. The migrated plan must actually be synchronizable.
                from unittest.mock import Mock
                from app.services.lifecycle_policies import sync_lifecycle_rules_for_bucket
                client = Mock()
                client.get_bucket_lifecycle_configuration.return_value = {"Rules": []}
                sync_lifecycle_rules_for_bucket(connection, client, bucket=vault["s3_bucket"])
                rules = client.put_bucket_lifecycle_configuration.call_args.kwargs["LifecycleConfiguration"]["Rules"]
                test.assertEqual(rules[0]["Filter"], {"Prefix": vault["s3_prefix"].strip("/") + "/"})
                client.delete_object.assert_not_called()
                client.delete_objects.assert_not_called()


def assert_ambiguous_plan_refused(test, run, connect, *, pinned=False):
    seed(run, connect, test)
    with connect() as connection:
        policy = legacy_policy(connection)
        connection.execute("UPDATE vaults SET default_lifecycle_policy_id=%s WHERE id=2", (policy,))
        if pinned:
            connection.execute("INSERT INTO lifecycle_pins(vault_id, path, is_directory, pinned_at) VALUES (2, 'only.txt', %s, '2026-01-01')", (False,))
        else:
            other = legacy_policy(connection, policy="22222222-2222-4222-8222-222222222222", days=2)
            connection.execute("INSERT INTO folder_policy_overrides(vault_id, folder_path, policy_id) VALUES (2, 'docs', %s)", (other,))
        before = state(connection)
    result = run("0053_vault_storage_plans")
    test.assertNotEqual(result.returncode, 0)
    test.assertIn("ambiguous legacy storage plan", result.stderr)
    with connect() as connection:
        test.assertEqual(state(connection), before)
        test.assertEqual(connection.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"], "0052_s3_sdk")
        test.assertNotIn("storage_plan_json", connection.execute("SELECT * FROM vaults LIMIT 1").fetchone())
