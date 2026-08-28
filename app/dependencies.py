from dataclasses import dataclass

from app import db
from app.api_errors import ResourceNotFound


@dataclass(frozen=True)
class CoachContext:
    instructor_id: str
    display_name: str


def get_current_coach() -> CoachContext:
    """
    MVP coach resolver.

    Future auth can replace this dependency centrally without changing route
    handler signatures.
    """
    instructor = db.get_instructor_info("inst_001")
    if not instructor:
        raise ResourceNotFound("The development coach profile could not be found.")
    return CoachContext(instructor_id=instructor.id, display_name=instructor.name)
