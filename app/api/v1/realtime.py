import asyncio
import json
import logging
import time
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from app.core.auth import AuthError, UserIdentity
from app.realtime.conferences import ConferenceRegistry
from app.realtime.connections import ConnectionManager
from app.realtime.coordinator import CallCoordinator
from app.realtime.events import error_event, presence_state, ready_event
from app.realtime.registry import CallError, CallRegistry
from app.realtime.tickets import WsTicketStore
from app.services.backend import BackendError
from app.services.directory import UserDirectory

logger = logging.getLogger(__name__)

router = APIRouter(tags=["realtime"])

MAX_SDP_LENGTH = 64_000
MAX_CANDIDATE_LENGTH = 4_096
MAX_PRESENCE_IDS = 100
MAX_CONFERENCE_INVITEES = 32
MAX_CONFERENCE_TITLE = 120


class RateLimiter:
    def __init__(self, capacity: int = 180, refill_per_second: float = 45.0) -> None:
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self.tokens = float(capacity)
        self.updated_at = time.monotonic()

    def allow(self) -> bool:
        now = time.monotonic()
        self.tokens = min(
            self.capacity,
            self.tokens + (now - self.updated_at) * self.refill_per_second,
        )
        self.updated_at = now
        if self.tokens < 1:
            return False
        self.tokens -= 1
        return True


def _parse_uuid(data: dict, key: str) -> UUID:
    try:
        return UUID(str(data.get(key)))
    except (TypeError, ValueError) as exc:
        raise CallError("invalid_payload", f"Invalid {key}", 422) from exc


def _parse_user_id_list(data: dict, key: str) -> list[UUID]:
    values = data.get(key)
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > MAX_CONFERENCE_INVITEES:
        raise CallError("invalid_payload", f"{key} must be a list", 422)
    ids: list[UUID] = []
    seen: set[UUID] = set()
    for value in values:
        try:
            parsed = UUID(str(value))
        except (TypeError, ValueError) as exc:
            raise CallError("invalid_payload", "Invalid user id", 422) from exc
        if parsed not in seen:
            seen.add(parsed)
            ids.append(parsed)
    return ids


def _parse_title(data: dict) -> str | None:
    title = data.get("title")
    if title is None:
        return None
    if not isinstance(title, str):
        raise CallError("invalid_payload", "Invalid conference title", 422)
    title = title.strip()
    if not title:
        return None
    if len(title) > MAX_CONFERENCE_TITLE:
        raise CallError("invalid_payload", "Conference title is too long", 422)
    return title


def _sanitize_candidate(value: object) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise CallError("invalid_payload", "Invalid ICE candidate", 422)

    raw = value.get("candidate")
    if not isinstance(raw, str) or not raw or len(raw) > MAX_CANDIDATE_LENGTH:
        raise CallError("invalid_payload", "Invalid ICE candidate", 422)

    sdp_mid = value.get("sdpMid")
    if sdp_mid is not None and (not isinstance(sdp_mid, str) or len(sdp_mid) > 64):
        raise CallError("invalid_payload", "Invalid sdpMid", 422)

    sdp_mline_index = value.get("sdpMLineIndex")
    if sdp_mline_index is not None and (
        isinstance(sdp_mline_index, bool)
        or not isinstance(sdp_mline_index, int)
        or sdp_mline_index < 0
        or sdp_mline_index > 1024
    ):
        raise CallError("invalid_payload", "Invalid sdpMLineIndex", 422)

    username_fragment = value.get("usernameFragment")
    if username_fragment is not None and (
        not isinstance(username_fragment, str) or len(username_fragment) > 256
    ):
        raise CallError("invalid_payload", "Invalid usernameFragment", 422)

    return {
        "candidate": raw,
        "sdpMid": sdp_mid,
        "sdpMLineIndex": sdp_mline_index,
        "usernameFragment": username_fragment,
    }


async def _send_error(websocket: WebSocket, code: str, detail: str) -> None:
    await websocket.send_json(error_event(code, detail))


async def _handle_event(
    websocket: WebSocket,
    identity: UserIdentity,
    raw: str,
    *,
    connections: ConnectionManager,
    registry: CallRegistry,
    conferences: ConferenceRegistry,
    coordinator: CallCoordinator,
    limiter: RateLimiter,
) -> None:
    raw = raw.strip()
    if raw.lower() == "ping":
        await websocket.send_json({"type": "pong"})
        return

    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        await _send_error(websocket, "invalid_json", "Invalid JSON")
        return

    if not isinstance(event, dict):
        await _send_error(websocket, "invalid_event", "Event must be a JSON object")
        return

    event_type = event.get("type")
    if event_type == "ping":
        await websocket.send_json({"type": "pong"})
        return

    if not limiter.allow():
        await _send_error(websocket, "rate_limited", "Too many events")
        return

    data = event.get("data") or {}
    if not isinstance(data, dict):
        await _send_error(websocket, "invalid_event", "Event data must be a JSON object")
        return

    try:
        if event_type == "presence.watch":
            ids = _parse_presence_ids(data)
            await connections.watch(websocket, ids)
            for user_id in ids:
                await websocket.send_json(
                    presence_state(
                        user_id,
                        online=connections.is_online(user_id),
                        in_call=(
                            registry.is_in_call(user_id) or conferences.is_in_conference(user_id)
                        ),
                    )
                )
            return

        if event_type == "presence.unwatch":
            await connections.unwatch(websocket, _parse_presence_ids(data))
            return

        if event_type == "call.invite":
            to_user_id = _parse_uuid(data, "to_user_id")
            media = data.get("media")
            if not isinstance(media, str):
                raise CallError("invalid_payload", "Invalid media", 422)
            await coordinator.invite(identity, to_user_id, media)
            return

        if event_type == "call.accept":
            await coordinator.accept(_parse_uuid(data, "call_id"), identity.id)
            return

        if event_type == "call.reject":
            await coordinator.reject(_parse_uuid(data, "call_id"), identity.id)
            return

        if event_type == "call.cancel":
            await coordinator.cancel(_parse_uuid(data, "call_id"), identity.id)
            return

        if event_type == "call.hangup":
            await coordinator.hangup(_parse_uuid(data, "call_id"), identity.id)
            return

        if event_type == "conference.create":
            media = data.get("media")
            if not isinstance(media, str):
                raise CallError("invalid_payload", "Invalid media", 422)
            await coordinator.create_conference(
                identity,
                media,
                _parse_title(data),
                _parse_user_id_list(data, "invitee_ids"),
            )
            return

        if event_type == "conference.join":
            await coordinator.join_conference(
                _parse_uuid(data, "conference_id"),
                identity.id,
            )
            return

        if event_type == "conference.decline":
            await coordinator.decline_conference(
                _parse_uuid(data, "conference_id"),
                identity.id,
            )
            return

        if event_type == "conference.leave":
            await coordinator.leave_conference(
                _parse_uuid(data, "conference_id"),
                identity.id,
            )
            return

        if event_type == "conference.end":
            await coordinator.end_conference(
                _parse_uuid(data, "conference_id"),
                identity.id,
            )
            return

        if event_type == "conference.invite":
            await coordinator.invite_to_conference(
                _parse_uuid(data, "conference_id"),
                identity,
                _parse_user_id_list(data, "user_ids"),
            )
            return

        if event_type == "conference.resync":
            renegotiate = data.get("renegotiate", False)
            if not isinstance(renegotiate, bool):
                raise CallError("invalid_payload", "Invalid renegotiate flag", 422)
            await coordinator.resync_conference(
                _parse_uuid(data, "conference_id"),
                identity.id,
                renegotiate,
            )
            return

        if event_type == "conference.renegotiate":
            await coordinator.renegotiate_conference(
                _parse_uuid(data, "conference_id"),
                identity.id,
                _parse_uuid(data, "to_user_id"),
            )
            return

        if event_type in {"webrtc.offer", "webrtc.answer"}:
            sdp = data.get("sdp")
            if not isinstance(sdp, str) or not sdp.strip() or len(sdp) > MAX_SDP_LENGTH:
                raise CallError("invalid_payload", "Invalid SDP", 422)
            if data.get("conference_id") is not None:
                await coordinator.relay_conference_signal(
                    _parse_uuid(data, "conference_id"),
                    identity.id,
                    _parse_uuid(data, "to_user_id"),
                    event_type,
                    {"sdp": sdp},
                )
            else:
                await coordinator.relay_signal(
                    _parse_uuid(data, "call_id"),
                    identity.id,
                    event_type,
                    {"sdp": sdp},
                )
            return

        if event_type == "webrtc.ice":
            candidate = _sanitize_candidate(data.get("candidate"))
            if data.get("conference_id") is not None:
                await coordinator.relay_conference_signal(
                    _parse_uuid(data, "conference_id"),
                    identity.id,
                    _parse_uuid(data, "to_user_id"),
                    event_type,
                    {"candidate": candidate},
                )
            else:
                await coordinator.relay_signal(
                    _parse_uuid(data, "call_id"),
                    identity.id,
                    event_type,
                    {"candidate": candidate},
                )
            return

        await _send_error(websocket, "unknown_event", "Unknown event type")
    except CallError as exc:
        await _send_error(websocket, exc.code, exc.detail)
    except Exception:
        logger.exception("Failed to handle signaling event %r", event_type)
        await _send_error(websocket, "internal_error", "Internal signaling error")


def _parse_presence_ids(data: dict) -> set[UUID]:
    values = data.get("user_ids")
    if not isinstance(values, list) or len(values) > MAX_PRESENCE_IDS:
        raise CallError("invalid_payload", "user_ids must be a list", 422)
    ids: set[UUID] = set()
    for value in values:
        try:
            ids.add(UUID(str(value)))
        except (TypeError, ValueError) as exc:
            raise CallError("invalid_payload", "Invalid user id", 422) from exc
    return ids


async def _authenticate_ws(
    websocket: WebSocket,
    tickets: WsTicketStore,
    directory: UserDirectory,
) -> UserIdentity | None:
    ticket = websocket.query_params.get("ticket")
    if ticket is not None:
        identity = tickets.consume(ticket)
        if identity is None:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return None
        return identity

    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return None
    try:
        return await directory.identity_for_token(token)
    except AuthError:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return None
    except BackendError:
        await websocket.close(code=1011)
        return None


async def _finalize_session(
    identity: UserIdentity,
    websocket: WebSocket,
    connections: ConnectionManager,
    registry: CallRegistry,
    conferences: ConferenceRegistry,
    coordinator: CallCoordinator,
) -> None:
    def in_call() -> bool:
        return registry.is_in_call(identity.id) or conferences.is_in_conference(identity.id)

    try:
        last_session = await connections.disconnect(identity.id, websocket)
        if not last_session:
            return
        had_call = in_call()
        await coordinator.handle_disconnect(identity.id)
        if not had_call or in_call():
            await connections.notify_presence(identity.id, in_call=in_call())
    except Exception:
        logger.exception("Failed to finalize signaling session for user %s", identity.id)


@router.websocket("/ws/calls")
async def calls_websocket(websocket: WebSocket) -> None:
    state = websocket.app.state
    identity = await _authenticate_ws(websocket, state.tickets, state.directory)
    if identity is None:
        return

    connections: ConnectionManager = state.connections
    registry: CallRegistry = state.registry
    conferences: ConferenceRegistry = state.conferences
    coordinator: CallCoordinator = state.coordinator

    await websocket.accept()
    first_session = await connections.connect(identity.id, websocket)
    await coordinator.handle_connect(identity.id)

    active = registry.active_for(identity.id)
    conference = conferences.current_for(identity.id)
    await websocket.send_json(ready_event(identity, active, conference, state.settings))

    if first_session:
        await coordinator.notify_conference_reconnected(identity.id)
        await connections.notify_presence(
            identity.id,
            in_call=(registry.is_in_call(identity.id) or conferences.is_in_conference(identity.id)),
        )
    await coordinator.flush_pending(identity.id)

    limiter = RateLimiter()
    idle_timeout = state.settings.ws_idle_timeout_seconds
    try:
        while True:
            try:
                if idle_timeout > 0:
                    raw = await asyncio.wait_for(
                        websocket.receive_text(),
                        timeout=idle_timeout,
                    )
                else:
                    raw = await websocket.receive_text()
            except TimeoutError:
                logger.info("Closing idle signaling session for user %s", identity.id)
                try:
                    await websocket.close(code=status.WS_1001_GOING_AWAY)
                except Exception:
                    pass
                break
            if len(raw.encode("utf-8")) > state.settings.max_ws_message_bytes:
                await _send_error(websocket, "payload_too_large", "Message is too large")
                continue
            await _handle_event(
                websocket,
                identity,
                raw,
                connections=connections,
                registry=registry,
                conferences=conferences,
                coordinator=coordinator,
                limiter=limiter,
            )
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("Signaling session failed for user %s", identity.id)
    finally:
        finalizer = asyncio.create_task(
            _finalize_session(
                identity,
                websocket,
                connections,
                registry,
                conferences,
                coordinator,
            ),
            name=f"finalize-ws-{identity.id}",
        )
        try:
            await asyncio.shield(finalizer)
        except asyncio.CancelledError:
            pass
