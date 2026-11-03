"""Plaintext Vaults only (#419).

Reject encrypted test Vaults before any DDL. Never convert cloud keys, discard
legacy secrets, call S3 or reinterpret ciphertext as plaintext. Operators must
recover legacy content with the old release before using a fresh installation.
Metadata backup artifacts and their encryption are unaffected.
"""
from alembic import op
import sqlalchemy as sa

revision = "0051_plaintext_vaults"
down_revision = "0050_current_snapshot_only"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    if connection.execute(sa.text("""
        SELECT 1 FROM vaults
        WHERE encryption_mode <> 'plain' OR encryption_mode IS NULL
           OR crypt_password_ciphertext IS NOT NULL
           OR crypt_password2_ciphertext IS NOT NULL
        LIMIT 1
    """)).first():
        # Fixed, non-secret error: do not include rows or ciphertext.
        raise RuntimeError(
            "Legacy encrypted Vaults are unsupported. Upgrade refused without "
            "changing objects or secrets; recover with the previous release "
            "before creating a fresh plaintext installation."
        )
    with op.batch_alter_table("vaults") as batch:
        batch.drop_constraint("vaults_crypt_secrets_ck", type_="check")
        batch.drop_constraint("vaults_encryption_mode_ck", type_="check")
        batch.drop_column("recovery_custody_confirmed_at")
        batch.drop_column("crypt_password2_ciphertext")
        batch.drop_column("crypt_password_ciphertext")
        batch.drop_column("encryption_mode")


def downgrade() -> None:
    with op.batch_alter_table("vaults") as batch:
        batch.add_column(sa.Column("encryption_mode", sa.Text(), nullable=False,
                                   server_default="plain"))
        batch.add_column(sa.Column("crypt_password_ciphertext", sa.Text()))
        batch.add_column(sa.Column("crypt_password2_ciphertext", sa.Text()))
        batch.add_column(sa.Column("recovery_custody_confirmed_at", sa.Text()))
        batch.create_check_constraint("vaults_encryption_mode_ck",
                                      "encryption_mode IN ('plain', 'crypt')")
        batch.create_check_constraint("vaults_crypt_secrets_ck", """
            (encryption_mode = 'plain' AND crypt_password_ciphertext IS NULL
                AND crypt_password2_ciphertext IS NULL)
            OR (encryption_mode = 'crypt' AND crypt_password_ciphertext IS NOT NULL
                AND crypt_password2_ciphertext IS NOT NULL)
        """)
