import asyncio
import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.auth import UserIdentity
from app.core.config import Settings
from app.realtime.connections import ConnectionManager
from app.realtime.events import call_event, signal_event
from app.realtime.registry import ACTIVE, ENDED, RINGING, Call, CallError, CallRegistry
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
        directory: UserDirectory,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self.connections = connections
        self.registry = registry
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
        if call is None:
            return
        existing = self._disconnect_tasks.pop(user_id, None)
        if existing is not None and existing is not asyncio.current_task():
            existing.cancel()

        if self.settings.disconnect_grace_seconds <= 0:
            await self._finish(call.id, "disconnected", "call.ended")
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
        call = self.registry.active_for(user_id)
        if call is not None:
            await self._finish(call.id, "disconnected", "call.ended")

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
