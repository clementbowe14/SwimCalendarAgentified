import json
import logging

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app import db
from app.api_schemas import (
    CoachDistanceBufferTier,
    CoachSchedulerPayments,
    CoachSchedulerRules,
    CoachSchedulingPolicyResponse,
)
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


def default_scheduling_policy() -> CoachSchedulingPolicyResponse:
    return _scheduling_policy_from_rules(default_scheduler_rules(), revision=0)


def load_scheduling_policy(
    instructor_id: str,
    *,
    operation: str,
) -> CoachSchedulingPolicyResponse:
    try:
        saved = db.get_scheduling_policy(instructor_id)
        if saved is None:
            return default_scheduling_policy()
        return CoachSchedulingPolicyResponse.model_validate(
            {
                "revision": saved["policy_revision"],
                "distance_buffers_enabled": saved["buffers_enabled"],
                "post_break_recovery_mins": saved["post_break_buffer_mins"],
                "distance_buffer_tiers": saved["distance_buffer_tiers"],
            }
        )
    except (ValidationError, ValueError, TypeError, KeyError, json.JSONDecodeError, SQLAlchemyError) as exc:
        _log_invalid_policy(instructor_id, operation, exc)
        raise InvalidSchedulerPolicy("Persisted scheduler settings are invalid.") from exc


def load_scheduler_settings_projection(
    instructor_id: str,
    *,
    operation: str,
) -> tuple[CoachSchedulerRules, CoachSchedulingPolicyResponse]:
    """Load the legacy and canonical projections from one persisted snapshot."""

    try:
        saved = db.get_scheduler_settings(instructor_id)
        if saved is None:
            rules = default_scheduler_rules()
            return rules, _scheduling_policy_from_rules(rules, revision=0)
        rules = CoachSchedulerRules.model_validate(saved)
        policy = CoachSchedulingPolicyResponse.model_validate(
            {
                "revision": saved["policy_revision"],
                "distance_buffers_enabled": saved["buffers_enabled"],
                "post_break_recovery_mins": saved["post_break_buffer_mins"],
                "distance_buffer_tiers": saved["distance_buffer_tiers"],
            }
        )
        return rules, policy
    except (ValidationError, ValueError, TypeError, KeyError, json.JSONDecodeError, SQLAlchemyError) as exc:
        _log_invalid_policy(instructor_id, operation, exc)
        raise InvalidSchedulerPolicy("Persisted scheduler settings are invalid.") from exc


def to_buffer_policy(policy: CoachSchedulingPolicyResponse) -> BufferPolicy:
    return BufferPolicy(
        distance_buffers_enabled=policy.distance_buffers_enabled,
        post_break_recovery_mins=policy.post_break_recovery_mins,
        distance_buffer_tiers=tuple(
            DistanceBufferTier(
                min_miles=tier.min_miles,
                max_miles=tier.max_miles,
                buffer_mins=tier.buffer_mins,
            )
            for tier in policy.distance_buffer_tiers
        ),
    )


def load_buffer_policy(instructor_id: str, *, operation: str) -> BufferPolicy:
    return to_buffer_policy(load_scheduling_policy(instructor_id, operation=operation))


def save_scheduling_policy(
    instructor_id: str,
    *,
    expected_revision: int,
    policy: CoachSchedulingPolicyResponse,
) -> CoachSchedulingPolicyResponse | None:
    saved = db.save_scheduling_policy_cas(
        instructor_id,
        expected_revision=expected_revision,
        distance_buffers_enabled=policy.distance_buffers_enabled,
        post_break_recovery_mins=policy.post_break_recovery_mins,
        distance_buffer_tiers=[
            tier.model_dump(mode="json")
            for tier in policy.distance_buffer_tiers
        ],
    )
    if saved is None:
        return None
    return _scheduling_policy_from_saved_record(saved)


def _scheduling_policy_from_saved_record(saved: dict) -> CoachSchedulingPolicyResponse:
    return CoachSchedulingPolicyResponse.model_validate(
        {
            "revision": saved["policy_revision"],
            "distance_buffers_enabled": saved["buffers_enabled"],
            "post_break_recovery_mins": saved["post_break_buffer_mins"],
            "distance_buffer_tiers": saved["distance_buffer_tiers"],
        }
    )


def _scheduling_policy_from_rules(
    rules: CoachSchedulerRules,
    *,
    revision: int,
) -> CoachSchedulingPolicyResponse:
    return CoachSchedulingPolicyResponse(
        revision=revision,
        distance_buffers_enabled=rules.buffers_enabled,
        post_break_recovery_mins=rules.post_break_buffer_mins,
        distance_buffer_tiers=rules.distance_buffer_tiers,
    )


def _log_invalid_policy(
    instructor_id: str,
    operation: str,
    exc: Exception,
) -> None:
    if isinstance(exc, json.JSONDecodeError):
        reason = "invalid_json"
    elif isinstance(exc, SQLAlchemyError):
        reason = "read_failed"
    else:
        reason = "invalid_shape"
    logger.error(
        "scheduler_policy_invalid",
        extra={
            "instructor_id": instructor_id,
            "operation": operation,
            "reason_code": reason,
        },
    )
