"""Populated 0069 -> 0070 preserves Vaults/Jobs and refuses active-hold downgrade."""


def assert_vault_pause_migration(test, run, connection):
    result = run("0069_upload_intent_per_attempt")
    test.assertEqual(result.returncode, 0, result.stderr)
    with connection() as c:
        c.execute("INSERT INTO vaults(id,slug,name,source_root,s3_bucket,s3_prefix) VALUES (2,'pause','Pause','/source','bucket','pause')")
        before = c.execute("SELECT * FROM vaults WHERE id=2").fetchone()
    result = run("0070_vault_pause")
    test.assertEqual(result.returncode, 0, result.stderr)
    with connection() as c:
        after = c.execute("SELECT * FROM vaults WHERE id=2").fetchone()
        test.assertFalse(after.pop("paused"))
        test.assertEqual(after, before)
        c.execute("UPDATE vaults SET paused=TRUE WHERE id=2")
    result = run("0069_upload_intent_per_attempt", "downgrade")
    test.assertNotEqual(result.returncode, 0)
    test.assertIn("Resume paused Vaults", result.stderr)
    with connection() as c:
        test.assertTrue(c.execute("SELECT paused FROM vaults WHERE id=2").fetchone()["paused"])
        test.assertEqual(c.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"], "0070_vault_pause")
        c.execute("UPDATE vaults SET paused=FALSE WHERE id=2")
        # A Stop Protection tombstone cannot resume and has no ordinary cloud
        # work to restart: its historical flag must not strand downgrade.
        c.execute("INSERT INTO vaults(id,slug,name,source_root,s3_bucket,s3_prefix,decommission_state,paused) VALUES (3,'stopped','Stopped','/old-source','bucket','stopped','decommissioned',TRUE)")
    result = run("0069_upload_intent_per_attempt", "downgrade")
    test.assertEqual(result.returncode, 0, result.stderr)
    result = run("0070_vault_pause")
    test.assertEqual(result.returncode, 0, result.stderr)
