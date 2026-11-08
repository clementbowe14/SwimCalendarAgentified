import json
import logging

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app import db
from app.api_schemas import CoachDistanceBufferTier, CoachSchedulerPayments, CoachSchedulerRules
from app.engine import BufferPolicy, DistanceBufferTier


logger = logging.getLogger(__name__)


class InvalidSchedulerPolicy(RuntimeError):
    """Persisted scheduler settings cannot safely be used."""


def default_scheduler_rules() -> CoachSchedulerRules:
    return CoachSchedulerRules(
        buffers_enabled=True,
        apply_distance_buffer_at_shift_start=False,
        rounding_increment_mins=15,
        post_break_buffer_mins=15,
        distance_buffer_tiers=[
            CoachDistanceBufferTier(id="near", label="Under 5 mi", min_miles=0, max_miles=4.999, buffer_mins=10),
            CoachDistanceBufferTier(id="medium", label="5-10 mi", min_miles=5, max_miles=10, buffer_mins=20),
            CoachDistanceBufferTier(id="far", label="Over 10 mi", min_miles=10.001, max_miles=None, buffer_mins=30),
        ],
        payments=CoachSchedulerPayments(
            enabled_methods=["card", "ach", "cash"],
            cancellation_window_hours=12,
            require_payment_method_for_new_clients=True,
        ),
    )


def load_scheduler_rules(instructor_id: str, *, operation: str) -> CoachSchedulerRules:
    try:
        saved = db.get_scheduler_settings(instructor_id)
        return default_scheduler_rules() if saved is None else CoachSchedulerRules.model_validate(saved)
    except (ValidationError, ValueError, TypeError, json.JSONDecodeError, SQLAlchemyError) as exc:
        if isinstance(exc, json.JSONDecodeError):
            reason = "invalid_json"
        elif isinstance(exc, SQLAlchemyError):
            reason = "read_failed"
        else:
            reason = "invalid_shape"
        logger.error(
            "scheduler_policy_invalid",
            extra={"instructor_id": instructor_id, "operation": operation, "reason_code": reason},
        )
        raise InvalidSchedulerPolicy("Persisted scheduler settings are invalid.") from exc


def to_buffer_policy(rules: CoachSchedulerRules) -> BufferPolicy:
    return BufferPolicy(
        buffers_enabled=rules.buffers_enabled,
        apply_distance_buffer_at_shift_start=rules.apply_distance_buffer_at_shift_start,
        rounding_increment_mins=rules.rounding_increment_mins,
        post_break_buffer_mins=rules.post_break_buffer_mins,
        distance_buffer_tiers=tuple(
            DistanceBufferTier(
                min_miles=tier.min_miles,
                max_miles=tier.max_miles,
                buffer_mins=tier.buffer_mins,
            )
            for tier in rules.distance_buffer_tiers
        ),
    )


def load_buffer_policy(instructor_id: str, *, operation: str) -> BufferPolicy:
    return to_buffer_policy(load_scheduler_rules(instructor_id, operation=operation))
