"""Add durable client-create idempotency metadata.

Revision ID: 20261111_0014
Revises: 20261110_0013
Create Date: 2026-11-11
"""

from alembic import op
import sqlalchemy as sa


revision = "20261111_0014"
down_revision = "20261110_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    _require_postgresql(bind)
    _lock_clients()

    op.add_column(
        "clients",
        sa.Column("create_idempotency_key", sa.String(160), nullable=True),
    )
    op.add_column(
        "clients",
        sa.Column("client_create_payload", sa.JSON(), nullable=True),
    )
    op.create_check_constraint(
        "ck_clients_create_replay_complete",
        "clients",
        "(create_idempotency_key IS NULL) = (client_create_payload IS NULL)",
    )
    op.create_index(
        "uq_clients_instructor_create_idempotency_key",
        "clients",
        ["instructor_id", "create_idempotency_key"],
        unique=True,
        postgresql_where=sa.text("create_idempotency_key IS NOT NULL"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    _require_postgresql(bind)
    _lock_clients()

    op.drop_index(
        "uq_clients_instructor_create_idempotency_key",
        table_name="clients",
    )
    op.drop_constraint(
        "ck_clients_create_replay_complete",
        "clients",
        type_="check",
    )
    op.drop_column("clients", "client_create_payload")
    op.drop_column("clients", "create_idempotency_key")


def _lock_clients() -> None:
    op.execute("LOCK TABLE clients IN ACCESS EXCLUSIVE MODE NOWAIT")


def _require_postgresql(bind) -> None:
    if bind.dialect.name != "postgresql":
        raise RuntimeError(
            "Client mutation idempotency migration requires PostgreSQL."
        )
