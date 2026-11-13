from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import (
    AuthContext,
    get_auth_context,
    get_backend,
    get_conferences,
    get_connections,
    get_registry,
)
from app.realtime.conferences import ConferenceRegistry
from app.realtime.connections import ConnectionManager
from app.realtime.registry import CallRegistry
from app.schemas.calls import IdentityOut
from app.services.backend import BackendClient, BackendError

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=IdentityOut)
async def get_me(
    context: AuthContext = Depends(get_auth_context),
    registry: CallRegistry = Depends(get_registry),
    conferences: ConferenceRegistry = Depends(get_conferences),
    connections: ConnectionManager = Depends(get_connections),
) -> dict:
    return {
        **context.identity.to_public(),
        "online": connections.is_online(context.identity.id),
        "in_call": (
            registry.is_in_call(context.identity.id)
            or conferences.is_in_conference(context.identity.id)
        ),
    }


@router.get("/search", response_model=list[IdentityOut])
async def search_users(
    q: str = Query(min_length=1, max_length=32),
    limit: int = Query(default=20, ge=1, le=50),
    context: AuthContext = Depends(get_auth_context),
    backend: BackendClient = Depends(get_backend),
    connections: ConnectionManager = Depends(get_connections),
    registry: CallRegistry = Depends(get_registry),
    conferences: ConferenceRegistry = Depends(get_conferences),
) -> list[dict]:
    try:
        users = await backend.search_users(context.token, q, limit)
    except BackendError as exc:
        status_code = exc.status_code if 400 <= exc.status_code <= 499 else 502
        raise HTTPException(status_code=status_code, detail=exc.detail) from exc

    result: list[dict] = []
    for user in users:
        try:
            user_id = UUID(str(user.get("id")))
        except (TypeError, ValueError):
            continue
        result.append(
            {
                "id": str(user_id),
                "username": user.get("username"),
                "display_name": user.get("display_name"),
                "online": connections.is_online(user_id),
                "in_call": (registry.is_in_call(user_id) or conferences.is_in_conference(user_id)),
            }
        )
    return result
