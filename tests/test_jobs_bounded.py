"""Bounded owner job polling against the real, disposable PostgreSQL catalog."""
import unittest
from unittest.mock import patch

from app.services.upload_remediation import project_jobs
from tests.postgres import PostgresConnection
from tests.test_job_messages import JobMessageHttpTests


class BoundedJobsHttpTests(unittest.TestCase):
    setUp = JobMessageHttpTests.setUp
    PASSWORD = JobMessageHttpTests.PASSWORD

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
        with patch("app.services.upload_remediation.project_jobs", wraps=project_jobs) as projection:
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
