"""Adopt the current multi-user schema.

Revision ID: 0001_current_schema
Revises:
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0001_current_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


EXPECTED_COLUMNS = {
    "users": {
        "id", "username", "display_name", "password_hash", "is_admin", "active",
        "session_version", "created_at",
    },
    "vaults": {
        "id", "slug", "name", "source_root", "s3_bucket", "s3_prefix",
        "rclone_remote", "enabled", "created_at",
    },
    "vault_members": {"vault_id", "user_id", "role"},
    "files": {
        "vault_id", "path", "local_exists", "local_size", "local_mtime",
        "cloud_exists", "cloud_size", "cloud_key", "storage_class", "etag",
        "restore_state", "restore_expiry", "last_local_scan",
        "last_restore_scan", "last_cloud_scan", "updated_at",
    },
    "jobs": {
        "id", "vault_id", "path", "action", "status", "message",
        "requested_by", "requested_at", "updated_at", "group_id", "group_path",
        "total_bytes", "transferred_bytes",
    },
}

EXPECTED_PRIMARY_KEYS = {
    "users": ("id",),
    "vaults": ("id",),
    "vault_members": ("vault_id", "user_id"),
    "files": ("vault_id", "path"),
    "jobs": ("id",),
}

EXPECTED_NULLABLE = {
    "users": set(),
    "vaults": set(),
    "vault_members": set(),
    "files": {
        "local_size", "local_mtime", "cloud_size", "cloud_key", "storage_class",
        "etag", "restore_state", "restore_expiry", "last_local_scan",
        "last_restore_scan", "last_cloud_scan",
    },
    "jobs": {"message", "requested_by", "group_id", "group_path"},
}

EXPECTED_FOREIGN_KEYS = {
    "users": set(),
    "vaults": set(),
    "vault_members": {
        (("vault_id",), "vaults", ("id",), "CASCADE"),
        (("user_id",), "users", ("id",), "CASCADE"),
    },
    "files": {(("vault_id",), "vaults", ("id",), "CASCADE")},
    "jobs": {
        (("requested_by",), "users", ("id",), "SET NULL"),
        (("vault_id", "path"), "files", ("vault_id", "path"), "CASCADE"),
    },
}

EXPECTED_TYPES = {
    "users": {
        "id": "integer", "username": "text", "display_name": "text",
        "password_hash": "text", "is_admin": "boolean", "active": "boolean",
        "session_version": "integer", "created_at": "timestamp",
    },
    "vaults": {
        "id": "integer", "slug": "text", "name": "text", "source_root": "text",
        "s3_bucket": "text", "s3_prefix": "text", "rclone_remote": "text",
        "enabled": "boolean", "created_at": "timestamp",
    },
    "vault_members": {
        "vault_id": "integer", "user_id": "integer", "role": "text",
    },
    "files": {
        "vault_id": "integer", "path": "text", "local_exists": "integer",
        "local_size": "integer", "local_mtime": "float",
        "cloud_exists": "integer", "cloud_size": "integer", "cloud_key": "text",
        "storage_class": "text", "etag": "text", "restore_state": "text",
        "restore_expiry": "text", "last_local_scan": "text",
        "last_restore_scan": "text", "last_cloud_scan": "text",
        "updated_at": "text",
    },
    "jobs": {
        "id": "integer", "vault_id": "integer", "path": "text",
        "action": "text", "status": "text", "message": "text",
        "requested_by": "integer", "requested_at": "text",
        "updated_at": "text", "group_id": "text", "group_path": "text",
        "total_bytes": "integer", "transferred_bytes": "integer",
    },
}

REQUIRED_INDEX_FRAGMENTS = {
    "users_username_lower_uq": ("uniqueindex", "lower(username)"),
    "files_vault_state_idx": (
        "index", "(vault_id,local_exists,cloud_exists)",
    ),
    "jobs_vault_active_idx": ("index", "(vault_id,status,action)"),
    "jobs_one_active_action_uq": (
        "uniqueindex",
        "(vault_id,path,action)",
    ),
}


def _compact(value: str) -> str:
    return "".join(value.lower().replace('"', "").split())


def _type_category(column_type: sa.types.TypeEngine) -> str:
    if isinstance(column_type, sa.Boolean):
        return "boolean"
    if isinstance(column_type, sa.Integer):
        return "integer"
    if isinstance(column_type, sa.Float):
        return "float"
    if isinstance(column_type, sa.DateTime):
        return "timestamp"
    if isinstance(column_type, sa.String):
        return "text"
    return column_type.__class__.__name__.lower()


def _index_definitions() -> dict[str, str]:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        rows = bind.execute(
            sa.text(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type='index' AND sql IS NOT NULL"
            )
        ).mappings()
        return {row["name"]: _compact(row["sql"]) for row in rows}
    rows = bind.execute(
        sa.text(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname=current_schema()"
        )
    ).mappings()
    return {row["indexname"]: _compact(row["indexdef"]) for row in rows}


def _validate_table(inspector: sa.Inspector, table: str) -> None:
    columns = {column["name"]: column for column in inspector.get_columns(table)}
    expected = EXPECTED_COLUMNS[table]
    actual = set(columns)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise RuntimeError(
            f"Unsupported pre-Alembic schema for {table}; "
            f"missing={missing}, extra={extra}"
        )

    primary_key = tuple(
        inspector.get_pk_constraint(table).get("constrained_columns") or ()
    )
    if primary_key != EXPECTED_PRIMARY_KEYS[table]:
        raise RuntimeError(
            f"Unsupported pre-Alembic schema for {table}; invalid primary key"
        )

    primary_columns = set(primary_key)
    nullable = {
        name
        for name, column in columns.items()
        if name not in primary_columns and column["nullable"]
    }
    if nullable != EXPECTED_NULLABLE[table]:
        raise RuntimeError(
            f"Unsupported pre-Alembic schema for {table}; invalid nullability"
        )

    for name, expected_type in EXPECTED_TYPES[table].items():
        actual_type = _type_category(columns[name]["type"])
        if (
            expected_type == "timestamp"
            and inspector.bind.dialect.name == "sqlite"
            and actual_type == "text"
        ):
            continue
        if actual_type != expected_type:
            raise RuntimeError(
                f"Unsupported pre-Alembic schema for {table}.{name}; "
                f"expected {expected_type}, found {actual_type}"
            )

    foreign_keys = {
        (
            tuple(key["constrained_columns"]),
            key["referred_table"],
            tuple(key["referred_columns"]),
            str((key.get("options") or {}).get("ondelete", "")).upper(),
        )
        for key in inspector.get_foreign_keys(table)
    }
    if foreign_keys != EXPECTED_FOREIGN_KEYS[table]:
        raise RuntimeError(
            f"Unsupported pre-Alembic schema for {table}; invalid foreign keys"
        )


def _validate_constraints(inspector: sa.Inspector) -> None:
    vault_uniques = {
        tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints("vaults")
    }
    if ("slug",) not in vault_uniques:
        raise RuntimeError(
            "Unsupported pre-Alembic schema for vaults; missing slug uniqueness"
        )

    expected_checks = {
        "vault_members": ("role", "owner", "viewer"),
        "jobs": ("action", "upload", "recover", "free-space"),
    }
    for table, tokens in expected_checks.items():
        checks = [
            _compact(constraint["sqltext"])
            for constraint in inspector.get_check_constraints(table)
        ]
        if not any(
            all(token in check for token in tokens)
            and ("in(" in check or "any(" in check)
            for check in checks
        ):
            raise RuntimeError(
                f"Unsupported pre-Alembic schema for {table}; "
                "missing required check constraint"
            )

    indexes = _index_definitions()
    for name, fragments in REQUIRED_INDEX_FRAGMENTS.items():
        definition = indexes.get(name)
        if definition is None or any(fragment not in definition for fragment in fragments):
            raise RuntimeError(
                f"Unsupported pre-Alembic schema; missing required index {name}"
            )
    active_index = indexes["jobs_one_active_action_uq"]
    if not (
        "where" in active_index
        and "status" in active_index
        and all(
            status in active_index
            for status in ("completed", "failed", "cancelled")
        )
        and ("notin" in active_index or "<>all" in active_index)
    ):
        raise RuntimeError(
            "Unsupported pre-Alembic schema; invalid active Job index predicate"
        )


def _adopt_existing_schema() -> bool:
    inspector = sa.inspect(op.get_bind())
    present = set(inspector.get_table_names()) - {"alembic_version"}
    app_tables = present.intersection(EXPECTED_COLUMNS)
    if not app_tables:
        return False
    if app_tables != set(EXPECTED_COLUMNS):
        missing = sorted(set(EXPECTED_COLUMNS) - app_tables)
        raise RuntimeError(
            "Unsupported pre-Alembic database schema; missing current-release "
            f"tables: {', '.join(missing)}"
        )
    for table in EXPECTED_COLUMNS:
        _validate_table(inspector, table)
    _validate_constraints(inspector)
    return True


def upgrade() -> None:
    if _adopt_existing_schema():
        return

    identifier = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    op.create_table(
        "users",
        sa.Column("id", identifier, primary_key=True, autoincrement=True),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("session_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "users_username_lower_uq",
        "users",
        [sa.text("lower(username)")],
        unique=True,
    )
    op.create_table(
        "vaults",
        sa.Column("id", identifier, primary_key=True, autoincrement=True),
        sa.Column("slug", sa.Text(), nullable=False, unique=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("source_root", sa.Text(), nullable=False),
        sa.Column("s3_bucket", sa.Text(), nullable=False),
        sa.Column("s3_prefix", sa.Text(), nullable=False),
        sa.Column("rclone_remote", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_table(
        "vault_members",
        sa.Column(
            "vault_id", identifier, sa.ForeignKey("vaults.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id", identifier, sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("role", sa.Text(), nullable=False, server_default="owner"),
        sa.CheckConstraint("role IN ('owner', 'viewer')", name="vault_members_role_ck"),
    )
    op.create_table(
        "files",
        sa.Column(
            "vault_id", identifier, sa.ForeignKey("vaults.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("path", sa.Text(), primary_key=True),
        sa.Column("local_exists", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("local_size", sa.BigInteger()),
        sa.Column("local_mtime", sa.Float()),
        sa.Column("cloud_exists", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cloud_size", sa.BigInteger()),
        sa.Column("cloud_key", sa.Text()),
        sa.Column("storage_class", sa.Text()),
        sa.Column("etag", sa.Text()),
        sa.Column("restore_state", sa.Text()),
        sa.Column("restore_expiry", sa.Text()),
        sa.Column("last_local_scan", sa.Text()),
        sa.Column("last_restore_scan", sa.Text()),
        sa.Column("last_cloud_scan", sa.Text()),
        sa.Column("updated_at", sa.Text(), nullable=False),
    )
    op.create_index(
        "files_vault_state_idx",
        "files",
        ["vault_id", "local_exists", "cloud_exists"],
    )
    op.create_table(
        "jobs",
        sa.Column("id", identifier, primary_key=True, autoincrement=True),
        sa.Column("vault_id", identifier, nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("message", sa.Text()),
        sa.Column(
            "requested_by", identifier,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("requested_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.Column("group_id", sa.Text()),
        sa.Column("group_path", sa.Text()),
        sa.Column("total_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "transferred_bytes", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.CheckConstraint(
            "action IN ('upload', 'recover', 'free-space')",
            name="jobs_action_check",
        ),
        sa.ForeignKeyConstraint(
            ["vault_id", "path"],
            ["files.vault_id", "files.path"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "jobs_vault_active_idx", "jobs", ["vault_id", "status", "action"]
    )
    active = sa.text("status NOT IN ('completed', 'failed', 'cancelled')")
    op.create_index(
        "jobs_one_active_action_uq",
        "jobs",
        ["vault_id", "path", "action"],
        unique=True,
        sqlite_where=active,
        postgresql_where=active,
    )


def downgrade() -> None:
    op.drop_table("jobs")
    op.drop_table("files")
    op.drop_table("vault_members")
    op.drop_table("vaults")
    op.drop_table("users")
