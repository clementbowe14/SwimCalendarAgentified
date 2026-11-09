"""Validate persisted scheduler settings before enabling strict policy reads."""

import json
import sys
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import inspect, text


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import db
from app.api_schemas import CoachSchedulingPolicyResponse


def main() -> int:
    failures: list[tuple[str, str]] = []
    with db.SessionLocal() as session:
        columns = {
            column["name"]
            for column in inspect(session.get_bind()).get_columns("scheduler_settings")
        }
        revision_projection = (
            "policy_revision" if "policy_revision" in columns else "0 AS policy_revision"
        )
        rows = session.execute(
            text(
                "SELECT instructor_id, buffers_enabled, post_break_buffer_mins, "
                f"distance_buffer_tiers, {revision_projection} "
                "FROM scheduler_settings ORDER BY instructor_id"
            )
        ).mappings().all()
        for row in rows:
            try:
                CoachSchedulingPolicyResponse.model_validate(
                    {
                        "revision": row["policy_revision"],
                        "distance_buffers_enabled": row["buffers_enabled"],
                        "post_break_recovery_mins": row["post_break_buffer_mins"],
                        "distance_buffer_tiers": json.loads(row["distance_buffer_tiers"]),
                    }
                )
            except json.JSONDecodeError:
                failures.append((row["instructor_id"], "invalid_json"))
            except (ValidationError, TypeError, ValueError):
                failures.append((row["instructor_id"], "invalid_shape"))

    if failures:
        for instructor_id, reason in failures:
            print(f"scheduler_policy_invalid instructor_id={instructor_id} reason_code={reason}")
        return 1
    print(f"scheduling_policy_preflight_ok rows={len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
