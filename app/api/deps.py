from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.auth import AuthError, UserIdentity
from app.realtime.conferences import ConferenceRegistry
from app.realtime.connections import ConnectionManager
from app.realtime.coordinator import CallCoordinator
from app.realtime.registry import CallRegistry
from app.realtime.tickets import WsTicketStore
from app.services.backend import BackendClient, BackendError
from app.services.directory import UserDirectory

bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True, slots=True)
class AuthContext:
    identity: UserIdentity
    token: str


def get_directory(request: Request) -> UserDirectory:
    return request.app.state.directory


def get_backend(request: Request) -> BackendClient:
    return request.app.state.backend


def get_connections(request: Request) -> ConnectionManager:
    return request.app.state.connections


def get_registry(request: Request) -> CallRegistry:
    return request.app.state.registry


def get_conferences(request: Request) -> ConferenceRegistry:
    return request.app.state.conferences


def get_coordinator(request: Request) -> CallCoordinator:
    return request.app.state.coordinator


def get_ticket_store(request: Request) -> WsTicketStore:
    return request.app.state.tickets


async def get_auth_context(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    directory: UserDirectory = Depends(get_directory),
) -> AuthContext:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise unauthorized
    try:
        identity = await directory.identity_for_token(credentials.credentials)
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=exc.detail,
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except BackendError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=exc.detail,
        ) from exc
    return AuthContext(identity=identity, token=credentials.credentials)


async def get_current_identity(
    context: AuthContext = Depends(get_auth_context),
) -> UserIdentity:
    return context.identity
