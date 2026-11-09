"""Add optimistic revisions for provider scheduling policies.

Revision ID: 20261109_0011
Revises: 20261109_0010
Create Date: 2026-11-09
"""

from alembic import op
import sqlalchemy as sa


revision = "20261109_0011"
down_revision = "20261109_0010"
branch_labels = None
depends_on = None


_TRIGGER_NAME = "trg_scheduler_settings_policy_revision"
_FUNCTION_NAME = "bump_scheduler_settings_policy_revision"
_CHECK_NAME = "ck_scheduler_settings_policy_revision_nonnegative"


def upgrade() -> None:
    bind = op.get_bind()
    _require_postgresql(bind)

    op.add_column(
        "scheduler_settings",
        sa.Column(
            "policy_revision",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.create_check_constraint(
        _CHECK_NAME,
        "scheduler_settings",
        "policy_revision >= 0",
    )
    op.execute(
        f"""
        CREATE FUNCTION {_FUNCTION_NAME}()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.policy_revision = OLD.policy_revision
               AND (
                    NEW.buffers_enabled IS DISTINCT FROM OLD.buffers_enabled
                    OR NEW.apply_distance_buffer_at_shift_start
                        IS DISTINCT FROM OLD.apply_distance_buffer_at_shift_start
                    OR NEW.rounding_increment_mins
                        IS DISTINCT FROM OLD.rounding_increment_mins
                    OR NEW.post_break_buffer_mins
                        IS DISTINCT FROM OLD.post_break_buffer_mins
                    OR NEW.distance_buffer_tiers
                        IS DISTINCT FROM OLD.distance_buffer_tiers
               )
            THEN
                NEW.policy_revision := OLD.policy_revision + 1;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {_TRIGGER_NAME}
        BEFORE UPDATE ON scheduler_settings
        FOR EACH ROW
        EXECUTE FUNCTION {_FUNCTION_NAME}()
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    _require_postgresql(bind)

    op.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER_NAME} ON scheduler_settings")
    op.execute(f"DROP FUNCTION IF EXISTS {_FUNCTION_NAME}()")
    op.drop_constraint(_CHECK_NAME, "scheduler_settings", type_="check")
    op.drop_column("scheduler_settings", "policy_revision")


def _require_postgresql(bind) -> None:
    if bind.dialect.name != "postgresql":
        raise RuntimeError(
            "This backend migration targets PostgreSQL. "
            "Set SWIM_DATABASE_URL to a PostgreSQL database."
        )
