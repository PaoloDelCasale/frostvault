"""Add the allow-listed effective system settings store.

Revision ID: 0024_system_settings
Revises: 0023_storage_class_change
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0024_system_settings"
down_revision: str | None = "0023_storage_class_change"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MANAGED_KEYS = (
    "reauth_window_seconds",
    "scan_interval",
    "audit_interval",
    "filesystem_watch_debounce_ms",
    "filesystem_watch_poll_ms",
    "queue_poll_interval",
    "operation_concurrency",
    "cloud_purge_delay_seconds",
    "allow_local_delete",
    "bandwidth_limit_kibps",
    "s3_download_max_concurrency",
    "s3_download_multipart_threshold_mib",
    "s3_download_multipart_chunksize_mib",
    "rclone_multi_thread_streams",
    "rclone_multi_thread_cutoff_mib",
    "job_progress_min_interval_ms",
    "restore_poll_interval",
    "restore_days",
    "restore_tier",
    "restore_high_impact_gib",
    "restore_high_impact_eur",
    "restore_approval_hold_seconds",
    "invite_ttl_seconds",
    "metadata_backup_retention",
    "metadata_backup_interval_seconds",
    "metadata_backup_s3_prefix",
    "metadata_backup_verify_interval_seconds",
)
_INTEGER_KEYS = tuple(
    key
    for key in _MANAGED_KEYS
    if key
    not in {
        "allow_local_delete",
        "bandwidth_limit_kibps",
        "restore_tier",
        "restore_high_impact_gib",
        "restore_high_impact_eur",
        "metadata_backup_s3_prefix",
    }
)
_NUMBER_KEYS = ("restore_high_impact_gib", "restore_high_impact_eur")


def _quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    identifier = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        json_type = "json_typeof(value)"
        integer_type = "number"
        string_type = "string"
        bool_types = "'boolean'"
        # PostgreSQL JSON has one number type; modulo distinguishes integers.
        integer_check = (
            f"{json_type} = 'number' AND "
            "((value #>> '{}')::numeric % 1) = 0"
        )
    else:
        json_type = "json_type(value)"
        integer_type = "integer"
        string_type = "text"
        bool_types = "'true', 'false'"
        integer_check = f"{json_type} = 'integer'"
    type_check = (
        f"(key IN ({_quoted(_INTEGER_KEYS)}) AND {integer_check}) OR "
        f"(key IN ({_quoted(_NUMBER_KEYS)}) AND {json_type} IN "
        f"('{integer_type}', 'real')) OR "
        f"(key IN ('restore_tier', 'metadata_backup_s3_prefix') "
        f"AND {json_type} = '{string_type}') OR "
        f"(key = 'allow_local_delete' AND {json_type} IN ({bool_types})) OR "
        f"(key = 'bandwidth_limit_kibps' AND "
        f"(({integer_check}) OR {json_type} = 'null'))"
    )
    op.create_table(
        "system_settings",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column(
            "updated_by",
            identifier,
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            f"key IN ({_quoted(_MANAGED_KEYS)})",
            name="system_settings_key_ck",
        ),
        sa.CheckConstraint(type_check, name="system_settings_value_type_ck"),
    )


def downgrade() -> None:
    op.drop_table("system_settings")
