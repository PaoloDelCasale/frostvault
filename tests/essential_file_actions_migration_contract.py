"""Shared SQLite/PostgreSQL preservation contract for #417."""
from __future__ import annotations

from app.catalog import ArchiveCatalog
from tests.notification_migration_contract import seed_legacy_notifications, preserved_state


def assert_file_actions_migration(test, run, connect):
    result = run("0045_drop_quotas_windows")
    test.assertEqual(result.returncode, 0, result.stderr)
    with connect() as connection:
        seed_legacy_notifications(connection)
    result = run("0048_essential_administration")
    test.assertEqual(result.returncode, 0, result.stderr)
    with connect() as connection:
        file_id = connection.execute("SELECT id FROM vault_files").fetchone()["id"]
        version_id = connection.execute("SELECT id FROM archive_versions").fetchone()["id"]
        connection.execute("INSERT INTO delete_markers(id, vault_id, vault_file_id, object_key, provider_version_id, created_at, discovered_at) VALUES ('11111111-1111-1111-1111-111111111111', 2, %s, 'photos/photo.jpg', 'legacy-marker', '2026-11-03', '2026-11-03')", (file_id,))
        second_file = ArchiveCatalog(connection).observe_local_copy(
            vault_id=2, path="second.jpg", file_type="regular", size=10,
            mtime_ns=1, observed_at="2026-11-03",
        )
        for job_id, status in ((10, "queued"), (11, "cleaning"), (12, "completed")):
            connection.execute("INSERT INTO jobs(id, vault_id, vault_file_id, archive_version_id, path, action, status, requested_at, updated_at, claim_token, claimed_at, claim_expires_at) VALUES (%s, 2, %s, %s, 'photo.jpg', 'cloud-archive', %s, '2026-11-03', '2026-11-03', 'legacy-claim', '2026-11-03', '2026-11-04')", (job_id, second_file if job_id == 11 else file_id, version_id, status))
        before = preserved_state(connection)
        markers = connection.execute("SELECT * FROM delete_markers").fetchall()
        for row in before["jobs"]:
            if row["id"] in (10, 11):
                row.update(status="cancelled", message="Cloud hiding was removed; cloud content retained", message_key=None, message_params=None, claim_token=None, claimed_at=None, claim_expires_at=None)
    for target, command in (("0049_essential_file_actions", "upgrade"), ("0048_essential_administration", "downgrade"), ("0049_essential_file_actions", "upgrade")):
        result = run(target, command)
        test.assertEqual(result.returncode, 0, result.stderr)
        with connect() as connection:
            test.assertEqual(preserved_state(connection), before)
            test.assertEqual(connection.execute("SELECT * FROM delete_markers").fetchall(), markers)
