import asyncio
import logging
from collections.abc import Iterable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.auth import UserIdentity
from app.core.config import Settings
from app.realtime.conferences import JOINED, Conference, ConferenceRegistry
from app.realtime.connections import ConnectionManager
from app.realtime.events import (
    call_event,
    conference_event,
    conference_signal_event,
    signal_event,
)
from app.realtime.registry import (
    ACTIVE,
    ENDED,
    MEDIA_TYPES,
    RINGING,
    Call,
    CallError,
    CallRegistry,
)
from app.repositories.calls import CallRepository
from app.services.directory import UserDirectory

logger = logging.getLogger(__name__)

PENDING_SIGNAL_LIMIT = 200


class CallCoordinator:
    """Applies call state transitions, timers and signaling fan-out."""

    def __init__(
        self,
        connections: ConnectionManager,
        registry: CallRegistry,
        conferences: ConferenceRegistry,
        directory: UserDirectory,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self.connections = connections
        self.registry = registry
        self.conferences = conferences
        self.directory = directory
        self.settings = settings
        self.session_factory = session_factory
        self._timers: dict[UUID, asyncio.Task] = {}
        self._disconnect_tasks: dict[UUID, asyncio.Task] = {}
        self._pending_signals: dict[UUID, list[dict]] = {}
        self._persistence_tasks: set[asyncio.Task] = set()
        self._closing = False

    async def invite(self, caller: UserIdentity, callee_id: UUID, media: str) -> Call:
        if self._closing:
            raise CallError("unavailable", "Call service is shutting down", 503)
        if callee_id == caller.id:
            raise CallError("self_call", "You cannot call yourself", 400)
        if not self.connections.is_online(callee_id):
            raise CallError("user_offline", "User is offline", 409)
        if self._is_busy(caller.id):
            raise CallError("caller_busy", "You are already in a call", 409)
        if self._is_busy(callee_id):
            raise CallError("callee_busy", "User is already in a call", 409)

        callee = self.directory.snapshot(callee_id)
        call = self.registry.create(caller, callee, media)
        await self._persist_create(call)
        if call.state != RINGING:
            return call
        self._schedule_timer(call.id, self.settings.ring_timeout_seconds, "timeout")

        await self.connections.send_to_user(call.caller.id, call_event("call.ringing", call))
        await self.connections.send_to_user(call.callee.id, call_event("call.incoming", call))
        await self._presence(call.caller.id, True)
        await self._presence(call.callee.id, True)
        return call

    async def accept(self, call_id: UUID, user_id: UUID) -> Call:
        call = self.registry.accept(call_id, user_id)
        self._cancel_timer(call_id)
        await self._persist_update(call)
        if call.state != ACTIVE:
            return call
        if self.settings.max_call_duration_seconds > 0:
            self._schedule_timer(
                call.id,
                self.settings.max_call_duration_seconds,
                "max_duration",
            )
        await self.connections.send_to_user(call.caller.id, call_event("call.accepted", call))
        await self.connections.send_to_user(call.callee.id, call_event("call.accepted", call))
        return call

    async def reject(self, call_id: UUID, user_id: UUID) -> Call:
        self.registry.expect(call_id, user_id, role="callee", states=frozenset({RINGING}))
        call = await self._finish(call_id, "rejected", "call.rejected")
        assert call is not None
        return call

    async def cancel(self, call_id: UUID, user_id: UUID) -> Call:
        self.registry.expect(call_id, user_id, role="caller", states=frozenset({RINGING}))
        call = await self._finish(call_id, "cancelled", "call.cancelled")
        assert call is not None
        return call

    async def hangup(self, call_id: UUID, user_id: UUID) -> Call | None:
        self.registry.expect(call_id, user_id, states=frozenset({RINGING, ACTIVE}))
        return await self._finish(call_id, "hangup", "call.ended")

    async def create_conference(
        self,
        host: UserIdentity,
        media: str,
        title: str | None,
        invitee_ids: list[UUID],
    ) -> Conference:
        if self._closing:
            raise CallError("unavailable", "Call service is shutting down", 503)
        if media not in MEDIA_TYPES:
            raise CallError("invalid_media", "Media type must be 'audio' or 'video'", 422)
        if self._is_busy(host.id):
            raise CallError("caller_busy", "You are already in a call", 409)

        unique_ids: list[UUID] = []
        seen: set[UUID] = set()
        for user_id in invitee_ids:
            if user_id == host.id or user_id in seen:
                continue
            seen.add(user_id)
            unique_ids.append(user_id)
        if 1 + len(unique_ids) > self.settings.max_conference_participants:
            raise CallError(
                "conference_full",
                f"Conference is limited to {self.settings.max_conference_participants} "
                "participants",
                409,
            )

        invitees: list[UserIdentity] = []
        for user_id in unique_ids:
            if not self.connections.is_online(user_id):
                raise CallError("user_offline", "User is offline", 409)
            if self._is_busy(user_id):
                raise CallError("callee_busy", "User is already in a call", 409)
            invitees.append(self.directory.snapshot(user_id))

        conference = self.conferences.create(host, media, title, invitees)
        await self.connections.send_to_user(
            host.id,
            conference_event("conference.created", conference),
        )
        await self._send_event_to_users(
            [invitee.id for invitee in invitees],
            conference_event(
                "conference.invited",
                conference,
                invited_by=host.to_public(),
            ),
        )
        await self._presence(host.id, True)
        return conference

    async def join_conference(self, conference_id: UUID, user_id: UUID) -> Conference:
        if self._closing:
            raise CallError("unavailable", "Call service is shutting down", 503)
        conference, participant = self.conferences.expect(
            conference_id,
            user_id,
            joined=False,
        )
        if participant.state == JOINED:
            return conference
        if self._is_busy(user_id):
            raise CallError(
                "already_in_conference",
                "You are already in a call",
                409,
            )

        conference = self.conferences.join(conference_id, user_id)
        joined = conference.participant(user_id)
        assert joined is not None
        event = conference_event(
            "conference.joined",
            conference,
            participant=joined.to_dict(),
        )
        await self._send_event_to_users(
            [member_id for member_id, _ in conference.joined_participants()],
            event,
        )
        await self._presence(user_id, True)
        return conference

    async def decline_conference(
        self,
        conference_id: UUID,
        user_id: UUID,
    ) -> Conference:
        conference, identity = self.conferences.decline(conference_id, user_id)
        event = conference_event(
            "conference.declined",
            conference,
            participant={"user": identity.to_public(), "state": "declined"},
        )
        await self._send_event_to_users(
            [member_id for member_id, _ in conference.joined_participants()],
            event,
        )
        return conference

    async def invite_to_conference(
        self,
        conference_id: UUID,
        inviter: UserIdentity,
        invitee_ids: list[UUID],
    ) -> Conference:
        if self._closing:
            raise CallError("unavailable", "Call service is shutting down", 503)
        conference, _ = self.conferences.expect(conference_id, inviter.id)

        unique_ids: list[UUID] = []
        seen: set[UUID] = set()
        for user_id in invitee_ids:
            if (
                user_id == inviter.id
                or user_id in seen
                or conference.participant(user_id) is not None
            ):
                continue
            seen.add(user_id)
            unique_ids.append(user_id)
        if len(conference.participants) + len(unique_ids) > conference.max_participants:
            raise CallError(
                "conference_full",
                f"Conference is limited to {conference.max_participants} participants",
                409,
            )

        invitees: list[UserIdentity] = []
        for user_id in unique_ids:
            if not self.connections.is_online(user_id):
                raise CallError("user_offline", "User is offline", 409)
            if self._is_busy(user_id):
                raise CallError("callee_busy", "User is already in a call", 409)
            invitees.append(self.directory.snapshot(user_id))

        added = self.conferences.invite(conference_id, inviter.id, invitees)
        if added:
            await self._send_event_to_users(
                [participant.identity.id for participant in added],
                conference_event(
                    "conference.invited",
                    conference,
                    invited_by=inviter.to_public(),
                ),
            )
        return conference

    async def leave_conference(
        self,
        conference_id: UUID,
        user_id: UUID,
    ) -> Conference:
        conference, participant = self.conferences.expect(
            conference_id,
            user_id,
            joined=False,
        )
        if participant.state != JOINED:
            return await self.decline_conference(conference_id, user_id)

        conference, identity = self.conferences.leave(conference_id, user_id)
        event = conference_event(
            "conference.left",
            conference,
            participant={"user": identity.to_public(), "state": "left"},
        )
        await self._send_event_to_users(
            [member_id for member_id, _ in conference.joined_participants()],
            event,
        )
        await self._presence(user_id, False)

        if conference.joined_count == 0:
            ended = self.conferences.finish(conference_id, "left")
            if ended is not None:
                await self._notify_conference_ended(ended)
                self.conferences.release(conference_id)
                return ended
        return conference

    async def end_conference(self, conference_id: UUID, user_id: UUID) -> Conference:
        conference, _ = self.conferences.expect(conference_id, user_id)
        if conference.host_id != user_id:
            raise CallError("forbidden", "Only the host can end the conference", 403)
        ended = self.conferences.finish(conference_id, "ended")
        if ended is None:
            raise CallError("invalid_state", "Conference has already ended", 409)
        await self._notify_conference_ended(ended)
        self.conferences.release(conference_id)
        return ended

    async def relay_conference_signal(
        self,
        conference_id: UUID,
        from_user_id: UUID,
        to_user_id: UUID,
        event_type: str,
        payload: dict,
    ) -> None:
        conference, _ = self.conferences.expect(conference_id, from_user_id)
        if to_user_id == from_user_id:
            raise CallError("invalid_payload", "Cannot signal to yourself", 422)
        target = conference.participant(to_user_id)
        if target is None:
            raise CallError(
                "forbidden",
                "Target is not a participant of this conference",
                403,
            )
        if target.state != JOINED:
            raise CallError("forbidden", "Target is not in the conference", 403)
        event = conference_signal_event(
            event_type,
            conference_id,
            from_user_id,
            to_user_id,
            **payload,
        )
        if self.connections.is_online(to_user_id):
            await self.connections.send_to_user(to_user_id, event)

    async def notify_conference_reconnected(self, user_id: UUID) -> None:
        conference = self.conferences.joined_for(user_id)
        if conference is None:
            return
        participant = conference.participant(user_id)
        if participant is None:
            return
        await self._broadcast_conference_reconnected(conference, participant, False)

    async def resync_conference(
        self,
        conference_id: UUID,
        user_id: UUID,
        renegotiate: bool,
    ) -> None:
        conference, participant = self.conferences.expect(conference_id, user_id)
        await self._broadcast_conference_reconnected(
            conference,
            participant,
            renegotiate,
        )

    async def renegotiate_conference(
        self,
        conference_id: UUID,
        from_user_id: UUID,
        to_user_id: UUID,
    ) -> None:
        conference, _ = self.conferences.expect(conference_id, from_user_id)
        if to_user_id == from_user_id:
            raise CallError("invalid_payload", "Cannot renegotiate with yourself", 422)
        target = conference.participant(to_user_id)
        if target is None or target.state != JOINED:
            raise CallError("forbidden", "Target is not in the conference", 403)
        event = conference_signal_event(
            "conference.renegotiate",
            conference_id,
            from_user_id,
            to_user_id,
        )
        if self.connections.is_online(to_user_id):
            await self.connections.send_to_user(to_user_id, event)

    async def _broadcast_conference_reconnected(
        self,
        conference: Conference,
        participant,
        renegotiate: bool,
    ) -> None:
        event = conference_event(
            "conference.reconnected",
            conference,
            participant=participant.to_dict(),
            renegotiate=renegotiate,
        )
        await self._send_event_to_users(
            [
                member_id
                for member_id, _ in conference.joined_participants()
                if member_id != participant.identity.id
            ],
            event,
        )

    async def _notify_conference_ended(self, conference: Conference) -> None:
        event = conference_event("conference.ended", conference)
        members = list(conference.participants.items())
        await self._send_event_to_users(
            [user_id for user_id, _ in members],
            event,
        )
        await asyncio.gather(
            *(
                self._presence(user_id, False)
                for user_id, participant in members
                if participant.state == JOINED
            ),
            return_exceptions=True,
        )

    async def _send_event_to_users(
        self,
        user_ids: Iterable[UUID],
        event: dict,
    ) -> None:
        await asyncio.gather(
            *(self.connections.send_to_user(user_id, event) for user_id in user_ids),
            return_exceptions=True,
        )

    def _is_busy(self, user_id: UUID) -> bool:
        return self.registry.is_in_call(user_id) or self.conferences.is_in_conference(user_id)

    async def relay_signal(
        self,
        call_id: UUID,
        user_id: UUID,
        event_type: str,
        payload: dict,
    ) -> None:
        call = self.registry.expect(call_id, user_id, states=frozenset({ACTIVE}))
        target_id = call.partner_id(user_id)
        event = signal_event(event_type, call_id, user_id, **payload)
        if self.connections.is_online(target_id):
            await self.connections.send_to_user(target_id, event)
            return
        queue = self._pending_signals.setdefault(target_id, [])
        queue.append(event)
        if len(queue) > PENDING_SIGNAL_LIMIT:
            del queue[0]

    async def flush_pending(self, user_id: UUID) -> None:
        events = self._pending_signals.pop(user_id, [])
        for event in events:
            await self.connections.send_to_user(user_id, event)

    async def handle_connect(self, user_id: UUID) -> None:
        task = self._disconnect_tasks.pop(user_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def handle_disconnect(self, user_id: UUID) -> None:
        call = self.registry.active_for(user_id)
        conference = self.conferences.joined_for(user_id)
        if call is None and conference is None:
            return
        existing = self._disconnect_tasks.pop(user_id, None)
        if existing is not None and existing is not asyncio.current_task():
            existing.cancel()

        if self.settings.disconnect_grace_seconds <= 0:
            await self._expire_session(user_id)
            return

        task = asyncio.create_task(
            self._disconnect_after(user_id, self.settings.disconnect_grace_seconds),
            name=f"call-disconnect-{user_id}",
        )
        self._disconnect_tasks[user_id] = task

    async def _disconnect_after(self, user_id: UUID, delay: int) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        self._disconnect_tasks.pop(user_id, None)
        if self.connections.is_online(user_id):
            return
        await self._expire_session(user_id)

    async def _expire_session(self, user_id: UUID) -> None:
        call = self.registry.active_for(user_id)
        if call is not None:
            await self._finish(call.id, "disconnected", "call.ended")
            return
        conference = self.conferences.joined_for(user_id)
        if conference is not None:
            try:
                await self.leave_conference(conference.id, user_id)
            except CallError:
                logger.debug("Conference %s already changed for user %s", conference.id, user_id)

    def _schedule_timer(self, call_id: UUID, delay: int, reason: str) -> None:
        self._cancel_timer(call_id)
        task = asyncio.create_task(
            self._expire(call_id, delay, reason),
            name=f"call-{reason}-{call_id}",
        )
        self._timers[call_id] = task

    def _cancel_timer(self, call_id: UUID) -> None:
        task = self._timers.pop(call_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _expire(self, call_id: UUID, delay: int, reason: str) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if self._timers.get(call_id) is asyncio.current_task():
            self._timers.pop(call_id, None)
        call = self.registry.get(call_id)
        if call is None or call.state == ENDED:
            return
        if reason == "timeout" and call.state != RINGING:
            return
        if reason == "max_duration" and call.state != ACTIVE:
            return
        await self._finish(call_id, reason, "call.ended")

    async def _finish(self, call_id: UUID, reason: str, event_type: str) -> Call | None:
        call = self.registry.finish(call_id, reason)
        if call is None:
            return None
        self._cancel_timer(call_id)
        self.registry.release(call_id)
        self._pending_signals.pop(call.caller.id, None)
        self._pending_signals.pop(call.callee.id, None)

        self._schedule_persist_update(call)

        event = call_event(event_type, call)
        await self.connections.send_to_user(call.caller.id, event)
        await self.connections.send_to_user(call.callee.id, event)
        await self._presence(call.caller.id, False)
        await self._presence(call.callee.id, False)
        return call

    async def shutdown(self) -> None:
        self._closing = True
        tasks = [*self._timers.values(), *self._disconnect_tasks.values()]
        for task in tasks:
            task.cancel()
        self._timers.clear()
        self._disconnect_tasks.clear()

        for call in self.registry.calls():
            try:
                await self._finish(call.id, "server_shutdown", "call.ended")
            except Exception:
                logger.exception("Failed to close call %s during shutdown", call.id)

        for conference in self.conferences.conferences():
            try:
                ended = self.conferences.finish(conference.id, "server_shutdown")
                if ended is not None:
                    await self._notify_conference_ended(ended)
                self.conferences.release(conference.id)
            except Exception:
                logger.exception(
                    "Failed to close conference %s during shutdown",
                    conference.id,
                )
        await self.flush_persistence()
        self._pending_signals.clear()

    async def flush_persistence(self) -> None:
        tasks = [task for task in self._persistence_tasks if not task.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _schedule_persist_update(self, call: Call) -> None:
        task = asyncio.create_task(
            self._persist_update(call),
            name=f"persist-call-{call.id}",
        )
        self._persistence_tasks.add(task)
        task.add_done_callback(self._persistence_tasks.discard)

    async def _presence(self, user_id: UUID, in_call: bool) -> None:
        await self.connections.notify_presence(user_id, in_call=in_call)

    async def _persist_create(self, call: Call) -> None:
        try:
            async with self.session_factory() as session:
                await CallRepository(session).create(call)
        except Exception:
            logger.exception("Failed to persist call %s", call.id)

    async def _persist_update(self, call: Call) -> None:
        try:
            async with self.session_factory() as session:
                await CallRepository(session).update(call)
        except Exception:
            logger.exception("Failed to update call %s", call.id)
