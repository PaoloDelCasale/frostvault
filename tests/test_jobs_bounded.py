"""Bounded owner job polling against the real, disposable PostgreSQL catalog."""
import unittest
from unittest.mock import patch

from app.services.upload_remediation import project_jobs
from tests.postgres import PostgresConnection
from tests import test_job_messages as fixtures

# Job groups are uuids scoped to one Vault (``job_groups``); fixed ids keep the
# assertions readable.
BULK = "00000000-0000-0000-0000-0000000b01c0"
OLD_DELETION = "00000000-0000-0000-0000-0000000de1e7"
LONG = "00000000-0000-0000-0000-00000000107e"


def add_group(connection, group_id, vault_id, path="folder"):
    connection.execute("INSERT INTO job_groups(id, vault_id, path) VALUES (%s, %s, %s) "
                       "ON CONFLICT (id) DO NOTHING", (group_id, vault_id, path))


class BoundedJobsHttpTests(unittest.TestCase):
    setUp = fixtures.JobMessageHttpTests.setUp
    PASSWORD = fixtures.JobMessageHttpTests.PASSWORD

    def seed_group(self, count=301, *, active=True):
        """``count`` uploads of a.txt in group BULK; the last one queued when ``active``."""
        with PostgresConnection(self.database_path) as connection:
            add_group(connection, BULK, self.vault_id)
            connection.execute("""
                INSERT INTO jobs(vault_id,file_id,action,status,message,
                    message_key,message_params,requested_at,updated_at,group_id,
                    total_bytes,transferred_bytes)
                SELECT vault_id,file_id,'upload',
                    CASE WHEN n=%s AND %s THEN 'queued' ELSE 'completed' END,
                    '', 'job.upload_verified','{}',requested_at,updated_at,%s,100,0
                FROM jobs CROSS JOIN generate_series(1,%s) n WHERE id=(SELECT min(id) FROM jobs)
                """, (count, active, BULK, count))

    def test_active_group_does_not_expand_poll_items_or_projection_work(self):
        self.seed_group()
        with patch("app.services.job_queries.project_jobs", wraps=project_jobs) as projection:
            response = self.client.get(f"/api/jobs?vault_id={self.vault_id}")
        self.assertEqual(response.status_code,200,response.text)
        body = response.json()
        self.assertLessEqual(len(body["items"]),50,"active group leaked into bounded recent items")
        self.assertLessEqual(sum(len(call.args[1]) for call in projection.call_args_list),50,
                             "polling must not project every member of an active group")
        group = next(group for group in body["groups"] if group["id"] == BULK)
        self.assertEqual((group["item_count"],group["completed_count"],group["total_bytes"],
                          group["transferred_bytes"],group["status"]),(301,300,30100,30000,"queued"))
        self.assertLess(len(response.content),100_000)

    def test_large_group_has_the_same_bounded_response_and_complete_sql_totals(self):
        self.seed_group(30_001)
        response = self.client.get(f"/api/jobs?vault_id={self.vault_id}")
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(len(response.json()["items"]),50)
        group = next(group for group in response.json()["groups"] if group["id"] == BULK)
        self.assertEqual((group["item_count"],group["completed_count"],group["percent"]),(30_001,30_000,100))
        self.assertLess(len(response.content),100_000)

    def test_group_membership_is_keyset_paginated_without_duplicates_and_vault_scoped(self):
        self.seed_group(121)
        ids = []
        cursor = None
        while True:
            response = self.client.get(f"/api/job-groups/{BULK}/jobs?vault_id={self.vault_id}&limit=17" +
                                       (f"&before_id={cursor}" if cursor else ""))
            self.assertEqual(response.status_code,200,response.text)
            body = response.json()
            self.assertLessEqual(len(body["items"]),17)
            ids.extend(row["id"] for row in body["items"])
            cursor = body["next_before_id"]
            if cursor is None:
                break
        self.assertEqual(len(ids),121)
        self.assertEqual(len(set(ids)),121)
        self.assertEqual(ids,sorted(ids,reverse=True))
        self.assertEqual(self.client.get(f"/api/job-groups/{BULK}/jobs?vault_id={self.vault_id}&limit=51").status_code,422)
        self.assertEqual(self.client.get("/api/job-groups/{BULK}/jobs?vault_id=999999").status_code,404)
        self.client.cookies.clear()
        self.assertEqual(self.client.get(f"/api/job-groups/{BULK}/jobs?vault_id={self.vault_id}").status_code,401)

    def test_filters_precede_summary_window_and_old_file_deletion_is_not_hidden(self):
        self.seed_group(active=False)
        with PostgresConnection(self.database_path) as connection:
            add_group(connection, OLD_DELETION, self.vault_id, "parent-folder")
            connection.execute("""INSERT INTO jobs(vault_id,file_id,action,status,
                requested_at,updated_at,group_id,run_after)
                SELECT vault_id,file_id,'cloud-purge','pending_delay',
                    '2000-01-01','2000-01-01',%s,'2099-01-01T00:00:00+00:00'
                FROM jobs ORDER BY id LIMIT 1""", (OLD_DELETION,))
        body = self.client.get(f"/api/jobs?vault_id={self.vault_id}&action=cloud-purge&active_only=true&path=a.txt").json()
        self.assertEqual([row["id"] for row in body["groups"]],[OLD_DELETION])
        self.assertTrue(str(body["groups"][0]["pending_until"]).startswith("2099-01-01"))
        self.assertEqual(len(body["items"]),1)
        self.assertEqual(self.client.get(f"/api/jobs?vault_id={self.vault_id}&path=other.txt").json()["groups"],[])
        self.assertEqual(self.client.get(f"/api/jobs?vault_id={self.vault_id}&path=../private").status_code,422)

    def test_matching_group_identity_in_another_vault_never_leaks_members_or_totals(self):
        from app.catalog import ArchiveCatalog
        import psycopg
        from tests.owner_fixtures import insert_vault
        self.seed_group(3)
        with PostgresConnection(self.database_path) as connection:
            other = insert_vault(connection, name="Other", source_root="/other-source")["id"]
            secret = ArchiveCatalog(connection).observe_local_copy(vault_id=other,path="secret.txt",file_type="regular",
                                                                  size=999,mtime_ns=1,observed_at="2026-11-11")
        # The schema itself scopes a group to its Vault: another Vault's Job
        # can never join it.
        with PostgresConnection(self.database_path) as connection, \
                self.assertRaises(psycopg.errors.ForeignKeyViolation):
            connection.execute("""INSERT INTO jobs(vault_id,file_id,action,status,
                requested_at,updated_at,group_id,total_bytes)
                VALUES (%s,%s,'upload','queued','2026-11-11','2026-11-11',%s,999)""",(other,secret,BULK))
        response = self.client.get(f"/api/jobs?vault_id={self.vault_id}")
        self.assertEqual(response.status_code,200,response.text)
        group = next(group for group in response.json()["groups"] if group["id"] == BULK)
        self.assertEqual((group["item_count"],group["total_bytes"]),(3,300))
        detail = self.client.get(f"/api/job-groups/{BULK}/jobs?vault_id={self.vault_id}").json()
        self.assertEqual(len(detail["items"]),3)
        self.assertNotIn("secret.txt",[row["path"] for row in detail["items"]])

    def test_summary_pages_include_complete_terminal_groups_not_just_recent_members(self):
        with PostgresConnection(self.database_path) as connection:
            # 101 complete terminal groups of one Job each, beside the
            # fixture's ungrouped Job.
            for _ in range(101):
                group = connection.execute("INSERT INTO job_groups(vault_id, path) VALUES (%s, 'folder') RETURNING id",
                                           (self.vault_id,)).fetchone()["id"]
                connection.execute("""INSERT INTO jobs(vault_id,file_id,action,status,group_id,total_bytes)
                    SELECT vault_id,file_id,'upload','completed',%s,100 FROM jobs
                    WHERE id=(SELECT min(id) FROM jobs)""", (group,))
        ids = []
        cursor = None
        while True:
            response = self.client.get(f"/api/jobs?vault_id={self.vault_id}" +
                                       (f"&groups_before_id={cursor}" if cursor else ""))
            self.assertEqual(response.status_code,200,response.text)
            body = response.json()
            self.assertLessEqual(len(body["groups"]),50)
            ids.extend(row["id"] for row in body["groups"])
            cursor = body["next_groups_before_id"]
            if cursor is None:
                break
        self.assertEqual(len(ids),102)
        self.assertEqual(len(set(ids)),102)

    def test_active_group_cursor_does_not_move_when_newest_member_completes(self):
        from app.catalog import ArchiveCatalog
        with PostgresConnection(self.database_path) as connection:
            # 53 active Jobs, one file each: 51 single-Job groups plus LONG,
            # which holds the oldest and the newest Job.
            catalog = ArchiveCatalog(connection)
            add_group(connection, LONG, self.vault_id)
            for n in range(53):
                path = f"cursor-{n}.txt"
                file_id = catalog.observe_local_copy(
                    vault_id=self.vault_id,path=path,file_type="regular",
                    size=100,mtime_ns=1,observed_at="2026-11-11")
                group = LONG if n in (0, 52) else connection.execute(
                    "INSERT INTO job_groups(vault_id, path) VALUES (%s, '') RETURNING id",
                    (self.vault_id,)).fetchone()["id"]
                connection.execute("""INSERT INTO jobs(vault_id,file_id,action,status,group_id,total_bytes)
                    VALUES (%s,%s,'upload','queued',%s,100)""",(self.vault_id,file_id,group))
        first = self.client.get(f"/api/jobs?vault_id={self.vault_id}&active_only=true")
        self.assertEqual(first.status_code,200,first.text)
        first = first.json()
        first_ids = {row["id"] for row in first["groups"]}
        self.assertIn(LONG,first_ids)
        cursor = first["next_groups_before_id"]
        self.assertIsNotNone(cursor)
        with PostgresConnection(self.database_path) as connection:
            connection.execute("UPDATE jobs SET status='completed' WHERE id=(SELECT max(id) FROM jobs)")
        second = self.client.get(f"/api/jobs?vault_id={self.vault_id}&active_only=true&groups_before_id={cursor}")
        self.assertEqual(second.status_code,200,second.text)
        second_ids = {row["id"] for row in second.json()["groups"]}
        self.assertTrue(second_ids)
        self.assertFalse(first_ids & second_ids,"completion moved a group across the cursor")
        self.assertEqual(len(first_ids | second_ids),52)

    def test_sql_weighted_progress_and_terminal_precedence_match_reference_reducer(self):
        from app.main import build_job_groups
        from app.services.upload_remediation import project_jobs
        self.seed_group(4)
        with PostgresConnection(self.database_path) as connection:
            # Terminal Jobs are written once: the failed and cancelled members
            # join the group as their own rows.
            for status, moved in (("failed", 150), ("cancelled", 30)):
                connection.execute("""INSERT INTO jobs(vault_id,file_id,action,status,group_id,total_bytes,transferred_bytes,
                    requested_at,updated_at) SELECT vault_id,file_id,'upload',%s,%s,100,%s,requested_at,updated_at
                    FROM jobs WHERE id=(SELECT min(id) FROM jobs)""", (status, BULK, moved))
            from app.services.job_queries import JOB_COLUMNS, JOB_ROWS
            rows = connection.execute(f"WITH {JOB_ROWS} SELECT {JOB_COLUMNS} FROM job_rows "
                                      "ORDER BY requested_at DESC,id DESC").fetchall()
            expected = build_job_groups(project_jobs(connection,rows))[0]
        response = self.client.get(f"/api/jobs?vault_id={self.vault_id}")
        self.assertEqual(response.status_code,200,response.text)
        actual = response.json()["groups"][0]
        for field in ("status","item_count","completed_count","failed_count","cancelled_count",
                      "resolved_failed_count","unresolved_failed_count","total_bytes","transferred_bytes","percent"):
            self.assertEqual(actual[field],expected[field],field)
        with PostgresConnection(self.database_path) as connection:
            connection.execute("UPDATE jobs SET status='completed' WHERE status='queued'")
        self.assertEqual(self.client.get(f"/api/jobs?vault_id={self.vault_id}").json()["groups"][0]["status"],"failed")


class RemediatedGroupSqlTests(unittest.TestCase):
    def test_resolved_failures_are_counted_in_sql_without_rewriting_history(self):
        import uuid
        from app import storage
        from app.services.job_queries import list_jobs
        from tests import test_upload_remediation as remediation_fixtures
        case = remediation_fixtures.UploadRemediationTests("test_owner_cancel_is_durable")
        case.setUp()
        self.addCleanup(case.doCleanups)
        group = str(uuid.uuid4())

        def grouped(mode="s3_checksum"):
            with case.db() as connection:
                connection.execute("INSERT INTO job_groups(id, vault_id, path) VALUES (%s, 2, 'folder') "
                                   "ON CONFLICT DO NOTHING", (group,))
                job_id = connection.execute(
                    "INSERT INTO jobs(vault_id, file_id, action, status, origin, total_bytes, group_id) "
                    "VALUES (2, %s, 'upload', 'queued', 'manual', %s, %s) RETURNING id",
                    (case.file_id, len(remediation_fixtures.A), group)).fetchone()["id"]
                connection.execute("INSERT INTO job_uploads(job_id, verification_mode) VALUES (%s, %s)",
                                   (job_id, mode))
            return job_id
        case.queue = grouped
        original = case.fail_original("accepted")
        case.make_due()
        case.sweep()
        resumed = case.admitted()
        case.run_job(resumed)
        case.assert_current(resumed, remediation_fixtures.A)
        with case.db() as connection:
            # A later failure in the same request, with no cloud work left.
            connection.execute("""INSERT INTO jobs(vault_id, file_id, action, status, group_id,
                requested_at, updated_at, finished_at)
                VALUES (2, %s, 'upload', 'failed', %s, %s, %s, %s)""",
                (case.file_id, group, storage.now_iso(), storage.now_iso(), storage.now_iso()))
            body = list_jobs(connection, vault_id=2, locale="en")
        summary = next(row for row in body["groups"] if row["id"] == group)
        self.assertEqual((summary["status"], summary["failed_count"], summary["resolved_failed_count"],
                          summary["unresolved_failed_count"]), ("failed", 2, 1, 1))
        self.assertEqual(case.job(original)["status"], "failed")
        projected = {row["id"]: row for row in body["items"]}
        self.assertEqual(projected[original]["remediation"]["state"], "resolved")
        self.assertEqual(projected[resumed]["remediation"]["resolved_by_job_id"], resumed)
