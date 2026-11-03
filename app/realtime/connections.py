import asyncio
import logging
from uuid import UUID

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    """In-memory registry of signaling WebSocket sessions and presence watchers."""

    def __init__(self) -> None:
        self._connections: dict[UUID, set[WebSocket]] = {}
        self._watchers: dict[UUID, set[WebSocket]] = {}
        self._watch_by_socket: dict[WebSocket, set[UUID]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, user_id: UUID, websocket: WebSocket) -> bool:
        async with self._lock:
            connections = self._connections.setdefault(user_id, set())
            connections.add(websocket)
            return len(connections) == 1

    async def disconnect(self, user_id: UUID, websocket: WebSocket) -> bool:
        async with self._lock:
            connections = self._connections.get(user_id)
            if connections is not None:
                connections.discard(websocket)
                if not connections:
                    self._connections.pop(user_id, None)

            watched = self._watch_by_socket.pop(websocket, set())
            for target_id in watched:
                watchers = self._watchers.get(target_id)
                if watchers is None:
                    continue
                watchers.discard(websocket)
                if not watchers:
                    self._watchers.pop(target_id, None)

            return user_id not in self._connections

    def is_online(self, user_id: UUID) -> bool:
        return user_id in self._connections

    def online_count(self, user_id: UUID) -> int:
        return len(self._connections.get(user_id, ()))

    async def send_to_user(self, user_id: UUID, payload: dict) -> None:
        connections = list(self._connections.get(user_id, ()))
        for websocket in connections:
            try:
                await websocket.send_json(payload)
            except Exception:
                logger.debug("Dropping dead signaling socket for user %s", user_id)
                await self.disconnect(user_id, websocket)

    async def watch(self, websocket: WebSocket, user_ids: set[UUID]) -> None:
        async with self._lock:
            watched = self._watch_by_socket.setdefault(websocket, set())
            for target_id in user_ids:
                watched.add(target_id)
                self._watchers.setdefault(target_id, set()).add(websocket)

    async def unwatch(self, websocket: WebSocket, user_ids: set[UUID]) -> None:
        async with self._lock:
            watched = self._watch_by_socket.get(websocket)
            if watched is None:
                return
            for target_id in user_ids:
                watched.discard(target_id)
                watchers = self._watchers.get(target_id)
                if watchers is not None:
                    watchers.discard(websocket)
                    if not watchers:
                        self._watchers.pop(target_id, None)
            if not watched:
                self._watch_by_socket.pop(websocket, None)

    async def notify_presence(self, user_id: UUID, *, in_call: bool) -> None:
        watchers = list(self._watchers.get(user_id, ()))
        if not watchers:
            return
        payload = {
            "type": "presence.state",
            "data": {
                "user_id": str(user_id),
                "online": self.is_online(user_id),
                "in_call": in_call,
            },
        }
        for websocket in watchers:
            try:
                await websocket.send_json(payload)
            except Exception:
                logger.debug("Dropping dead presence watcher for user %s", user_id)
                async with self._lock:
                    targets = self._watch_by_socket.pop(websocket, set())
                    for target_id in targets:
                        target_watchers = self._watchers.get(target_id)
                        if target_watchers is not None:
                            target_watchers.discard(websocket)
                            if not target_watchers:
                                self._watchers.pop(target_id, None)
