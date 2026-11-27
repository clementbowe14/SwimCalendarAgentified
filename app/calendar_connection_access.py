from urllib.parse import quote

from fastapi import Depends, Header

from app.api_errors import ApiException
from app.dependencies import CoachContext, get_current_coach


EXPECTED_CALENDAR_INSTRUCTOR_HEADER = "X-Expected-Instructor-Id"


def get_expected_instructor_coach(
    coach: CoachContext = Depends(get_current_coach),
    expected_instructor: str | None = Header(None, alias=EXPECTED_CALENDAR_INSTRUCTOR_HEADER),
) -> CoachContext:
    if expected_instructor is not None:
        require_expected_instructor(coach, expected_instructor)
    return coach


def require_expected_instructor(coach: CoachContext, expected_instructor: str) -> None:
    if expected_instructor != quote(coach.instructor_id, safe="!~*'()"):
        raise ApiException(
            "CALENDAR_CONNECTION_OWNER_CHANGED",
            "Your signed-in provider changed. Reload Dayweave and try again.",
            409,
        )
