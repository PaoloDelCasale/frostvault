"""Shared PostgreSQL preservation contract for #416."""
from __future__ import annotations

from tests.notification_migration_contract import seed_legacy_notifications
from tests.offline_removal_migration_contract import normalized_state


def assert_administration_migration(test, run, connect):
    result = run("0045_drop_quotas_windows")
    test.assertEqual(result.returncode, 0, result.stderr)
    with connect() as connection:
        seed_legacy_notifications(connection)
    result = run("0047_remove_offline_cache")
    test.assertEqual(result.returncode, 0, result.stderr)
    with connect() as connection:
        connection.execute("INSERT INTO system_settings(key, value, updated_by, updated_at) VALUES ('restore_tier', '\"Standard\"', 1, '2026-11-03')")
        connection.execute("INSERT INTO worker_errors(created_at, component, classification, message) VALUES ('2026-11-03', 'scan', 'unexpected', 'legacy diagnostic')")
        connection.execute("INSERT INTO source_volumes(alias, fingerprint_version, expected_fingerprint, first_seen_at, last_seen_at) VALUES ('photos', 'linux-mountinfo-v1', %s, '2026-11-03', '2026-11-03')", ("a" * 64,))
        before = normalized_state(connection)
        source_volumes = connection.execute("SELECT * FROM source_volumes ORDER BY alias").fetchall()
    for target, command in (("0048_essential_administration", "upgrade"), ("0047_remove_offline_cache", "downgrade"), ("0048_essential_administration", "upgrade")):
        result = run(target, command)
        test.assertEqual(result.returncode, 0, result.stderr)
        with connect() as connection:
            test.assertEqual(normalized_state(connection), before)
            test.assertEqual(connection.execute("SELECT * FROM source_volumes ORDER BY alias").fetchall(), source_volumes)
            if command == "downgrade":
                for table in ("system_settings", "worker_errors"):
                    test.assertEqual(connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"], 0)
