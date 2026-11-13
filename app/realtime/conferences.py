from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from app.core.auth import UserIdentity
from app.db.base import utcnow
from app.realtime.registry import ACTIVE, ENDED, MEDIA_TYPES, CallError

INVITED = "invited"
JOINED = "joined"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


@dataclass(slots=True)
class ConferenceParticipant:
    identity: UserIdentity
    state: str = INVITED
    joined_at: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "user": self.identity.to_public(),
            "state": self.state,
            "joined_at": _iso(self.joined_at),
        }


@dataclass(slots=True)
class Conference:
    id: UUID
    host_id: UUID
    media: str
    max_participants: int
    title: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    ended_at: datetime | None = None
    end_reason: str | None = None
    participants: dict[UUID, ConferenceParticipant] = field(default_factory=dict)

    @property
    def is_ended(self) -> bool:
        return self.ended_at is not None

    @property
    def state(self) -> str:
        return ENDED if self.is_ended else ACTIVE

    def participant(self, user_id: UUID) -> ConferenceParticipant | None:
        return self.participants.get(user_id)

    def joined_participants(self) -> list[tuple[UUID, ConferenceParticipant]]:
        return [
            (user_id, participant)
            for user_id, participant in self.participants.items()
            if participant.state == JOINED
        ]

    @property
    def joined_count(self) -> int:
        return sum(1 for p in self.participants.values() if p.state == JOINED)

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "kind": "conference",
            "host_id": str(self.host_id),
            "title": self.title,
            "media": self.media,
            "state": self.state,
            "status": self.state,
            "end_reason": self.end_reason,
            "created_at": _iso(self.created_at),
            "ended_at": _iso(self.ended_at),
            "max_participants": self.max_participants,
            "participants": [p.to_dict() for p in self.participants.values()],
            "joined_ids": [str(uid) for uid, p in self.joined_participants()],
        }


class ConferenceRegistry:
    """In-memory registry of many-to-many conference rooms (full mesh signaling)."""

    def __init__(self, max_participants: int = 8) -> None:
        self.max_participants = max(2, max_participants)
        self._conferences: dict[UUID, Conference] = {}
        self._joined: dict[UUID, UUID] = {}
        self._invited: dict[UUID, set[UUID]] = {}

    def create(
        self,
        host: UserIdentity,
        media: str,
        title: str | None,
        invitees: list[UserIdentity],
    ) -> Conference:
        if media not in MEDIA_TYPES:
            raise CallError("invalid_media", "Media type must be 'audio' or 'video'", 422)
        if host.id in self._joined:
            raise CallError("already_in_conference", "You are already in a conference", 409)

        unique: dict[UUID, UserIdentity] = {}
        for identity in invitees:
            if identity.id != host.id:
                unique.setdefault(identity.id, identity)
        if 1 + len(unique) > self.max_participants:
            raise CallError(
                "conference_full",
                f"Conference is limited to {self.max_participants} participants",
                409,
            )

        conference = Conference(
            id=uuid4(),
            host_id=host.id,
            media=media,
            max_participants=self.max_participants,
            title=title,
        )
        conference.participants[host.id] = ConferenceParticipant(
            identity=host,
            state=JOINED,
            joined_at=conference.created_at,
        )
        for identity in unique.values():
            conference.participants[identity.id] = ConferenceParticipant(identity=identity)
            self._invited.setdefault(identity.id, set()).add(conference.id)

        self._conferences[conference.id] = conference
        self._joined[host.id] = conference.id
        return conference

    def get(self, conference_id: UUID) -> Conference | None:
        return self._conferences.get(conference_id)

    def expect(
        self,
        conference_id: UUID,
        user_id: UUID,
        *,
        joined: bool = True,
    ) -> tuple[Conference, ConferenceParticipant]:
        conference = self._conferences.get(conference_id)
        if conference is None:
            raise CallError("conference_not_found", "Conference not found", 404)
        if conference.is_ended:
            raise CallError("invalid_state", "Conference has already ended", 409)
        participant = conference.participant(user_id)
        if participant is None:
            raise CallError("forbidden", "You are not a participant of this conference", 403)
        if joined and participant.state != JOINED:
            raise CallError("not_joined", "Join the conference first", 409)
        return conference, participant

    def current_for(self, user_id: UUID) -> Conference | None:
        conference_id = self._joined.get(user_id)
        if conference_id is not None:
            conference = self._conferences.get(conference_id)
            if conference is not None and not conference.is_ended:
                return conference
        invited = [
            self._conferences[conference_id]
            for conference_id in self._invited.get(user_id, set())
            if conference_id in self._conferences and not self._conferences[conference_id].is_ended
        ]
        if not invited:
            return None
        return max(invited, key=lambda item: item.created_at)

    def joined_for(self, user_id: UUID) -> Conference | None:
        conference_id = self._joined.get(user_id)
        if conference_id is None:
            return None
        conference = self._conferences.get(conference_id)
        if conference is None or conference.is_ended:
            return None
        return conference

    def is_in_conference(self, user_id: UUID) -> bool:
        return self.joined_for(user_id) is not None

    def join(self, conference_id: UUID, user_id: UUID) -> Conference:
        conference, participant = self.expect(conference_id, user_id, joined=False)
        if participant.state == JOINED:
            return conference
        if user_id in self._joined:
            raise CallError("already_in_conference", "You are already in a conference", 409)
        participant.state = JOINED
        participant.joined_at = utcnow()
        self._joined[user_id] = conference.id
        self._forget_invite(user_id, conference.id)
        return conference

    def decline(self, conference_id: UUID, user_id: UUID) -> tuple[Conference, UserIdentity]:
        conference, participant = self.expect(conference_id, user_id, joined=False)
        if participant.state == JOINED:
            raise CallError("invalid_state", "Leave the conference instead", 409)
        conference.participants.pop(user_id, None)
        self._forget_invite(user_id, conference.id)
        return conference, participant.identity

    def invite(
        self,
        conference_id: UUID,
        inviter_id: UUID,
        invitees: list[UserIdentity],
    ) -> list[ConferenceParticipant]:
        conference, _ = self.expect(conference_id, inviter_id)
        unique: dict[UUID, UserIdentity] = {}
        for identity in invitees:
            if identity.id not in conference.participants:
                unique.setdefault(identity.id, identity)
        if not unique:
            return []
        if len(conference.participants) + len(unique) > self.max_participants:
            raise CallError(
                "conference_full",
                f"Conference is limited to {self.max_participants} participants",
                409,
            )
        added: list[ConferenceParticipant] = []
        for identity in unique.values():
            participant = ConferenceParticipant(identity=identity)
            conference.participants[identity.id] = participant
            self._invited.setdefault(identity.id, set()).add(conference.id)
            added.append(participant)
        return added

    def leave(self, conference_id: UUID, user_id: UUID) -> tuple[Conference, UserIdentity]:
        conference, participant = self.expect(conference_id, user_id)
        conference.participants.pop(user_id, None)
        self._joined.pop(user_id, None)
        self._forget_invite(user_id, conference.id)
        if conference.host_id == user_id:
            remaining = conference.joined_participants()
            if remaining:
                conference.host_id = min(
                    remaining,
                    key=lambda item: item[1].joined_at or conference.created_at,
                )[0]
        return conference, participant.identity

    def finish(self, conference_id: UUID, reason: str) -> Conference | None:
        conference = self._conferences.get(conference_id)
        if conference is None or conference.is_ended:
            return None
        conference.ended_at = utcnow()
        conference.end_reason = reason
        for user_id, participant in conference.participants.items():
            if participant.state == JOINED and self._joined.get(user_id) == conference.id:
                self._joined.pop(user_id, None)
            self._forget_invite(user_id, conference.id)
        return conference

    def release(self, conference_id: UUID) -> None:
        conference = self._conferences.pop(conference_id, None)
        if conference is None:
            return
        for user_id, participant in conference.participants.items():
            if participant.state == JOINED and self._joined.get(user_id) == conference.id:
                self._joined.pop(user_id, None)
            self._forget_invite(user_id, conference.id)

    def conferences(self) -> list[Conference]:
        return list(self._conferences.values())

    def _forget_invite(self, user_id: UUID, conference_id: UUID) -> None:
        invited = self._invited.get(user_id)
        if invited is None:
            return
        invited.discard(conference_id)
        if not invited:
            self._invited.pop(user_id, None)
