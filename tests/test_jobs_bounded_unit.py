"""Fast materialization guard; SQL semantics are covered by test_jobs_bounded on PG16."""
import unittest
from unittest.mock import Mock, patch

from app.services.job_queries import list_jobs


class JobsMaterializationTests(unittest.TestCase):
    def test_handler_preserves_direct_call_defaults_and_positional_vault(self):
        from contextlib import contextmanager
        from starlette.requests import Request
        from app.main import jobs
        connection = Mock()
        @contextmanager
        def db():
            yield connection
        with patch("app.main.db", db), patch("app.services.job_queries.list_jobs", return_value={"items": [], "groups": []}) as query:
            self.assertEqual(jobs(Request({"type": "http", "headers": []}), {"id": 2})["items"], [])
        query.assert_called_once_with(connection,vault_id=2,locale="en",actions=None,
                                      active_only=False,path=None,groups_before_id=None)

    def test_group_size_never_changes_per_job_projection_or_localization_work(self):
        for count in (301, 30_001):
            with self.subTest(count=count):
                rows = [dict(id=i, path=f"folder/{i}", action="upload", status="queued", message="")
                        for i in range(50)]
                summary = dict(group_key="bulk", last_id=count, path="folder/1", group_path="folder",
                               action="upload", message="", message_key=None, message_params=None,
                               active_status="queued", item_count=count, completed_count=count-1,
                               failed_count=0, resolved_failed_count=0, cancelled_count=0,
                               total_bytes=count*100, transferred_bytes=(count-1)*100,
                               updated_at="2026-11-11", pending_until=None)
                connection = Mock()
                connection.execute.side_effect = [Mock(fetchall=lambda: rows), Mock(fetchall=lambda: [summary])]
                with patch("app.services.job_queries.project_jobs", side_effect=lambda c,r:r) as project:
                    with patch("app.services.job_queries.present_job_message", return_value="") as localize:
                        result = list_jobs(connection,vault_id=1,locale="en")
                self.assertEqual(len(result["items"]),50)
                self.assertEqual(result["groups"][0]["item_count"],count)
                self.assertEqual(len(project.call_args.args[1]),50)
                self.assertEqual(localize.call_count,51)
                self.assertEqual(connection.execute.call_count,2)

    def test_terminal_zero_byte_progress_and_failure_counts(self):
        summary = dict(group_key="empty",last_id=3,path="empty",group_path=None,action="recover",
                       message="",message_key=None,message_params=None,active_status=None,
                       item_count=3,completed_count=1,failed_count=1,resolved_failed_count=1,
                       cancelled_count=1,total_bytes=0,transferred_bytes=0)
        connection = Mock()
        connection.execute.side_effect = [Mock(fetchall=lambda: []),Mock(fetchall=lambda: [summary])]
        result = list_jobs(connection,vault_id=1,locale="en")
        group = result["groups"][0]
        self.assertEqual((group["status"],group["percent"],group["unresolved_failed_count"]),("failed",100,0))
        self.assertEqual(group["message_key"],"job.operation_failed")
