"""Populated 0065 -> 0066: metadata-only table; an open multipart obligation refuses downgrade."""
import psycopg

from app.catalog import ArchiveCatalog


def assert_upload_multipart_journal_migration(test, run, connect):
    result = run("0065_upload_remediation")
    test.assertEqual(result.returncode, 0, result.stderr)
    stamp = "2026-11-07T00:00:00+00:00"
    with connect() as c:
        c.execute("INSERT INTO users(id,username,display_name,password_hash) VALUES (1,'owner','Owner','hash')")
        c.execute("INSERT INTO vaults(id,slug,name,source_root,s3_bucket,s3_prefix) VALUES (2,'docs','Docs','/nonexistent/no-filesystem-ddl','bucket','docs')")
        file_id = ArchiveCatalog(c).observe_local_copy(vault_id=2, path="a", file_type="regular", size=1, mtime_ns=100, observed_at=stamp)
        job = c.execute("""INSERT INTO jobs(vault_id,vault_file_id,path,action,status,requested_by,requested_at,updated_at)
            VALUES (2,%s,'a','upload','failed',1,%s,%s) RETURNING id""", (file_id, stamp, stamp)).fetchone()["id"]
        c.execute("""INSERT INTO upload_intents(id,token,vault_id,vault_file_id,original_job_id,target_json,snapshot_json,requested,inventory_json,send_started,created_at)
            VALUES ('intent-1','token-1',2,%s,%s,'{}','{}','read_back','{}',TRUE,%s)""", (file_id, job, stamp))
        cancelled = c.execute("""INSERT INTO jobs(vault_id,vault_file_id,path,action,status,requested_by,requested_at,updated_at)
            VALUES (2,%s,'a','upload','cancelled',1,%s,%s) RETURNING id""", (file_id, stamp, stamp)).fetchone()["id"]
        c.execute("""INSERT INTO upload_intents(id,token,vault_id,vault_file_id,original_job_id,target_json,snapshot_json,requested,inventory_json,send_started,created_at)
            VALUES ('intent-2','token-2',2,%s,%s,'{}','{}','read_back','{}',TRUE,%s)""", (file_id, cancelled, stamp))
        before = {table: c.execute(f"SELECT * FROM {table} ORDER BY id").fetchall() for table in ("vaults", "jobs")}
        intents = c.execute("SELECT * FROM upload_intents").fetchall()
    upgraded = run("0066_upload_multipart_journal")
    test.assertEqual(upgraded.returncode, 0, upgraded.stderr)
    with connect() as c:
        test.assertEqual({table: c.execute(f"SELECT * FROM {table} ORDER BY id").fetchall() for table in before}, before)
        test.assertEqual(c.execute("SELECT * FROM upload_intents").fetchall(), intents)
        test.assertEqual(c.execute("SELECT count(*) n FROM upload_multiparts").fetchone()["n"], 0, "DDL manufactures no authority to abort")
        test.assertEqual(c.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"], "0066_upload_multipart_journal")
        c.execute("INSERT INTO upload_multiparts(intent_id,upload_id,created_at) VALUES ('intent-1','up-1',%s)", (stamp,))
        c.execute("INSERT INTO upload_multiparts(intent_id,upload_id,created_at) VALUES ('intent-2','up-cancelled',%s)", (stamp,))
        test.assertEqual(c.execute("SELECT state,closed_at FROM upload_multiparts WHERE upload_id='up-1'").fetchone(), {"state": "open", "closed_at": None})
    for statement in ("INSERT INTO upload_multiparts(intent_id,upload_id,created_at) VALUES ('intent-1','up-1',%s)",
                      "INSERT INTO upload_multiparts(intent_id,upload_id,state,created_at) VALUES ('intent-1','up-2','closed',%s)",
                      "INSERT INTO upload_multiparts(intent_id,upload_id,state,created_at) VALUES ('intent-1','up-3','unknown',%s)"):
        with test.assertRaises(psycopg.errors.IntegrityError, msg=statement):
            with connect() as c:
                c.execute(statement, (stamp,))
    refused = run("0065_upload_remediation", "downgrade")
    test.assertNotEqual(refused.returncode, 0)
    test.assertIn("Abort or reconcile open multipart uploads", refused.stderr)
    with connect() as c:
        test.assertEqual(c.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"], "0066_upload_multipart_journal")
        test.assertEqual(c.execute("SELECT count(*) n FROM upload_multiparts").fetchone()["n"], 2, "Refusal precedes DDL")
        # Only the intent's own obligation matters: once it is settled the row (still
        # open) no longer blocks the downgrade.
        # intent-2 (cancelled Job, never resumable) never counted: settling intent-1
        # alone lifts the refusal while both rows are still open.
        c.execute("UPDATE upload_intents SET settled=TRUE WHERE id='intent-1'")
        test.assertEqual({r["state"] for r in c.execute("SELECT state FROM upload_multiparts").fetchall()}, {"open"})
        intents = c.execute("SELECT * FROM upload_intents").fetchall()
    for revision, command in (("0065_upload_remediation", "downgrade"), ("0066_upload_multipart_journal", "upgrade")):
        result = run(revision, command)
        test.assertEqual(result.returncode, 0, result.stderr)
    with connect() as c:
        test.assertEqual({table: c.execute(f"SELECT * FROM {table} ORDER BY id").fetchall() for table in before}, before)
        test.assertEqual(c.execute("SELECT * FROM upload_intents").fetchall(), intents)
        test.assertEqual(c.execute("SELECT count(*) n FROM upload_multiparts").fetchone()["n"], 0, "rows of a settled or cancelled intent carry no obligation")
