"""Add encrypted provider calendar OAuth PKCE-verifier storage.

Revision ID: 20261112_0017
Revises: 20261112_0016
Create Date: 2026-11-12
"""

from alembic import op
import sqlalchemy as sa


revision = "20261112_0017"
down_revision = "20261112_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    _require_postgresql(bind)

    op.execute(
        "LOCK TABLE provider_calendar_oauth_states "
        "IN SHARE ROW EXCLUSIVE MODE NOWAIT"
    )
    op.create_table(
        "provider_calendar_oauth_pkce_verifiers",
        sa.Column("state_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("verifier_version", sa.Integer(), nullable=False),
        sa.Column("verifier_key_id", sa.String(length=64), nullable=False),
        sa.Column("verifier_nonce", sa.LargeBinary(length=12), nullable=False),
        sa.Column("verifier_ciphertext", sa.LargeBinary(), nullable=False),
        sa.CheckConstraint(
            "octet_length(state_digest) = 32",
            name="ck_provider_calendar_oauth_pkce_verifiers_digest_length",
        ),
        sa.CheckConstraint(
            "verifier_version = 1",
            name="ck_provider_calendar_oauth_pkce_verifiers_version",
        ),
        sa.CheckConstraint(
            "verifier_key_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'",
            name="ck_provider_calendar_oauth_pkce_verifiers_key_id",
        ),
        sa.CheckConstraint(
            "octet_length(verifier_nonce) = 12",
            name="ck_provider_calendar_oauth_pkce_verifiers_nonce_length",
        ),
        sa.CheckConstraint(
            "octet_length(verifier_ciphertext) = 59",
            name="ck_provider_calendar_oauth_pkce_verifiers_ciphertext_length",
        ),
        sa.ForeignKeyConstraint(
            ["state_digest"],
            ["provider_calendar_oauth_states.state_digest"],
            name="fk_provider_calendar_oauth_pkce_verifiers_state",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "state_digest",
            name="pk_provider_calendar_oauth_pkce_verifiers",
        ),
    )
    op.create_index(
        "uq_provider_calendar_oauth_pkce_verifiers_key_nonce",
        "provider_calendar_oauth_pkce_verifiers",
        ["verifier_key_id", "verifier_nonce"],
        unique=True,
    )


def downgrade() -> None:
    bind = op.get_bind()
    _require_postgresql(bind)

    op.execute(
        "LOCK TABLE provider_calendar_oauth_states, "
        "provider_calendar_oauth_pkce_verifiers "
        "IN ACCESS EXCLUSIVE MODE NOWAIT"
    )
    if bind.execute(
        sa.text(
            "SELECT EXISTS ("
            "SELECT 1 "
            "FROM provider_calendar_oauth_pkce_verifiers AS verifier "
            "JOIN provider_calendar_oauth_states AS oauth_state "
            "USING (state_digest) "
            "WHERE oauth_state.expires_at > CURRENT_TIMESTAMP"
            ")"
        )
    ).scalar_one():
        raise RuntimeError(
            "Cannot downgrade calendar OAuth PKCE-verifier storage while an "
            "authorization attempt is active. Retry after it expires or is consumed."
        )

    op.drop_index(
        "uq_provider_calendar_oauth_pkce_verifiers_key_nonce",
        table_name="provider_calendar_oauth_pkce_verifiers",
    )
    op.drop_table("provider_calendar_oauth_pkce_verifiers")


def _require_postgresql(bind) -> None:
    if bind.dialect.name != "postgresql":
        raise RuntimeError(
            "Calendar OAuth PKCE-verifier storage migration requires PostgreSQL."
        )
