from fastapi import APIRouter, Request

from app.api_schemas import HealthResponse, ReadinessResponse


router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/ready", response_model=ReadinessResponse)
def readiness(request: Request) -> ReadinessResponse:
    return ReadinessResponse(
        status="ready",
        routing=request.app.state.routing_readiness,
    )
