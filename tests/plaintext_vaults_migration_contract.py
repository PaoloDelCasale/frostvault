"""Populated SQLite/PostgreSQL contract for the #419 schema cut."""
from tests.notification_migration_contract import preserved_state, seed_legacy_notifications

REMOVED = ("encryption_mode", "crypt_password_ciphertext",
           "crypt_password2_ciphertext", "recovery_custody_confirmed_at")


def state(connection):
    result = preserved_state(connection)
    for row in result["vaults"]:
        for field in REMOVED:
            row.pop(field, None)
    for table in ("metadata_backup_runs", "vault_operation_policies"):
        result[table] = [dict(row) for row in connection.execute(f"SELECT * FROM {table}").fetchall()]
    return {table: sorted(rows, key=lambda row: repr(sorted(row.items())))
            for table, rows in result.items()}


def assert_plaintext_migration(test, run, connect):
    result = run("0045_drop_quotas_windows")
    test.assertEqual(result.returncode, 0, result.stderr)
    with connect() as connection:
        seed_legacy_notifications(connection)
    result = run("0050_current_snapshot_only")
    test.assertEqual(result.returncode, 0, result.stderr)
    with connect() as connection:
        connection.execute("""
            INSERT INTO metadata_backup_runs(created_at, reason, backend, status,
                local_path, digest_sha256, database_sha256, s3_key, size_bytes)
            VALUES ('2026-11-03', 'manual', 'sqlite', 'succeeded',
                '/backups/metadata.bak.enc', 'artifact-digest', 'database-digest',
                'system/backups/metadata.bak.enc', 1234)
        """)
        before = state(connection)
    for target, command in (("0051_plaintext_vaults", "upgrade"),
                            ("0050_current_snapshot_only", "downgrade"),
                            ("0051_plaintext_vaults", "upgrade")):
        result = run(target, command)
        test.assertEqual(result.returncode, 0, result.stderr)
        with connect() as connection:
            test.assertEqual(state(connection), before)
            vault = connection.execute("SELECT * FROM vaults WHERE id=2").fetchone()
            if command == "upgrade":
                test.assertTrue(all(field not in vault for field in REMOVED))
            else:
                test.assertEqual(vault["encryption_mode"], "plain")
                test.assertTrue(all(vault[field] is None for field in REMOVED[1:]))

    # Fail before any DDL, with no row values in output and no lost secrets.
    result = run("0050_current_snapshot_only", "downgrade")
    test.assertEqual(result.returncode, 0, result.stderr)
    with connect() as connection:
        connection.execute("""
            UPDATE vaults SET encryption_mode='crypt',
                crypt_password_ciphertext='legacy-secret-fixture-one',
                crypt_password2_ciphertext='legacy-secret-fixture-two' WHERE id=2
        """)
        before = state(connection)
    result = run("0051_plaintext_vaults")
    test.assertNotEqual(result.returncode, 0)
    test.assertIn("Legacy encrypted Vaults are unsupported", result.stderr)
    test.assertNotIn("legacy-secret-fixture-one", result.stderr + result.stdout)
    test.assertNotIn("legacy-secret-fixture-two", result.stderr + result.stdout)
    with connect() as connection:
        test.assertEqual(state(connection), before)
        vault = connection.execute("SELECT * FROM vaults WHERE id=2").fetchone()
        test.assertEqual(vault["encryption_mode"], "crypt")
        test.assertEqual(vault["crypt_password_ciphertext"], "legacy-secret-fixture-one")
        test.assertEqual(vault["crypt_password2_ciphertext"], "legacy-secret-fixture-two")
        test.assertEqual(connection.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"],
                         "0050_current_snapshot_only")
