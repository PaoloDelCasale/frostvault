"""Join independent bulk admission and scheduler turn migrations for v0.5.3.

Neither feature depends on the other's schema. Preserve both revision identities
and require both branches before the application serves the combined release.
"""

revision = "0071_bulk_scheduler_merge"
down_revision = ("bulk_upload_admission_550", "scheduler_turns_552")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
