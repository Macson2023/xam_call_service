import secrets
import time

from app.core.auth import UserIdentity


class WsTicketStore:
    """Short-lived, single-use tickets used to authenticate WebSocket upgrades.

    Keeping the OAuth2 access token out of the WebSocket URL avoids leaking it
    into access logs and proxy history. Tickets live in process memory; a
    multi-worker deployment should replace this with a shared store (Redis).
    """

    def __init__(self, ttl_seconds: int = 30) -> None:
        self.ttl_seconds = ttl_seconds
        self._tickets: dict[str, tuple[float, UserIdentity]] = {}

    def issue(self, identity: UserIdentity) -> tuple[str, int]:
        self._prune()
        ticket = secrets.token_urlsafe(32)
        self._tickets[ticket] = (time.monotonic() + self.ttl_seconds, identity)
        return ticket, self.ttl_seconds

    def consume(self, ticket: str) -> UserIdentity | None:
        if not ticket:
            return None
        self._prune()
        entry = self._tickets.pop(ticket, None)
        if entry is None:
            return None
        expires_at, identity = entry
        if expires_at <= time.monotonic():
            return None
        return identity

    def _prune(self) -> None:
        now = time.monotonic()
        expired = [key for key, (expires_at, _) in self._tickets.items() if expires_at <= now]
        for key in expired:
            self._tickets.pop(key, None)
        if len(self._tickets) > 50_000:
            self._tickets.clear()
