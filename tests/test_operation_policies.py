"""Per-Vault operation policies (issue #12).

Seams under test:
- ``app.services.operation_policies`` — get/set policy defaults, glob match +
  preview, stability window, auto-upload eligibility.
- New Vaults auto-upload by default; fixed automatic noise exclusions cannot be overridden.
"""

from __future__ import annotations

from tests.postgres import PostgresConnection, database_dsn

import hashlib
import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app.catalog import ArchiveCatalog

from app.services.operation_policies import (
    OperationPolicy,
    auto_upload_path_is_included,
    file_is_stable,
    get_policy,
    path_is_included,
    preview_glob_rules,
    set_policy,
)
from tests.catalog_support import fingerprint, protect, seed_vault
from tests.owner_fixtures import insert_owner
from tests.test_database import run_alembic


def _protect(catalog, path: str, digest: str, provider_version_id: str, verified_at: str) -> str:
    """Fingerprint the Local Copy at ``path`` and make it a verified Current."""
    fingerprint(catalog, 2, path, digest)
    return protect(
        catalog, 2, path, digest, provider_version_id=provider_version_id,
        created_at=verified_at, verified_at=verified_at,
    )


class OperationPolicyDefaultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "policies.db"
        result = run_alembic(self.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        with PostgresConnection(str(self.path)) as connection:
            seed_vault(connection, vault_id=1)

    def test_missing_row_means_automatic_upload_and_no_custom_globs(self) -> None:
        with PostgresConnection(str(self.path)) as connection:
            policy = get_policy(connection, 1)
        self.assertEqual(
            policy,
            OperationPolicy(
                auto_upload=True,
                stability_seconds=300,
                include_globs=(),
                exclude_globs=(),
            ),
        )
        self.assertTrue(path_is_included("photos/raw/IMG_001.CR2", policy))
        self.assertTrue(path_is_included("notes/todo.txt", policy))


class GlobRuleTests(unittest.TestCase):
    def test_fixed_noise_exclusions_cannot_be_overridden_by_include_globs(self) -> None:
        policy = OperationPolicy(include_globs=("**/*", "*"))
        for path in (".DS_Store", "nested/Thumbs.db", "x/._photo.jpg", "db.lock", "a/db-wal", "db-journal"):
            with self.subTest(path=path):
                self.assertFalse(auto_upload_path_is_included(path, policy))
                self.assertIn(path, preview_glob_rules(paths=[path], include_globs=["*"])["excluded"])
        for path in ("photos/DS_Store.jpg", "thumbs.db", "photo.jpg", "catalog.lrcat"):
            self.assertTrue(auto_upload_path_is_included(path, policy))

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
    def test_observation_restarts_window_even_for_preserved_old_mtime(self) -> None:
        old = int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp() * 1e9)
        for seconds, expected in ((299, False), (300, True)):
            self.assertEqual(file_is_stable(
                mtime_ns=old, now=datetime(2026, 7, 1, 12, 5, seconds - 299, tzinfo=timezone.utc),
                stability_seconds=300, content_observed_at="2026-07-01T12:00:01+00:00",
            ), expected)

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

    def test_local_modification_after_verification_restarts_the_window(self) -> None:
        """A recovered or edited Local Copy must not be removed on the next scan."""
        from app.services.operation_policies import local_cleanup_is_due

        verified_at = "2026-01-01T12:00:00+00:00"
        recovered_mtime_ns = int(
            datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc).timestamp() * 1e9
        )
        now = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
        self.assertTrue(
            local_cleanup_is_due(verified_at=verified_at, retention_days=30, now=now)
        )
        self.assertFalse(
            local_cleanup_is_due(
                verified_at=verified_at,
                retention_days=30,
                now=now,
                local_mtime_ns=recovered_mtime_ns,
            )
        )
        self.assertTrue(
            local_cleanup_is_due(
                verified_at=verified_at,
                retention_days=30,
                now=datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc),
                local_mtime_ns=recovered_mtime_ns,
            )
        )


    def test_recovery_restarts_the_window_even_with_an_old_mtime(self) -> None:
        """#409: a recovered file may keep its original mtime from years ago."""
        from app.services.operation_policies import local_cleanup_is_due

        verified_at = "2026-01-01T12:00:00+00:00"
        original_mtime_ns = int(
            datetime(2019, 5, 1, 12, 0, tzinfo=timezone.utc).timestamp() * 1e9
        )
        recovered_at = "2026-07-30T12:00:00+00:00"
        self.assertFalse(
            local_cleanup_is_due(
                verified_at=verified_at,
                retention_days=30,
                now=datetime(2026, 8, 29, 11, 59, 59, tzinfo=timezone.utc),
                local_mtime_ns=original_mtime_ns,
                recovered_at=recovered_at,
            )
        )
        self.assertTrue(
            local_cleanup_is_due(
                verified_at=verified_at,
                retention_days=30,
                now=datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc),
                local_mtime_ns=original_mtime_ns,
                recovered_at=recovered_at,
            )
        )


class OperationPolicyPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "policies.db"
        result = run_alembic(self.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        with PostgresConnection(str(self.path)) as connection:
            seed_vault(connection, vault_id=1)

    def test_set_policy_round_trips(self) -> None:
        desired = OperationPolicy(
            auto_upload=True,
            local_release_after_days=90,
            stability_seconds=600,
            include_globs=("**/*.pdf",),
            exclude_globs=("tmp/**",),
        )
        with PostgresConnection(str(self.path)) as connection:
            stored = set_policy(connection, 1, desired)
            loaded = get_policy(connection, 1)
        self.assertEqual(stored, desired)
        self.assertEqual(loaded, desired)

    def test_concurrent_decommission_transition_wins_before_policy_write(self) -> None:
        transition_started = threading.Event()
        policy_finished = threading.Event()
        errors: list[BaseException] = []

        def update_policy() -> None:
            transition_started.wait(2)
            try:
                with PostgresConnection(str(self.path)) as connection:
                    set_policy(connection, 1, OperationPolicy(auto_upload=True))
            except BaseException as exc:
                errors.append(exc)
            finally:
                policy_finished.set()

        thread = threading.Thread(target=update_policy)
        thread.start()
        with PostgresConnection(str(self.path)) as connection:
            connection.execute(
                "UPDATE vaults SET lifecycle='stopping' WHERE id=1"
            )
            transition_started.set()
            self.assertFalse(policy_finished.wait(0.1))
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], LookupError)
        self.assertEqual(str(errors[0]), "vault_quiesced")
        with PostgresConnection(str(self.path)) as connection:
            count = connection.execute(
                "SELECT COUNT(*) AS total FROM vault_operation_policies WHERE vault_id=1"
            ).fetchone()["total"]
        self.assertEqual(count, 0)


class AutoUploadEligibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        # Upload remediation (#592) is outside this seam.
        remediation = patch(
            "app.services.upload_remediation.intercept_upload",
            return_value=(False, None),
        )
        remediation.start()
        self.addCleanup(remediation.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.path = self.root / "catalog.db"
        result = run_alembic(self.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        with PostgresConnection(str(self.path)) as connection:
            insert_owner(connection)
            seed_vault(connection, source_root=str(self.source))

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

        with PostgresConnection(str(self.path)) as connection:
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
                    observed_at="2026-07-01T11:00:00+00:00",
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
                now=now,
            )
            paths = [
                row["path"]
                for row in connection.execute(
                    "SELECT f.path FROM jobs j JOIN files f ON f.id=j.file_id WHERE j.action='upload' ORDER BY f.path"
                ).fetchall()
            ]
        self.assertEqual(queued, 1)
        self.assertEqual(paths, ["ready.txt"])

    def test_explicit_pause_queues_nothing(self) -> None:
        from app.services.operation_policies import queue_auto_uploads

        target = self.source / "ready.txt"
        target.write_text("stable")
        old = datetime(2026, 7, 1, 11, 0, tzinfo=timezone.utc).timestamp()
        import os

        os.utime(target, (old, old))
        now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
        with PostgresConnection(str(self.path)) as connection:
            ArchiveCatalog(connection).observe_local_copy(
                vault_id=2,
                path="ready.txt",
                file_type="regular",
                size=target.stat().st_size,
                mtime_ns=target.stat().st_mtime_ns,
                observed_at="2026-07-01T11:00:00+00:00",
            )
            set_policy(connection, 2, OperationPolicy(auto_upload=False))
            queued = queue_auto_uploads(
                connection,
                vault_id=2,
                source_root=str(self.source),
                now=now,
            )
            total = connection.execute("SELECT COUNT(*) AS total FROM jobs").fetchone()[
                "total"
            ]
        self.assertEqual(queued, 0)
        self.assertEqual(total, 0)

    def test_auto_upload_queues_changed_content_already_on_s3(self) -> None:
        from app.services.operation_policies import queue_auto_uploads
        from app.storage import now_iso
        import os

        target = self.source / "ready.txt"
        original = b"version-a"
        changed = b"version-b-is-longer"
        target.write_bytes(original)
        old = datetime(2026, 7, 1, 11, 0, tzinfo=timezone.utc).timestamp()
        os.utime(target, (old, old))
        now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
        digest = hashlib.sha256(original).hexdigest()
        with PostgresConnection(str(self.path)) as connection:
            catalog = ArchiveCatalog(connection)
            catalog.observe_local_copy(
                vault_id=2,
                path="ready.txt",
                file_type="regular",
                size=len(original),
                mtime_ns=target.stat().st_mtime_ns,
                observed_at="2026-07-01T11:00:00+00:00",
            )
            version_id = _protect(catalog, "ready.txt", digest, "s3-a", "2026-07-01T11:00:00+00:00")
            set_policy(
                connection,
                2,
                OperationPolicy(auto_upload=True, stability_seconds=300),
            )
            skipped = queue_auto_uploads(
                connection,
                vault_id=2,
                source_root=str(self.source),
                now=now,
            )
            self.assertEqual(skipped, 0)

            target.write_bytes(changed)
            os.utime(target, (old, old))
            catalog.observe_local_copy(
                vault_id=2,
                path="ready.txt",
                file_type="regular",
                size=len(changed),
                mtime_ns=target.stat().st_mtime_ns,
                observed_at="2026-07-01T11:30:00+00:00",
            )
            queued = queue_auto_uploads(
                connection,
                vault_id=2,
                source_root=str(self.source),
                now=now,
            )
            jobs = connection.execute(
                "SELECT f.path, j.action, j.archive_version_id FROM jobs j JOIN files f ON f.id=j.file_id"
            ).fetchall()
        self.assertEqual(queued, 1)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["path"], "ready.txt")
        self.assertEqual(jobs[0]["action"], "upload")
        self.assertIsNone(jobs[0]["archive_version_id"])

    def test_missing_cloud_copy_is_automatically_reuploaded(self) -> None:
        from app.services.operation_policies import queue_auto_uploads
        import os

        target = self.source / "ready.txt"
        payload = b"protected-bytes"
        target.write_bytes(payload)
        old = datetime(2026, 7, 1, 11, 0, tzinfo=timezone.utc).timestamp()
        os.utime(target, (old, old))
        now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
        digest = hashlib.sha256(payload).hexdigest()
        with PostgresConnection(str(self.path)) as connection:
            catalog = ArchiveCatalog(connection)
            catalog.observe_local_copy(
                vault_id=2,
                path="ready.txt",
                file_type="regular",
                size=len(payload),
                mtime_ns=target.stat().st_mtime_ns,
                observed_at="2026-07-01T11:00:00+00:00",
            )
            version_id = _protect(catalog, "ready.txt", digest, "s3-a", "2026-07-01T11:00:00+00:00")
            set_policy(
                connection,
                2,
                OperationPolicy(auto_upload=True, stability_seconds=300),
            )
            self.assertEqual(
                queue_auto_uploads(
                    connection,
                    vault_id=2,
                    source_root=str(self.source),
                    now=now,
                ),
                0,
            )
            connection.execute(
                "UPDATE archive_versions SET availability='missing' WHERE id=%s",
                (version_id,),
            )
            queued = queue_auto_uploads(
                connection,
                vault_id=2,
                source_root=str(self.source),
                now=now,
            )
            again = queue_auto_uploads(
                connection,
                vault_id=2,
                source_root=str(self.source),
                now=now,
            )
            jobs = connection.execute(
                "SELECT f.path, j.action, j.archive_version_id, j.status FROM jobs j JOIN files f ON f.id=j.file_id"
            ).fetchall()
            kept = connection.execute(
                "SELECT availability FROM archive_versions WHERE id=%s",
                (version_id,),
            ).fetchone()
        self.assertEqual(queued, 1)
        self.assertEqual(again, 0)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["path"], "ready.txt")
        self.assertEqual(jobs[0]["action"], "upload")
        self.assertIsNone(jobs[0]["archive_version_id"])
        self.assertEqual(kept["availability"], "missing")

    def test_auto_cleanup_queues_only_matching_versions_past_retention(self) -> None:
        from app.services.operation_policies import queue_auto_local_cleanups

        now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
        due_version_id = None
        with PostgresConnection(str(self.path)) as connection:
            catalog = ArchiveCatalog(connection)
            for name, verified_at in (
                ("due.txt", "2026-07-01T12:00:00+00:00"),
                ("not-due.txt", "2026-07-02T12:00:00+00:00"),
                # Identical bytes reimported with old preserved mtime and an
                # already verified cloud snapshot still get a full new period.
                ("imported-old.jpg", "2015-01-01T12:00:00+00:00"),
                # Verified long ago, but written back locally yesterday: a
                # recovered Local Copy starts a fresh retention window.
                ("recovered.txt", "2026-01-01T12:00:00+00:00"),
            ):
                target = self.source / name
                content = name.encode()
                target.write_bytes(content)
                local_mtime = datetime.fromisoformat(
                    "2026-07-30T12:00:00+00:00"
                    if name == "recovered.txt"
                    else verified_at
                ).timestamp()
                os.utime(target, (local_mtime, local_mtime))
                digest = hashlib.sha256(content).hexdigest()
                catalog.observe_local_copy(
                    vault_id=2,
                    path=name,
                    file_type="regular",
                    size=len(content),
                    mtime_ns=target.stat().st_mtime_ns,
                    observed_at=now.isoformat() if name == "imported-old.jpg" else verified_at,
                )
                version_id = _protect(catalog, name, digest, f"s3-{name}", verified_at)
                if name == "due.txt":
                    due_version_id = version_id
            set_policy(
                connection,
                2,
                OperationPolicy(
                    local_release_after_days=90,
                    include_globs=("**/*.jpg",),
                    exclude_globs=("**/*.txt",),
                ),
            )

            queued = queue_auto_local_cleanups(
                connection,
                vault_id=2,
                now=now,
            )
            jobs = connection.execute(
                "SELECT j.id, f.path, j.action, j.archive_version_id::text AS archive_version_id, j.origin "
                "FROM jobs j JOIN files f ON f.id=j.file_id ORDER BY f.path"
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
        self.assertEqual(notifications, [])  # Cleanup activity belongs only in the summary.


if __name__ == "__main__":
    unittest.main()
