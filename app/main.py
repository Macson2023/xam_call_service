import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1 import realtime
from app.api.v1.router import api_router
from app.core.auth import TokenValidator
from app.core.config import settings
from app.core.errors import http_error_handler, validation_error_handler
from app.db.session import async_session_factory, engine, init_db
from app.realtime.connections import ConnectionManager
from app.realtime.coordinator import CallCoordinator
from app.realtime.registry import CallRegistry
from app.realtime.tickets import WsTicketStore
from app.services.backend import BackendClient
from app.services.directory import UserDirectory

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    if settings.auto_create_tables:
        await init_db()

    introspection_client = (
        httpx.AsyncClient(timeout=settings.backend_timeout_seconds)
        if settings.auth_mode == "introspect"
        else None
    )
    backend = BackendClient(settings)
    validator = TokenValidator(settings, introspection_client)
    directory = UserDirectory(settings, validator, backend)
    connections = ConnectionManager(settings.ws_send_timeout_seconds)
    registry = CallRegistry()
    coordinator = CallCoordinator(
        connections,
        registry,
        directory,
        settings,
        async_session_factory,
    )
    tickets = WsTicketStore(settings.ws_ticket_ttl_seconds)

    application.state.settings = settings
    application.state.backend = backend
    application.state.validator = validator
    application.state.directory = directory
    application.state.connections = connections
    application.state.registry = registry
    application.state.coordinator = coordinator
    application.state.tickets = tickets

    logger.info(
        "xam_call_service started (auth_mode=%s, backend=%s)",
        settings.auth_mode,
        settings.backend_base,
    )
    try:
        yield
    finally:
        await coordinator.shutdown()
        await backend.aclose()
        if introspection_client is not None:
            await introspection_client.aclose()
        await engine.dispose()


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description="Signaling, call history and WebRTC test client for xam audio/video calls.",
    lifespan=lifespan,
    docs_url="/docs",
    openapi_url="/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_exception_handler(StarletteHTTPException, http_error_handler)
app.add_exception_handler(RequestValidationError, validation_error_handler)

app.include_router(api_router, prefix=settings.api_v1_prefix)
app.include_router(realtime.router)

static_directory = Path(__file__).parent / "static"
static_directory.mkdir(parents=True, exist_ok=True)
app.mount("/client", StaticFiles(directory=static_directory, html=True), name="client")


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/client/")


@app.get("/health", tags=["system"])
async def health() -> dict[str, str]:
    return {"status": "ok", "service": settings.app_name}
