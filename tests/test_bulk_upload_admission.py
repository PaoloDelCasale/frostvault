"""DB-free HTTP contract and bounded durable producer tests for #550."""
from contextlib import contextmanager
import unittest
import uuid
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from app import main
from app.services import bulk_upload_admission as bulk


class BulkUploadApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        main.app.dependency_overrides[main.current_user] = lambda: {"id": 1}
        main.app.dependency_overrides[main.current_vault] = lambda: {"id": 9}
        self.addCleanup(main.app.dependency_overrides.clear)

    def test_bulk_returns_202_without_catalog_enumeration_or_filesystem_or_background_task(self):
        group = str(uuid.uuid4())
        admission = {"group_id": group, "status": "pending", "admitted_count": 0, "total_bytes": 0}
        @contextmanager
        def db():
            yield Mock()
        with patch.object(main, "db", db), patch.object(bulk, "create", return_value=admission) as create, \
                patch.object(main, "queue_jobs", side_effect=AssertionError("synchronous catalog traversal")), \
                patch.object(main, "vault_local_access", side_effect=AssertionError("filesystem probe")):
            response = self.client.post("/api/upload", json={"path": "photos", "is_directory": True, "request_id": group})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["admission"], admission)
        self.assertEqual(response.json()["job_ids"], [])
        self.assertEqual(create.call_args.kwargs, {"vault_id": 9, "path": "photos", "user_id": 1, "request_id": group})
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_single_file_keeps_existing_synchronous_admission(self):
        with patch.object(main, "queue_jobs", return_value={"group_id": "old", "item_count": 1}) as queue, \
                patch.object(bulk, "create", side_effect=AssertionError("bulk single file")):
            response = self.client.post("/api/upload", json={"path": "photo.jpg"})
        self.assertEqual(response.status_code, 202)
        queue.assert_called_once_with("photo.jpg", "upload", 9, 1, False)
        self.assertNotIn("admission", response.json())

    def test_status_is_vault_scoped_and_invalid_uuid_or_scope_is_rejected(self):
        group = str(uuid.uuid4())
        @contextmanager
        def db():
            yield Mock()
        with patch.object(main, "db", db), patch.object(bulk, "get", return_value=None) as get:
            self.assertEqual(self.client.get(f"/api/upload/admissions/{group}").status_code, 404)
        self.assertEqual(get.call_args.kwargs, {"vault_id": 9, "group_id": group})
        self.assertEqual(self.client.post("/api/upload", json={"path": "", "is_directory": True, "request_id": "bad"}).status_code, 422)
        for path in ("../outside", "/outside", "a/../../outside"):
            with self.subTest(path=path):
                self.assertIn(self.client.post("/api/upload", json={"path": path, "is_directory": True}).status_code, (400, 422))
        main.app.dependency_overrides.clear()
        self.assertEqual(self.client.get(f"/api/upload/admissions/{group}").status_code, 401)

    def test_worker_batch_size_is_hard_bounded(self):
        for size in (0, -1, bulk.BATCH_SIZE + 1):
            with self.assertRaises(ValueError):
                bulk.advance(Mock(), vault_id=9, group_id="g", batch_size=size)


class BulkProducerRotationTests(unittest.TestCase):
    def test_skipped_and_failed_attempts_rotate_past_the_vault_window(self):
        # Model only SQL selection/commits; advance remains the real tick seam.
        for failed in (False, True):
            with self.subTest(failed=failed):
                rows = [{"group_id": str(v), "vault_id": v, "attempt": 0}
                        for v in range(1, bulk.VAULT_BATCH + 2)]
                healthy = rows[-1]["vault_id"]
                attempted = []

                class Connection:
                    def execute(self, statement, params=()):
                        if "UPDATE bulk_upload_admissions" in statement:
                            for row in rows:
                                if row["group_id"] in params[1]:
                                    row["attempt"] += 1
                            return Mock()
                        ordered = sorted(rows, key=lambda row: (
                            row["attempt"] if "last_attempted_at" in statement else 0,
                            row["vault_id"],
                        ))
                        return Mock(fetchall=lambda: ordered[:bulk.VAULT_BATCH])

                @contextmanager
                def connect():
                    yield Connection()

                def advance(connection, *, vault_id, group_id):
                    attempted.append(vault_id)
                    if vault_id == healthy:
                        return True
                    if failed:
                        raise RuntimeError("batch unavailable")
                    return False

                with patch.object(bulk, "advance", side_effect=advance):
                    for _ in range(2):
                        if failed:
                            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                                bulk.tick(connect)
                        else:
                            bulk.tick(connect)
                self.assertIn(healthy, attempted)


class CombinedMigrationContractTests(unittest.TestCase):
    def test_release_head_joins_both_independent_feature_revisions(self):
        from pathlib import Path
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        from app.database import HEAD_SCHEMA_REVISION

        root = Path(__file__).resolve().parents[1]
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "migrations"))
        script = ScriptDirectory.from_config(config)
        self.assertEqual(script.get_heads(), [HEAD_SCHEMA_REVISION])
        # Later revisions build on the join; both feature branches stay required.
        self.assertIn("0071_bulk_scheduler_merge",
                      {item.revision for item in script.iterate_revisions(HEAD_SCHEMA_REVISION, "base")})
        self.assertEqual(set(script.get_revision("0071_bulk_scheduler_merge").down_revision),
                         {"bulk_upload_admission_550", "scheduler_turns_552"})


if __name__ == "__main__":
    unittest.main()
