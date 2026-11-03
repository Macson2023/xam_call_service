from uuid import UUID

from app.core.auth import UserIdentity
from app.core.config import Settings
from app.realtime.registry import Call


def ready_event(identity: UserIdentity, call: Call | None, settings: Settings) -> dict:
    return {
        "type": "ready",
        "data": {
            "user": identity.to_public(),
            "call": call.to_dict() if call is not None else None,
            "ring_timeout_seconds": settings.ring_timeout_seconds,
            "disconnect_grace_seconds": settings.disconnect_grace_seconds,
            "media": sorted(["audio", "video"]),
        },
    }


def call_event(event_type: str, call: Call) -> dict:
    return {"type": event_type, "data": {"call": call.to_dict()}}


def signal_event(
    event_type: str,
    call_id: UUID,
    from_user_id: UUID,
    **payload,
) -> dict:
    data = {
        "call_id": str(call_id),
        "from_user_id": str(from_user_id),
    }
    data.update(payload)
    return {"type": event_type, "data": data}


def presence_state(user_id: UUID, *, online: bool, in_call: bool) -> dict:
    return {
        "type": "presence.state",
        "data": {
            "user_id": str(user_id),
            "online": online,
            "in_call": in_call,
        },
    }


def error_event(code: str, detail: str) -> dict:
    return {"type": "error", "data": {"code": code, "detail": detail}}
