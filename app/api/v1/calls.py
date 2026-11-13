from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_conferences,
    get_coordinator,
    get_current_identity,
    get_registry,
    get_ticket_store,
)
from app.core.auth import UserIdentity
from app.core.config import settings
from app.db.session import get_db
from app.realtime.conferences import ConferenceRegistry
from app.realtime.coordinator import CallCoordinator
from app.realtime.registry import CallRegistry
from app.realtime.tickets import WsTicketStore
from app.schemas.calls import (
    CallConfigOut,
    CallHistoryPage,
    CallOut,
    ConferenceOut,
    WsTicketOut,
)
from app.services.history import CallHistoryService

router = APIRouter(prefix="/calls", tags=["calls"])


@router.get("/config", response_model=CallConfigOut)
async def call_config(_: UserIdentity = Depends(get_current_identity)) -> dict:
    return {
        "ice_servers": settings.resolved_ice_servers,
        "ring_timeout_seconds": settings.ring_timeout_seconds,
        "disconnect_grace_seconds": settings.disconnect_grace_seconds,
        "max_call_duration_seconds": settings.max_call_duration_seconds,
        "max_conference_participants": settings.max_conference_participants,
        "media": ["audio", "video"],
    }


@router.post("/ws-ticket", response_model=WsTicketOut)
async def issue_ws_ticket(
    identity: UserIdentity = Depends(get_current_identity),
    tickets: WsTicketStore = Depends(get_ticket_store),
) -> dict:
    ticket, expires_in = tickets.issue(identity)
    return {"ticket": ticket, "expires_in": expires_in}


@router.get("/history", response_model=CallHistoryPage)
async def call_history(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=100_000),
    identity: UserIdentity = Depends(get_current_identity),
    session: AsyncSession = Depends(get_db),
    coordinator: CallCoordinator = Depends(get_coordinator),
) -> dict:
    await coordinator.flush_persistence()
    return await CallHistoryService(session).page(identity.id, limit=limit, offset=offset)


@router.get("/active", response_model=CallOut | None)
async def active_call(
    identity: UserIdentity = Depends(get_current_identity),
    registry: CallRegistry = Depends(get_registry),
) -> dict | None:
    call = registry.active_for(identity.id)
    return call.to_dict() if call is not None else None


@router.get("/conference", response_model=ConferenceOut | None)
async def active_conference(
    identity: UserIdentity = Depends(get_current_identity),
    conferences: ConferenceRegistry = Depends(get_conferences),
) -> dict | None:
    conference = conferences.current_for(identity.id)
    return conference.to_dict() if conference is not None else None


@router.get("/{call_id}", response_model=CallOut)
async def get_call(
    call_id: UUID,
    identity: UserIdentity = Depends(get_current_identity),
    registry: CallRegistry = Depends(get_registry),
    session: AsyncSession = Depends(get_db),
    coordinator: CallCoordinator = Depends(get_coordinator),
) -> dict:
    call = registry.get(call_id)
    if call is not None:
        if not call.participant(identity.id):
            raise HTTPException(status_code=404, detail="Call not found")
        return call.to_dict()

    await coordinator.flush_persistence()
    item = await CallHistoryService(session).get(call_id, identity.id)
    if item is None:
        raise HTTPException(status_code=404, detail="Call not found")
    me = identity.to_public()
    peer = item["peer"]
    caller = me if item["direction"] == "outgoing" else peer
    callee = peer if item["direction"] == "outgoing" else me
    return {
        "id": item["id"],
        "caller": caller,
        "callee": callee,
        "media": item["media"],
        "state": "ended",
        "status": item["status"],
        "end_reason": item["end_reason"],
        "created_at": item["created_at"],
        "answered_at": item["answered_at"],
        "ended_at": item["ended_at"],
        "duration_seconds": item["duration_seconds"],
    }
