from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.catalog import ArchiveCatalog
from app.database import SQLiteConnection
from app.services.lifecycle_policies import (
    PolicyAssignments,
    create_policy,
    load_policy_assignments,
    policy_object_tags,
    resolve_effective_policy_id,
    set_folder_override,
    set_vault_default_policy,
)
from app.services.policy_reconciliation import reconcile_pending_policy_tags
from app.services.s3_object_tags import apply_version_policy_tag
from tests.test_database import run_alembic


class ResolveEffectivePolicyIdTests(unittest.TestCase):
    def test_vault_default_applies_when_no_folder_override(self) -> None:
        assignments = PolicyAssignments(
            default_policy_id="11111111-1111-4111-8111-111111111111",
            folder_overrides=(),
        )
        self.assertEqual(
            resolve_effective_policy_id("docs/report.pdf", assignments),
            "11111111-1111-4111-8111-111111111111",
        )

    def test_longest_folder_override_wins(self) -> None:
        assignments = PolicyAssignments(
            default_policy_id="22222222-2222-4222-8222-222222222222",
            folder_overrides=(
                ("photos", "33333333-3333-4333-8333-333333333333"),
                ("photos/2024", "44444444-4444-4444-8444-444444444444"),
            ),
        )
        self.assertEqual(
            resolve_effective_policy_id("photos/2024/album.jpg", assignments),
            "44444444-4444-4444-8444-444444444444",
        )


class PolicyObjectTagsTests(unittest.TestCase):
    def test_policy_object_tags_use_stable_key(self) -> None:
        policy_id = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
        self.assertEqual(
            policy_object_tags(policy_id),
            {"psa:policy-id": policy_id},
        )


class ApplyVersionPolicyTagTests(unittest.TestCase):
    def test_apply_version_policy_tag_calls_put_object_tagging(self) -> None:
        client = Mock()
        apply_version_policy_tag(
            client,
            bucket="archive-bucket",
            key="vaults/uuid/docs/report.pdf",
            version_id="ver-1",
            policy_id="3fa85f64-5717-4562-b3fc-2c963f66afa6",
        )
        client.put_object_tagging.assert_called_once_with(
            Bucket="archive-bucket",
            Key="vaults/uuid/docs/report.pdf",
            VersionId="ver-1",
            Tagging={
                "TagSet": [
                    {
                        "Key": "psa:policy-id",
                        "Value": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                    }
                ]
            },
        )


class LifecyclePolicyDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "policies.db"
        result = run_alembic(self.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        with SQLiteConnection(str(self.path)) as connection:
            connection.execute(
                "INSERT INTO vaults(id, slug, name, source_root, s3_bucket, s3_prefix, rclone_remote) "
                "VALUES (1, 'docs', 'Docs', '/source', 'bucket', 'vaults/uuid', 'remote')"
            )

    def test_folder_override_updates_desired_policy_on_existing_versions(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            default_policy = create_policy(connection, vault_id=1, name="default")
            override_policy = create_policy(connection, vault_id=1, name="photos")
            set_vault_default_policy(connection, 1, default_policy)
            catalog = ArchiveCatalog(connection)
            catalog.record_archive_version(
                vault_id=1,
                path="photos/album.jpg",
                object_key="vaults/uuid/photos/album.jpg",
                provider_version_id="v1",
                size=10,
                storage_class="STANDARD",
                etag="etag",
                uploaded_at="2026-01-01T00:00:00+00:00",
                observed_at="2026-01-01T00:00:00+00:00",
                scan_id="2026-01-01T00:00:00+00:00",
                desired_policy_id=default_policy,
                applied_policy_id=default_policy,
            )
            set_folder_override(
                connection,
                vault_id=1,
                folder_path="photos",
                policy_id=override_policy,
            )
            row = connection.execute(
                "SELECT desired_policy_id, applied_policy_id FROM archive_versions"
            ).fetchone()
        self.assertEqual(row["desired_policy_id"], override_policy)
        self.assertEqual(row["applied_policy_id"], default_policy)

    def test_load_policy_assignments_reads_defaults_and_overrides(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            default_policy = create_policy(connection, vault_id=1, name="default")
            override_policy = create_policy(connection, vault_id=1, name="photos")
            set_vault_default_policy(connection, 1, default_policy)
            set_folder_override(
                connection,
                vault_id=1,
                folder_path="photos",
                policy_id=override_policy,
            )
            assignments = load_policy_assignments(connection, 1)
        self.assertEqual(assignments.default_policy_id, default_policy)
        self.assertEqual(assignments.folder_overrides, (("photos", override_policy),))


class PolicyReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "reconcile.db"
        result = run_alembic(self.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        with SQLiteConnection(str(self.path)) as connection:
            connection.execute(
                "INSERT INTO vaults(id, slug, name, source_root, s3_bucket, s3_prefix, rclone_remote) "
                "VALUES (1, 'docs', 'Docs', '/source', 'bucket', 'vaults/uuid', 'remote')"
            )
            policy_id = create_policy(connection, vault_id=1, name="default")
            set_vault_default_policy(connection, 1, policy_id)
            catalog = ArchiveCatalog(connection)
            catalog.record_archive_version(
                vault_id=1,
                path="docs/report.pdf",
                object_key="vaults/uuid/docs/report.pdf",
                provider_version_id="v1",
                size=10,
                storage_class="STANDARD",
                etag="etag",
                uploaded_at="2026-01-01T00:00:00+00:00",
                observed_at="2026-01-01T00:00:00+00:00",
                scan_id="2026-01-01T00:00:00+00:00",
                desired_policy_id=policy_id,
                applied_policy_id=None,
            )
            self.policy_id = policy_id

    def test_reconcile_pending_policy_tags_applies_s3_tag_and_records_applied_id(
        self,
    ) -> None:
        client = Mock()
        vault = {
            "id": 1,
            "s3_bucket": "bucket",
        }
        with SQLiteConnection(str(self.path)) as connection:
            applied = reconcile_pending_policy_tags(connection, vault, client)
            row = connection.execute(
                "SELECT applied_policy_id FROM archive_versions"
            ).fetchone()
        self.assertEqual(applied, 1)
        self.assertEqual(row["applied_policy_id"], self.policy_id)
        client.put_object_tagging.assert_called_once()


class UploadPolicyTagIntegrationTests(unittest.TestCase):
    def test_plain_upload_tags_object_version_and_records_policy_ids(self) -> None:
        from app.storage import process_upload

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "upload-policy.db"
        source_root = Path(tmp.name) / "source"
        source_root.mkdir()
        source_file = source_root / "docs" / "report.pdf"
        source_file.parent.mkdir()
        source_file.write_bytes(b"hello")

        result = run_alembic(path)
        self.assertEqual(result.returncode, 0, result.stderr)
        with SQLiteConnection(str(path)) as connection:
            connection.execute(
                "INSERT INTO vaults(id, slug, name, source_root, s3_bucket, s3_prefix, rclone_remote) "
                "VALUES (1, 'docs', 'Docs', %s, 'bucket', 'vaults/uuid', 'remote')",
                (str(source_root),),
            )
            policy_id = create_policy(connection, vault_id=1, name="default")
            set_vault_default_policy(connection, 1, policy_id)
            self.policy_id = policy_id

        s3 = SimpleNamespace(
            head_object=lambda **_: {
                "VersionId": "ver-123",
                "ContentLength": 5,
                "StorageClass": "STANDARD",
                "ETag": '"abc"',
            },
            put_object_tagging=Mock(),
            head_bucket=lambda **_: None,
            get_bucket_location=lambda **_: {"LocationConstraint": "eu-south-1"},
            get_bucket_versioning=lambda **_: {"Status": "Enabled"},
        )

        def fake_rclone(*args, **kwargs) -> None:
            command = [str(arg) for arg in args if not callable(arg)]
            if command[:1] != ["copyto"] or len(command) < 3:
                return
            origin, destination = command[1], command[2]
            if ":" in origin and not Path(origin).exists():
                target = Path(destination)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"hello")

        def fake_rclone_stream(*args, **kwargs) -> int:
            kwargs["on_chunk"](b"hello")
            return 5

        with (
            patch("app.storage.s3_client", return_value=s3),
            patch("app.storage.run_rclone", side_effect=fake_rclone),
            patch("app.storage.run_rclone_stream", side_effect=fake_rclone_stream),
            patch("app.storage.settings") as mock_settings,
            patch("app.storage.db", side_effect=lambda: SQLiteConnection(str(path))),
        ):
            mock_settings.aws_region = "eu-south-1"
            process_upload(
                {
                    "id": 9,
                    "vault_id": 1,
                    "path": "docs/report.pdf",
                    "source_root": str(source_root),
                    "s3_bucket": "bucket",
                    "s3_prefix": "vaults/uuid",
                    "rclone_remote": "remote",
                    "encryption_mode": "plain",
                    "total_bytes": 5,
                }
            )

        s3.put_object_tagging.assert_called_once()
        with SQLiteConnection(str(path)) as connection:
            row = connection.execute(
                "SELECT desired_policy_id, applied_policy_id FROM archive_versions"
            ).fetchone()
        self.assertEqual(row["desired_policy_id"], policy_id)
        self.assertEqual(row["applied_policy_id"], policy_id)
