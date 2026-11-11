"""Bounded owner job polling against the real, disposable PostgreSQL catalog."""
import unittest
from unittest.mock import patch

from app.services.upload_remediation import project_jobs
from tests.postgres import PostgresConnection
from tests import test_job_messages as fixtures


class BoundedJobsHttpTests(unittest.TestCase):
    setUp = fixtures.JobMessageHttpTests.setUp
    PASSWORD = fixtures.JobMessageHttpTests.PASSWORD

    def seed_group(self, count=301):
        with PostgresConnection(self.database_path) as connection:
            connection.execute("""
                INSERT INTO jobs(vault_id,vault_file_id,path,action,status,message,
                    message_key,message_params,requested_at,updated_at,group_id,group_path,
                    total_bytes,transferred_bytes)
                SELECT vault_id,vault_file_id,'a.txt','upload',
                    CASE WHEN n=%s THEN 'queued' ELSE 'completed' END,
                    '', 'job.upload_verified','{}',requested_at,updated_at,'bulk','folder',100,0
                FROM jobs CROSS JOIN generate_series(1,%s) n WHERE id=(SELECT min(id) FROM jobs)
                """, (count,count))

    def test_active_group_does_not_expand_poll_items_or_projection_work(self):
        self.seed_group()
        with patch("app.services.job_queries.project_jobs", wraps=project_jobs) as projection:
            response = self.client.get(f"/api/jobs?vault_id={self.vault_id}")
        self.assertEqual(response.status_code,200,response.text)
        body = response.json()
        self.assertLessEqual(len(body["items"]),50,"active group leaked into bounded recent items")
        self.assertLessEqual(sum(len(call.args[1]) for call in projection.call_args_list),50,
                             "polling must not project every member of an active group")
        group = next(group for group in body["groups"] if group["id"] == "bulk")
        self.assertEqual((group["item_count"],group["completed_count"],group["total_bytes"],
                          group["transferred_bytes"],group["status"]),(301,300,30100,30000,"queued"))
        self.assertLess(len(response.content),100_000)

    def test_large_group_has_the_same_bounded_response_and_complete_sql_totals(self):
        self.seed_group(30_001)
        response = self.client.get(f"/api/jobs?vault_id={self.vault_id}")
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(len(response.json()["items"]),50)
        group = next(group for group in response.json()["groups"] if group["id"] == "bulk")
        self.assertEqual((group["item_count"],group["completed_count"],group["percent"]),(30_001,30_000,100))
        self.assertLess(len(response.content),100_000)

    def test_group_membership_is_keyset_paginated_without_duplicates_and_vault_scoped(self):
        self.seed_group(121)
        ids = []
        cursor = None
        while True:
            response = self.client.get(f"/api/job-groups/bulk/jobs?vault_id={self.vault_id}&limit=17" +
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
        self.assertEqual(self.client.get(f"/api/job-groups/bulk/jobs?vault_id={self.vault_id}&limit=51").status_code,422)
        self.assertEqual(self.client.get("/api/job-groups/bulk/jobs?vault_id=999999").status_code,404)
        self.client.cookies.clear()
        self.assertEqual(self.client.get(f"/api/job-groups/bulk/jobs?vault_id={self.vault_id}").status_code,401)

    def test_filters_precede_summary_window_and_old_file_deletion_is_not_hidden(self):
        self.seed_group()
        with PostgresConnection(self.database_path) as connection:
            connection.execute("UPDATE jobs SET status='completed'")
            connection.execute("""INSERT INTO jobs(vault_id,vault_file_id,path,action,status,
                requested_at,updated_at,group_id,group_path,pending_until)
                SELECT vault_id,vault_file_id,path,'cloud-purge','pending_delay',
                    '2000-01-01','2000-01-01','old-deletion','parent-folder','2099-01-01'
                FROM jobs ORDER BY id LIMIT 1""")
        body = self.client.get(f"/api/jobs?vault_id={self.vault_id}&action=cloud-purge&active_only=true&path=a.txt").json()
        self.assertEqual([row["id"] for row in body["groups"]],["old-deletion"])
        self.assertEqual(body["groups"][0]["pending_until"],"2099-01-01")
        self.assertEqual(len(body["items"]),1)
        self.assertEqual(self.client.get(f"/api/jobs?vault_id={self.vault_id}&path=other.txt").json()["groups"],[])
        self.assertEqual(self.client.get(f"/api/jobs?vault_id={self.vault_id}&path=../private").status_code,422)

    def test_matching_group_identity_in_another_vault_never_leaks_members_or_totals(self):
        from app.catalog import ArchiveCatalog
        self.seed_group(3)
        with PostgresConnection(self.database_path) as connection:
            other = connection.execute("""INSERT INTO vaults(slug,name,source_root,s3_bucket,s3_prefix)
                VALUES ('other','Other','/other-source','bucket','other') RETURNING id""").fetchone()["id"]
            ArchiveCatalog(connection).observe_local_copy(vault_id=other,path="secret.txt",file_type="regular",
                                                         size=999,mtime_ns=1,observed_at="2026-11-11")
            connection.execute("""INSERT INTO jobs(vault_id,vault_file_id,path,action,status,
                requested_at,updated_at,group_id,total_bytes)
                SELECT %s,id,'secret.txt','upload','queued','2026-11-11','2026-11-11','bulk',999
                FROM vault_files WHERE vault_id=%s""",(other,other))
        response = self.client.get(f"/api/jobs?vault_id={self.vault_id}")
        self.assertEqual(response.status_code,200,response.text)
        group = next(group for group in response.json()["groups"] if group["id"] == "bulk")
        self.assertEqual((group["item_count"],group["total_bytes"]),(3,300))
        detail = self.client.get(f"/api/job-groups/bulk/jobs?vault_id={self.vault_id}").json()
        self.assertEqual(len(detail["items"]),3)
        self.assertNotIn("secret.txt",[row["path"] for row in detail["items"]])

    def test_summary_pages_include_complete_terminal_groups_not_just_recent_members(self):
        self.seed_group(101)
        with PostgresConnection(self.database_path) as connection:
            connection.execute("UPDATE jobs SET status='completed',group_id='group-' || id::text")
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
        self.seed_group(52)
        with PostgresConnection(self.database_path) as connection:
            connection.execute("UPDATE jobs SET status='queued',group_id='single-' || id::text")
            connection.execute("""UPDATE jobs SET group_id='long'
                WHERE id IN ((SELECT min(id) FROM jobs),(SELECT max(id) FROM jobs))""")
        first = self.client.get(f"/api/jobs?vault_id={self.vault_id}&active_only=true")
        self.assertEqual(first.status_code,200,first.text)
        first = first.json()
        first_ids = {row["id"] for row in first["groups"]}
        self.assertIn("long",first_ids)
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
        self.seed_group(5)
        with PostgresConnection(self.database_path) as connection:
            connection.execute("UPDATE jobs SET group_id='bulk',group_path='folder',total_bytes=100")
            connection.execute("UPDATE jobs SET status='failed',transferred_bytes=150 WHERE id=(SELECT min(id) FROM jobs)")
            connection.execute("UPDATE jobs SET status='cancelled',transferred_bytes=30 WHERE id=(SELECT min(id)+1 FROM jobs)")
            rows = connection.execute("SELECT * FROM jobs ORDER BY requested_at DESC,id DESC").fetchall()
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
        from app import storage
        from tests import test_upload_remediation as remediation_fixtures
        case = remediation_fixtures.UploadRemediationTests("test_post_ack_original_preserved_resolution_requires_completed_cleanup")
        case.setUp()
        self.addCleanup(case.doCleanups)
        original = case.fail_after_ack()
        resumed = case.admit()
        case.run_job(resumed)
        case.assert_keeper(resumed)
        with case.db() as connection:
            connection.execute("UPDATE jobs SET group_id='resolved-bulk',group_path='folder'")
            connection.execute("""INSERT INTO jobs(vault_id,vault_file_id,path,action,status,
                requested_at,updated_at,group_id,group_path)
                VALUES (2,%s,'report.txt','upload','failed',%s,%s,'resolved-bulk','folder')""",
                (case.file_id,storage.now_iso(),storage.now_iso()))
        with patch("app.main._allowed_hosts",return_value=[]):
            response = case.http_client().get("/api/jobs?vault_id=2")
        self.assertEqual(response.status_code,200,response.text)
        group = response.json()["groups"][0]
        self.assertEqual((group["status"],group["failed_count"],group["resolved_failed_count"],
                          group["unresolved_failed_count"],group["completed_count"]),("failed",2,1,1,1))
        self.assertEqual(case.row(original)["status"],"failed")
