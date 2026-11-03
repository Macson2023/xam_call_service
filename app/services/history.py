from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.models.call import CallRecord
from app.repositories.calls import CallRepository


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _duration(record: CallRecord) -> int:
    answered = _as_utc(record.answered_at)
    if answered is None:
        return 0
    ended = _as_utc(record.ended_at) or utcnow()
    return max(0, int((ended - answered).total_seconds()))


def history_item(record: CallRecord, viewer_id: UUID) -> dict:
    outgoing = record.caller_id == viewer_id
    if outgoing:
        peer = {
            "id": str(record.callee_id),
            "username": record.callee_username,
            "display_name": record.callee_display_name,
        }
    else:
        peer = {
            "id": str(record.caller_id),
            "username": record.caller_username,
            "display_name": record.caller_display_name,
        }

    return {
        "id": str(record.id),
        "direction": "outgoing" if outgoing else "incoming",
        "peer": peer,
        "media": record.media,
        "status": record.status,
        "end_reason": record.end_reason,
        "created_at": _as_utc(record.created_at),
        "answered_at": _as_utc(record.answered_at),
        "ended_at": _as_utc(record.ended_at),
        "duration_seconds": _duration(record),
    }


class CallHistoryService:
    def __init__(self, session: AsyncSession) -> None:
        self.repository = CallRepository(session)

    async def page(self, user_id: UUID, *, limit: int, offset: int) -> dict:
        records, total = await self.repository.list_for_user(user_id, limit=limit, offset=offset)
        return {
            "items": [history_item(record, user_id) for record in records],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    async def get(self, call_id: UUID, user_id: UUID) -> dict | None:
        record = await self.repository.get_for_user(call_id, user_id)
        if record is None:
            return None
        return history_item(record, user_id)
