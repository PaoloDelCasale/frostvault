"""Populated SQLite/PostgreSQL contract for the transport-only schema cut."""
from tests.notification_migration_contract import preserved_state, seed_legacy_notifications


def state(connection):
    result = preserved_state(connection)
    for row in result["vaults"]:
        row.pop("rclone_remote", None)
    return {table: sorted(rows, key=lambda row: repr(sorted(row.items())))
            for table, rows in result.items()}


def assert_sdk_migration(test, run, connect):
    result = run("0045_drop_quotas_windows")
    test.assertEqual(result.returncode, 0, result.stderr)
    with connect() as connection:
        seed_legacy_notifications(connection)
    result = run("0051_plaintext_vaults")
    test.assertEqual(result.returncode, 0, result.stderr)
    with connect() as connection:
        before = state(connection)
    for target, command in (("0052_s3_sdk", "upgrade"),
                            ("0051_plaintext_vaults", "downgrade"),
                            ("0052_s3_sdk", "upgrade")):
        result = run(target, command)
        test.assertEqual(result.returncode, 0, result.stderr)
        with connect() as connection:
            test.assertEqual(state(connection), before)
            vault = connection.execute("SELECT * FROM vaults WHERE id=2").fetchone()
            if command == "upgrade":
                test.assertNotIn("rclone_remote", vault)
            else:
                test.assertEqual(vault["rclone_remote"], "")
