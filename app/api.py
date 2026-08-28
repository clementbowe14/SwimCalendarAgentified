import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware

from app import db
from app.api_errors import (
    ApiException,
    api_exception_handler,
    unexpected_exception_handler,
    validation_exception_handler,
)
from app.routes import coach, health, public


DEFAULT_CORS_ORIGINS = (
    "http://127.0.0.1:3000",
    "http://localhost:3000",
    "http://127.0.0.1:3001",
    "http://localhost:3001",
    "http://127.0.0.1:3002",
    "http://localhost:3002",
    "http://127.0.0.1:4173",
    "http://localhost:4173",
    "http://127.0.0.1:5173",
    "http://localhost:5173",
)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    db.init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Swim Command Local API",
        version="0.1.0",
        description="Local HTTP API for the swim scheduling prototype.",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    app.add_exception_handler(ApiException, api_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unexpected_exception_handler)

    app.include_router(health.router)
    app.include_router(coach.router)
    app.include_router(public.router)
    return app


def _cors_origins() -> list[str]:
    configured = os.getenv("SWIM_API_CORS_ORIGINS")
    if not configured:
        return list(DEFAULT_CORS_ORIGINS)
    return [origin.strip() for origin in configured.split(",") if origin.strip()]


app = create_app()
