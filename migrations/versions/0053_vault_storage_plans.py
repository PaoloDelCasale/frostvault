"""One current-version storage plan per Vault, with no cloud I/O.

Identical folder policies collapse into the default. Heterogeneous assignments
or pins on a transitioning Vault are ambiguous and stop before DDL. Retired
expiration/noncurrent actions are discarded, never translated into deletions.
"""
import importlib
import json

from alembic import op
import sqlalchemy as sa

revision = "0053_vault_storage_plans"
down_revision = "0052_s3_sdk"
branch_labels = None
depends_on = None


def _legacy_plan(value):
    from app.services.lifecycle_profiles import profile_from_json, profile_to_json

    payload = json.loads(value) if value else {}
    if not isinstance(payload, dict) or set(payload) - {
        "transitions", "noncurrent_transitions", "expiration_days", "noncurrent_expiration_days",
    }:
        raise ValueError("Unrecognized legacy storage policy")
    # #418/#421 explicitly retire these actions. Only current transitions map.
    return profile_to_json(profile_from_json(json.dumps({"transitions": payload.get("transitions", [])})))


def upgrade():
    connection = op.get_bind()
    plans = []
    namespaces = {}
    for vault in connection.execute(sa.text(
        "SELECT id, s3_bucket, s3_prefix, default_lifecycle_policy_id FROM vaults"
    )).mappings():
        def policy_plan(policy_id):
            if not policy_id:
                return _legacy_plan(None)
            row = connection.execute(sa.text("SELECT vault_id, profile_json FROM lifecycle_policies WHERE id=:id"), {"id": policy_id}).mappings().first()
            if row is None or row["vault_id"] != vault["id"]:
                raise ValueError("Missing or cross-Vault legacy policy")
            return _legacy_plan(row["profile_json"])
        try:
            plan = policy_plan(vault["default_lifecycle_policy_id"])
            overrides = connection.execute(sa.text("SELECT policy_id FROM folder_policy_overrides WHERE vault_id=:id"), {"id": vault["id"]}).scalars()
            if any(policy_plan(policy_id) != plan for policy_id in overrides):
                raise ValueError("Folder policies disagree with the Vault default")
            pinned = connection.execute(sa.text("SELECT 1 FROM lifecycle_pins WHERE vault_id=:id LIMIT 1"), {"id": vault["id"]}).first()
            if pinned and json.loads(plan)["transitions"]:
                raise ValueError("Pinned content disagrees with a transitioning Vault plan")
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError(f"Vault {vault['id']} has an ambiguous legacy storage plan: {exc}. Resolve its assignments with the previous release before upgrading; no cloud content was changed.") from exc
        plans.append((vault["id"], plan))
        namespaces.setdefault(vault["s3_bucket"], []).append(
            (vault["s3_prefix"], bool(json.loads(plan)["transitions"]))
        )

    from app.services.s3_lifecycle_rules import validate_vault_prefixes
    for entries in namespaces.values():
        if any(transitioning for _, transitioning in entries):
            try:
                validate_vault_prefixes([prefix for prefix, _ in entries])
            except ValueError as exc:
                raise RuntimeError(
                    "Ambiguous legacy Vault namespaces: resolve empty/overlapping "
                    "prefixes with the previous release before upgrading; "
                    "no cloud content was changed."
                ) from exc

    # All legacy Vaults are validated before the first schema/data modification.
    with op.batch_alter_table("vaults") as batch:
        batch.add_column(sa.Column("storage_plan_json", sa.Text(), nullable=False, server_default='{"transitions": []}'))
        batch.drop_constraint("vaults_default_lifecycle_policy_fk", type_="foreignkey")
        batch.drop_column("default_lifecycle_policy_id")
    for vault_id, plan in plans:
        connection.execute(sa.text("UPDATE vaults SET storage_plan_json=:plan WHERE id=:id"), {"plan": plan, "id": vault_id})
    op.drop_table("folder_policy_overrides")
    op.drop_table("lifecycle_pins")
    op.drop_table("lifecycle_policies")
    with op.batch_alter_table("archive_versions") as batch:
        batch.drop_column("desired_policy_id")
        batch.drop_column("applied_policy_id")
    with op.batch_alter_table("directory_aggregates") as batch:
        batch.drop_constraint("directory_aggregates_pinned_ck", type_="check")
        batch.drop_column("pinned_count")
        batch.drop_column("action_storage_class")


def downgrade():
    connection = op.get_bind()
    plans = list(connection.execute(sa.text("SELECT id, storage_plan_json FROM vaults")).mappings())
    # Restore the old structure and equivalent single default, never expiration,
    # pin/folder assignments, cloud tags, content or historical policy promises.
    importlib.import_module("migrations.versions.0011_lifecycle_policies").upgrade()
    importlib.import_module("migrations.versions.0012_lifecycle_profile_json").upgrade()
    identifier = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    op.create_table("lifecycle_pins",
        sa.Column("vault_id", identifier, sa.ForeignKey("vaults.id", ondelete="CASCADE"), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("is_directory", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("pinned_at", sa.Text(), nullable=False),
        sa.Column("pinned_by", identifier, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.PrimaryKeyConstraint("vault_id", "path", name="lifecycle_pins_pk"))
    op.create_index("lifecycle_pins_vault_idx", "lifecycle_pins", ["vault_id"])
    with op.batch_alter_table("archive_versions") as batch:
        batch.add_column(sa.Column("desired_policy_id", sa.String(36)))
        batch.add_column(sa.Column("applied_policy_id", sa.String(36)))
    with op.batch_alter_table("directory_aggregates") as batch:
        batch.add_column(sa.Column("pinned_count", identifier, nullable=False, server_default="0"))
        batch.add_column(sa.Column("action_storage_class", identifier, nullable=False, server_default="0"))
        batch.create_check_constraint("directory_aggregates_pinned_ck", "pinned_count >= 0 AND pinned_count <= item_count")
    import uuid
    for row in plans:
        policy_id = str(uuid.uuid4())
        connection.execute(sa.text("INSERT INTO lifecycle_policies(id, vault_id, name, created_at, profile_json) VALUES (:id, :vault, 'Vault storage plan', 'migration-0053', :plan)"), {"id": policy_id, "vault": row["id"], "plan": row["storage_plan_json"]})
        connection.execute(sa.text("UPDATE vaults SET default_lifecycle_policy_id=:policy WHERE id=:id"), {"policy": policy_id, "id": row["id"]})
    with op.batch_alter_table("vaults") as batch:
        batch.drop_column("storage_plan_json")
