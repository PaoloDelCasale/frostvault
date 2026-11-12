"""Populated 0068 -> 0069: one OPEN intent per Job; a reused Job refuses downgrade (#538)."""
import psycopg

from tests.historical_catalog_fixture import HistoricalCatalogFixture as ArchiveCatalog

INSERT_INTENT = """INSERT INTO upload_intents(id,token,vault_id,vault_file_id,original_job_id,target_json,snapshot_json,requested,inventory_json,settled,created_at)
    VALUES (%s,%s,2,%s,%s,'{}','{}','read_back','{}',%s,%s)"""


def assert_upload_intent_per_attempt_migration(test, run, connect):
    result = run("0068_unsupported_state")
    test.assertEqual(result.returncode, 0, result.stderr)
    stamp = "2026-11-10T00:00:00+00:00"
    with connect() as c:
        c.execute("INSERT INTO users(id,username,display_name,password_hash) VALUES (1,'owner','Owner','hash')")
        c.execute("INSERT INTO vaults(id,slug,name,source_root,s3_bucket,s3_prefix) VALUES (2,'docs','Docs','/nonexistent/no-filesystem-ddl','bucket','docs')")
        file_id = ArchiveCatalog(c).observe_local_copy(vault_id=2, path="a", file_type="regular", size=1, mtime_ns=100, observed_at=stamp)
        job = c.execute("""INSERT INTO jobs(vault_id,vault_file_id,path,action,status,requested_by,requested_at,updated_at)
            VALUES (2,%s,'a','upload','retrying',1,%s,%s) RETURNING id""", (file_id, stamp, stamp)).fetchone()["id"]
        c.execute(INSERT_INTENT, ("intent-1", "token-1", file_id, job, True, stamp))
        intents = c.execute("SELECT * FROM upload_intents ORDER BY id").fetchall()
    with test.assertRaises(psycopg.errors.UniqueViolation, msg="0068 allows one intent per Job"):
        with connect() as c:
            c.execute(INSERT_INTENT, ("intent-2", "token-2", file_id, job, False, stamp))
    upgraded = run("0069_upload_intent_per_attempt")
    test.assertEqual(upgraded.returncode, 0, upgraded.stderr)
    with connect() as c:
        test.assertEqual(c.execute("SELECT * FROM upload_intents ORDER BY id").fetchall(), intents, "DDL rewrites no intent")
        c.execute(INSERT_INTENT, ("intent-2", "token-2", file_id, job, False, stamp))
    with test.assertRaises(psycopg.errors.UniqueViolation, msg="still one OPEN intent per Job"):
        with connect() as c:
            c.execute(INSERT_INTENT, ("intent-3", "token-3", file_id, job, False, stamp))
    refused = run("0068_unsupported_state", "downgrade")
    test.assertNotEqual(refused.returncode, 0)
    test.assertIn("more than one upload intent", refused.stderr)
    with connect() as c:
        test.assertEqual(c.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"], "0069_upload_intent_per_attempt")
        test.assertEqual(c.execute("SELECT count(*) n FROM upload_intents").fetchone()["n"], 2, "refusal precedes DDL; history is never deleted")
        c.execute("DELETE FROM upload_intents WHERE id='intent-2'")
    for revision, command in (("0068_unsupported_state", "downgrade"), ("0069_upload_intent_per_attempt", "upgrade")):
        result = run(revision, command)
        test.assertEqual(result.returncode, 0, result.stderr)
    with connect() as c:
        test.assertEqual(c.execute("SELECT * FROM upload_intents ORDER BY id").fetchall(), intents)
