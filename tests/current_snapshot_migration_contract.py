"""Populated SQLite/PostgreSQL preservation contract for #418."""
from tests.notification_migration_contract import preserved_state, seed_legacy_notifications
from app.catalog import ArchiveCatalog


def _state(connection):
    state = preserved_state(connection)
    for row in state['vaults']:
        row.pop('cloud_history_policy', None)
    return {table: sorted(rows, key=lambda row: str(row.get('id', row.get('vault_file_id', ''))))
            for table, rows in state.items()}


def assert_current_snapshot_migration(test, run, connect):
    result = run('0045_drop_quotas_windows')
    test.assertEqual(result.returncode, 0, result.stderr)
    with connect() as connection:
        seed_legacy_notifications(connection)
    result = run('0049_essential_file_actions')
    test.assertEqual(result.returncode, 0, result.stderr)
    with connect() as connection:
        catalog = ArchiveCatalog(connection)
        # Do not fabricate verification when retiring a legacy policy. Both
        # unverified and mismatched cloud content must survive schema changes.
        for provider, integrity in (('legacy-unverified', 'unverified'), ('legacy-mismatch', 'mismatch')):
            version = catalog.record_archive_version(
                vault_id=2, path='photo.jpg', object_key='photos/photo.jpg',
                provider_version_id=provider, size=10, storage_class='STANDARD',
                etag=provider, uploaded_at='2026-11-03', observed_at='2026-11-03',
                scan_id='2026-11-03',
            )
            connection.execute('UPDATE archive_versions SET integrity=%s WHERE id=%s', (integrity, version))
        connection.execute("INSERT INTO vaults(id, slug, name, source_root, s3_bucket, s3_prefix, rclone_remote, cloud_history_policy) VALUES (9, 'snapshot', 'Snapshot', '/source2', 'bucket', 'second', 'remote', 'current_snapshot')")
        before = _state(connection)
    for target, command in (('0050_current_snapshot_only', 'upgrade'), ('0049_essential_file_actions', 'downgrade'), ('0050_current_snapshot_only', 'upgrade')):
        result = run(target, command)
        test.assertEqual(result.returncode, 0, result.stderr)
        with connect() as connection:
            test.assertEqual(_state(connection), before)
            vaults = connection.execute('SELECT * FROM vaults ORDER BY id').fetchall()
            if command == 'upgrade':
                test.assertNotIn('cloud_history_policy', vaults[0])
            else:
                test.assertEqual([row['cloud_history_policy'] for row in vaults], ['current_snapshot', 'current_snapshot'])
