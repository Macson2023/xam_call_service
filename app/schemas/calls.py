from datetime import datetime

from pydantic import BaseModel, Field


class CallUserOut(BaseModel):
    id: str
    username: str | None = None
    display_name: str | None = None


class IdentityOut(CallUserOut):
    online: bool = True
    in_call: bool = False


class CallOut(BaseModel):
    id: str
    caller: CallUserOut
    callee: CallUserOut
    media: str
    state: str
    status: str
    end_reason: str | None = None
    created_at: datetime
    answered_at: datetime | None = None
    ended_at: datetime | None = None
    duration_seconds: int = 0


class ConferenceParticipantOut(BaseModel):
    user: CallUserOut
    state: str
    joined_at: datetime | None = None


class ConferenceOut(BaseModel):
    id: str
    kind: str = "conference"
    host_id: str
    title: str | None = None
    media: str
    state: str
    status: str
    end_reason: str | None = None
    created_at: datetime
    ended_at: datetime | None = None
    max_participants: int
    participants: list[ConferenceParticipantOut]
    joined_ids: list[str]


class CallConfigOut(BaseModel):
    ice_servers: list[dict]
    ring_timeout_seconds: int
    disconnect_grace_seconds: int
    max_call_duration_seconds: int
    max_conference_participants: int
    media: list[str]


class WsTicketOut(BaseModel):
    ticket: str
    expires_in: int


class CallHistoryItem(BaseModel):
    id: str
    direction: str
    peer: CallUserOut
    media: str
    status: str
    end_reason: str | None = None
    created_at: datetime
    answered_at: datetime | None = None
    ended_at: datetime | None = None
    duration_seconds: int


class CallHistoryPage(BaseModel):
    items: list[CallHistoryItem]
    total: int
    limit: int = Field(ge=1, le=200)
    offset: int = Field(ge=0)
