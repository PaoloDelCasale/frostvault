"""Index the per-file lineage lookups of a small upload.

Every upload asked four questions by a column that had no usable index, so each
one read the whole table (or a whole unrelated index) and the cost of one file
grew with the size of the catalog:

- every path a Vault File ever had (``file_paths.vault_file_id``; the existing
  indexes only cover the current path);
- the upload intents and Jobs of an Archive Version;
- the cleanup inventories that kept an Archive Version.

Directory rollups and file pages also filter current paths by prefix
(``path LIKE 'dir/%'``). The catalog database uses a linguistic collation, where
a plain B-tree cannot serve a prefix match, so every rollup refresh read all
current paths. ``file_paths_vault_current_path_idx`` was an exact duplicate of
the unique ``file_paths_one_current_path_uq`` index; it is replaced by
``(path text_pattern_ops, vault_id)``, which serves the prefix match. The path
leads because the rollup filters the Vault on ``vault_files`` and reaches
``file_paths`` only by prefix. Equality and ordered path reads keep using the
unique index.
"""
from alembic import op
import sqlalchemy as sa

revision = "0072_upload_lineage_indexes"
down_revision = "0071_bulk_scheduler_merge"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_index("file_paths_vault_current_path_idx", table_name="file_paths")
    op.create_index(
        "file_paths_current_path_prefix_idx", "file_paths", ["path", "vault_id"],
        postgresql_where=sa.text("valid_to IS NULL"),
        postgresql_ops={"path": "text_pattern_ops"},
    )
    op.create_index("file_paths_vault_file_idx", "file_paths", ["vault_file_id"])
    op.create_index(
        "upload_intents_archive_version_idx", "upload_intents", ["archive_version_id"],
        postgresql_where=sa.text("archive_version_id IS NOT NULL"),
    )
    op.create_index(
        "jobs_archive_version_idx", "jobs", ["archive_version_id"],
        postgresql_where=sa.text("archive_version_id IS NOT NULL"),
    )
    op.create_index(
        "snapshot_cleanup_inventories_keeper_idx", "snapshot_cleanup_inventories", ["keeper_id"],
        postgresql_where=sa.text("keeper_id IS NOT NULL"),
    )


def downgrade():
    op.drop_index("snapshot_cleanup_inventories_keeper_idx", table_name="snapshot_cleanup_inventories")
    op.drop_index("jobs_archive_version_idx", table_name="jobs")
    op.drop_index("upload_intents_archive_version_idx", table_name="upload_intents")
    op.drop_index("file_paths_vault_file_idx", table_name="file_paths")
    op.drop_index("file_paths_current_path_prefix_idx", table_name="file_paths")
    op.create_index(
        "file_paths_vault_current_path_idx", "file_paths", ["vault_id", "path"],
        postgresql_where=sa.text("valid_to IS NULL"),
    )
