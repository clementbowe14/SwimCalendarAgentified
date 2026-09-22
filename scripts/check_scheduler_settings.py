"""Validate persisted scheduler settings before enabling strict policy reads."""

import json
import sys
from pathlib import Path
from typing import Optional

from pydantic import ValidationError


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import db
from app.api_schemas import CoachSchedulerRules
from app.models import SchedulerSettingsDB


def main() -> int:
    failures: list[tuple[str, str]] = []
    with db.SessionLocal() as session:
        rows = session.query(SchedulerSettingsDB).order_by(SchedulerSettingsDB.instructor_id).all()
        for row in rows:
            try:
                CoachSchedulerRules.model_validate(
                    {
                        "buffers_enabled": row.buffers_enabled,
                        "apply_distance_buffer_at_shift_start": row.apply_distance_buffer_at_shift_start,
                        "rounding_increment_mins": row.rounding_increment_mins,
                        "post_break_buffer_mins": row.post_break_buffer_mins,
                        "distance_buffer_tiers": json.loads(row.distance_buffer_tiers),
                        "payments": {
                            "enabled_methods": _split_csv(row.payments_enabled_methods),
                            "cancellation_window_hours": row.cancellation_window_hours,
                            "require_payment_method_for_new_clients": row.require_payment_method_for_new_clients,
                        },
                    }
                )
            except json.JSONDecodeError:
                failures.append((row.instructor_id, "invalid_json"))
            except (ValidationError, TypeError, ValueError):
                failures.append((row.instructor_id, "invalid_shape"))

    if failures:
        for instructor_id, reason in failures:
            print(f"scheduler_policy_invalid instructor_id={instructor_id} reason_code={reason}")
        return 1
    print(f"scheduler_settings_preflight_ok rows={len(rows)}")
    return 0


def _split_csv(value: Optional[str]) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


if __name__ == "__main__":
    raise SystemExit(main())
