from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from app.core.auth import UserIdentity
from app.db.base import utcnow

MEDIA_TYPES = frozenset({"audio", "video"})
RINGING = "ringing"
ACTIVE = "active"
ENDED = "ended"


class CallError(Exception):
    def __init__(self, code: str, detail: str, status_code: int = 409) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


@dataclass(slots=True)
class Call:
    id: UUID
    caller: UserIdentity
    callee: UserIdentity
    media: str
    state: str = RINGING
    created_at: datetime = field(default_factory=utcnow)
    answered_at: datetime | None = None
    ended_at: datetime | None = None
    end_reason: str | None = None

    @property
    def status(self) -> str:
        if self.state == RINGING:
            return "ringing"
        if self.state == ACTIVE:
            return "active"
        if self.end_reason == "timeout":
            return "missed"
        if self.end_reason == "rejected":
            return "rejected"
        if self.end_reason == "cancelled":
            return "cancelled"
        if self.answered_at is not None:
            return "completed"
        return "failed"

    @property
    def duration_seconds(self) -> int:
        if self.answered_at is None:
            return 0
        end = self.ended_at or utcnow()
        return max(0, int((end - self.answered_at).total_seconds()))

    def partner_id(self, user_id: UUID) -> UUID:
        if user_id == self.caller.id:
            return self.callee.id
        if user_id == self.callee.id:
            return self.caller.id
        raise CallError("forbidden", "You are not a participant of this call", 403)

    def participant(self, user_id: UUID) -> bool:
        return user_id in (self.caller.id, self.callee.id)

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "caller": self.caller.to_public(),
            "callee": self.callee.to_public(),
            "media": self.media,
            "state": self.state,
            "status": self.status,
            "end_reason": self.end_reason,
            "created_at": _iso(self.created_at),
            "answered_at": _iso(self.answered_at),
            "ended_at": _iso(self.ended_at),
            "duration_seconds": self.duration_seconds,
        }


class CallRegistry:
    """In-memory registry of 1:1 calls with a strict state machine."""

    def __init__(self) -> None:
        self._calls: dict[UUID, Call] = {}
        self._by_user: dict[UUID, UUID] = {}

    def create(
        self,
        caller: UserIdentity,
        callee: UserIdentity,
        media: str,
    ) -> Call:
        if media not in MEDIA_TYPES:
            raise CallError("invalid_media", "Media type must be 'audio' or 'video'", 422)
        if caller.id == callee.id:
            raise CallError("self_call", "You cannot call yourself", 400)
        if caller.id in self._by_user:
            raise CallError("caller_busy", "You are already in a call", 409)
        if callee.id in self._by_user:
            raise CallError("callee_busy", "User is already in a call", 409)

        call = Call(
            id=uuid4(),
            caller=caller,
            callee=callee,
            media=media,
        )
        self._calls[call.id] = call
        self._by_user[caller.id] = call.id
        self._by_user[callee.id] = call.id
        return call

    def get(self, call_id: UUID) -> Call | None:
        return self._calls.get(call_id)

    def active_for(self, user_id: UUID) -> Call | None:
        call_id = self._by_user.get(user_id)
        if call_id is None:
            return None
        call = self._calls.get(call_id)
        if call is None or call.state == ENDED:
            return None
        return call

    def is_in_call(self, user_id: UUID) -> bool:
        return self.active_for(user_id) is not None

    def expect(
        self,
        call_id: UUID,
        user_id: UUID,
        *,
        role: str | None = None,
        states: frozenset[str] | None = None,
    ) -> Call:
        call = self._calls.get(call_id)
        if call is None:
            raise CallError("call_not_found", "Call not found", 404)
        if not call.participant(user_id):
            raise CallError("forbidden", "You are not a participant of this call", 403)
        if role == "caller" and call.caller.id != user_id:
            raise CallError("forbidden", "Only the caller can perform this action", 403)
        if role == "callee" and call.callee.id != user_id:
            raise CallError("forbidden", "Only the callee can perform this action", 403)
        if states is not None and call.state not in states:
            raise CallError("invalid_state", "Call is not in a valid state for this action", 409)
        return call

    def accept(self, call_id: UUID, user_id: UUID) -> Call:
        call = self.expect(call_id, user_id, role="callee", states=frozenset({RINGING}))
        call.state = ACTIVE
        call.answered_at = utcnow()
        return call

    def finish(self, call_id: UUID, reason: str) -> Call | None:
        call = self._calls.get(call_id)
        if call is None or call.state == ENDED:
            return None
        call.state = ENDED
        call.ended_at = utcnow()
        call.end_reason = reason
        return call

    def release(self, call_id: UUID) -> None:
        call = self._calls.pop(call_id, None)
        if call is None:
            return
        for user_id in (call.caller.id, call.callee.id):
            if self._by_user.get(user_id) == call_id:
                self._by_user.pop(user_id, None)

    def calls(self) -> list[Call]:
        return list(self._calls.values())
