from dataclasses import dataclass
from typing import Optional

from fastapi import Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api_errors import AuthenticationRequired
from app.auth import verify_access_token


bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class CoachContext:
    instructor_id: str
    display_name: str


def get_current_coach(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(bearer_scheme),
) -> CoachContext:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthenticationRequired()
    principal = verify_access_token(credentials.credentials)
    return CoachContext(instructor_id=principal.instructor_id, display_name=principal.display_name)
