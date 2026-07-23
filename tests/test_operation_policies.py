"""Per-Vault operation policies (issue #12).

Seams under test:
- ``app.services.operation_policies`` — get/set policy defaults, glob match +
  preview, stability window, effective bandwidth, auto-upload eligibility.
- Defaults must keep upload manual and apply no implicit exclusions.
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.catalog import ArchiveCatalog
from app.database import SQLiteConnection
from app.services.operation_policies import (
    OperationPolicy,
    effective_bandwidth_kibps,
    file_is_stable,
    get_policy,
    path_is_included,
    preview_glob_rules,
    set_policy,
)
from tests.test_database import run_alembic


class OperationPolicyDefaultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "policies.db"
        result = run_alembic(self.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        with SQLiteConnection(str(self.path)) as connection:
            connection.execute(
                "INSERT INTO vaults(id, slug, name, source_root, s3_bucket, s3_prefix, rclone_remote) "
                "VALUES (1, 'docs', 'Docs', '/source', 'bucket', 'docs', 'remote')"
            )

    def test_missing_row_means_manual_upload_and_no_exclusions(self) -> None:
        with SQLiteConnection(str(self.path)) as connection:
            policy = get_policy(connection, 1)
        self.assertEqual(
            policy,
            OperationPolicy(
                auto_upload=False,
                stability_seconds=300,
                include_globs=(),
                exclude_globs=(),
                bandwidth_limit_kibps=None,
                operating_windows=(),
            ),
        )
        self.assertTrue(path_is_included("photos/raw/IMG_001.CR2", policy))
        self.assertTrue(path_is_included("notes/todo.txt", policy))


class GlobRuleTests(unittest.TestCase):
    def test_exclude_wins_over_include(self) -> None:
        policy = OperationPolicy(
            include_globs=("**/*.txt", "docs/**"),
            exclude_globs=("docs/secret/**",),
        )
        self.assertTrue(path_is_included("readme.txt", policy))
        self.assertTrue(path_is_included("docs/public/a.txt", policy))
        self.assertFalse(path_is_included("docs/secret/key.pem", policy))
        self.assertFalse(path_is_included("photo.jpg", policy))

    def test_preview_lists_included_and_excluded_sample_paths(self) -> None:
        preview = preview_glob_rules(
            paths=(
                "readme.txt",
                "docs/public/a.txt",
                "docs/secret/key.pem",
                "photo.jpg",
            ),
            include_globs=("**/*.txt", "docs/**"),
            exclude_globs=("docs/secret/**",),
        )
        self.assertEqual(preview["included"], ["readme.txt", "docs/public/a.txt"])
        self.assertEqual(preview["excluded"], ["docs/secret/key.pem", "photo.jpg"])


class StabilityWindowTests(unittest.TestCase):
    def test_file_is_stable_only_after_window(self) -> None:
        mtime_ns = int(datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc).timestamp() * 1e9)
        before = datetime(2026, 7, 1, 12, 4, 59, tzinfo=timezone.utc)
        at_window = datetime(2026, 7, 1, 12, 5, 0, tzinfo=timezone.utc)
        self.assertFalse(
            file_is_stable(mtime_ns=mtime_ns, now=before, stability_seconds=300)
        )
        self.assertTrue(
            file_is_stable(mtime_ns=mtime_ns, now=at_window, stability_seconds=300)
        )


class LocalRetentionWindowTests(unittest.TestCase):
    def test_cleanup_becomes_due_from_verification_time_at_retention_boundary(self) -> None:
        from app.services.operation_policies import local_cleanup_is_due

        verified_at = "2026-07-01T12:00:00+00:00"
        self.assertFalse(
            local_cleanup_is_due(
                verified_at=verified_at,
                retention_days=30,
                now=datetime(2026, 7, 31, 11, 59, 59, tzinfo=timezone.utc),
            )
        )
        self.assertTrue(
            local_cleanup_is_due(
                verified_at=verified_at,
                retention_days=30,
                now=datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc),
            )
        )


class BandwidthPolicyTests(unittest.TestCase):
    def test_vault_override_beats_global_limit(self) -> None:
        self.assertEqual(
            effective_bandwidth_kibps(global_limit=1024, vault_limit=256),
            256,
        )
        self.assertEqual(
            effective_bandwidth_kibps(global_limit=1024, vault_limit=None),
            1024,
        )
        self.assertIsNone(effective_bandwidth_kibps(global_limit=None, vault_limit=None))


class OperationPolicyPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "policies.db"
        result = run_alembic(self.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        with SQLiteConnection(str(self.path)) as connection:
            connection.execute(
                "INSERT INTO vaults(id, slug, name, source_root, s3_bucket, s3_prefix, rclone_remote) "
                "VALUES (1, 'docs', 'Docs', '/source', 'bucket', 'docs', 'remote')"
            )

    def test_set_policy_round_trips(self) -> None:
        desired = OperationPolicy(
            auto_upload=True,
            auto_local_cleanup=True,
            local_retention_days=30,
            stability_seconds=600,
            include_globs=("**/*.pdf",),
            exclude_globs=("tmp/**",),
            bandwidth_limit_kibps=512,
            operating_windows=({"weekday": 0, "start": "09:00", "end": "17:00"},),
        )
        with SQLiteConnection(str(self.path)) as connection:
            stored = set_policy(connection, 1, desired)
            loaded = get_policy(connection, 1)
        self.assertEqual(stored, desired)
        self.assertEqual(loaded, desired)


class AutoUploadEligibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.path = self.root / "catalog.db"
        result = run_alembic(self.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        with SQLiteConnection(str(self.path)) as connection:
            connection.execute(
                "INSERT INTO users(id, username, display_name, password_hash, is_admin) "
                "VALUES (1, 'owner', 'Owner', 'hash', 0)"
            )
            connection.execute(
                "INSERT INTO vaults(id, slug, name, source_root, s3_bucket, s3_prefix, rclone_remote) "
                "VALUES (2, 'docs', 'Docs', %s, 'bucket', 'docs', 'remote')",
                (str(self.source),),
            )
            connection.execute(
                "INSERT INTO vault_members(vault_id, user_id, role) VALUES (2, 1, 'owner')"
            )

    def test_auto_upload_queues_only_stable_included_files(self) -> None:
        from app.services.operation_policies import queue_auto_uploads
        from app.storage import now_iso

        stable = self.source / "ready.txt"
        fresh = self.source / "fresh.txt"
        ignored = self.source / "skip.bin"
        stable.write_text("stable")
        fresh.write_text("fresh")
        ignored.write_text("nope")
        # Age the stable file's mtime beyond the default 300s window.
        old = datetime(2026, 7, 1, 11, 0, tzinfo=timezone.utc).timestamp()
        import os

        os.utime(stable, (old, old))
        now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)

        with SQLiteConnection(str(self.path)) as connection:
            catalog = ArchiveCatalog(connection)
            for name, path in (
                ("ready.txt", stable),
                ("fresh.txt", fresh),
                ("skip.bin", ignored),
            ):
                catalog.observe_local_copy(
                    vault_id=2,
                    path=name,
                    file_type="regular",
                    size=path.stat().st_size,
                    mtime_ns=path.stat().st_mtime_ns,
                    observed_at=now_iso(),
                )
            set_policy(
                connection,
                2,
                OperationPolicy(
                    auto_upload=True,
                    stability_seconds=300,
                    include_globs=("**/*.txt",),
                    exclude_globs=(),
                ),
            )
            queued = queue_auto_uploads(
                connection,
                vault_id=2,
                source_root=str(self.source),
                requested_by=1,
                now=now,
            )
            paths = [
                row["path"]
                for row in connection.execute(
                    "SELECT path FROM jobs WHERE action='upload' ORDER BY path"
                ).fetchall()
            ]
        self.assertEqual(queued, 1)
        self.assertEqual(paths, ["ready.txt"])

    def test_manual_default_queues_nothing(self) -> None:
        from app.services.operation_policies import queue_auto_uploads

        target = self.source / "ready.txt"
        target.write_text("stable")
        old = datetime(2026, 7, 1, 11, 0, tzinfo=timezone.utc).timestamp()
        import os

        os.utime(target, (old, old))
        now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
        with SQLiteConnection(str(self.path)) as connection:
            ArchiveCatalog(connection).observe_local_copy(
                vault_id=2,
                path="ready.txt",
                file_type="regular",
                size=target.stat().st_size,
                mtime_ns=target.stat().st_mtime_ns,
                observed_at="2026-07-01T11:00:00+00:00",
            )
            queued = queue_auto_uploads(
                connection,
                vault_id=2,
                source_root=str(self.source),
                requested_by=1,
                now=now,
            )
            total = connection.execute("SELECT COUNT(*) AS total FROM jobs").fetchone()[
                "total"
            ]
        self.assertEqual(queued, 0)
        self.assertEqual(total, 0)

    def test_auto_cleanup_queues_only_matching_versions_past_retention(self) -> None:
        from app.services.operation_policies import queue_auto_local_cleanups

        now = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
        due_version_id = None
        with SQLiteConnection(str(self.path)) as connection:
            catalog = ArchiveCatalog(connection)
            for name, verified_at in (
                ("due.txt", "2026-07-01T12:00:00+00:00"),
                ("not-due.txt", "2026-07-02T12:00:00+00:00"),
            ):
                target = self.source / name
                content = name.encode()
                target.write_bytes(content)
                digest = hashlib.sha256(content).hexdigest()
                catalog.observe_local_copy(
                    vault_id=2,
                    path=name,
                    file_type="regular",
                    size=len(content),
                    mtime_ns=target.stat().st_mtime_ns,
                    observed_at=now.isoformat(),
                )
                version_id = catalog.record_archive_version(
                    vault_id=2,
                    path=name,
                    object_key=f"docs/{name}",
                    provider_version_id=f"s3-{name}",
                    size=len(content),
                    storage_class="STANDARD",
                    etag="etag",
                    uploaded_at=verified_at,
                    observed_at=verified_at,
                    scan_id=verified_at,
                )
                if name == "due.txt":
                    due_version_id = version_id
                catalog.mark_version_verified(
                    version_id,
                    plaintext_sha256=digest,
                    verified_at=verified_at,
                )
                catalog.set_local_fingerprint(
                    vault_id=2,
                    path=name,
                    plaintext_sha256=digest,
                    matched_archive_version_id=version_id,
                )
            set_policy(
                connection,
                2,
                OperationPolicy(
                    auto_local_cleanup=True,
                    local_retention_days=30,
                    include_globs=("**/*.txt",),
                ),
            )

            queued = queue_auto_local_cleanups(
                connection,
                vault_id=2,
                requested_by=1,
                local_delete_enabled=True,
                now=now,
            )
            jobs = connection.execute(
                "SELECT id, path, action, archive_version_id, origin "
                "FROM jobs ORDER BY path"
            ).fetchall()
            audit_events = connection.execute(
                "SELECT event, job_id, outcome FROM audit_events ORDER BY id"
            ).fetchall()
            notifications = connection.execute(
                "SELECT event, job_id FROM notifications ORDER BY id"
            ).fetchall()

        self.assertEqual(queued, 1)
        self.assertEqual(
            jobs,
            [
                {
                    "id": jobs[0]["id"],
                    "path": "due.txt",
                    "action": "free-space",
                    "archive_version_id": due_version_id,
                    "origin": "automatic",
                }
            ],
        )
        self.assertEqual(
            audit_events,
            [
                {
                    "event": "local_cleanup.auto_queued",
                    "job_id": jobs[0]["id"],
                    "outcome": "queued",
                }
            ],
        )
        self.assertEqual(
            notifications,
            [
                {
                    "event": "local_cleanup.auto_queued",
                    "job_id": jobs[0]["id"],
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
