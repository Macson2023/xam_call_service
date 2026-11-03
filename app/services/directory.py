import logging
import time
from uuid import UUID

from app.core.auth import AuthError, TokenValidator, UserIdentity
from app.core.config import Settings
from app.services.backend import BackendClient, BackendError

logger = logging.getLogger(__name__)


def identity_from_profile(profile: dict) -> UserIdentity:
    user_id = profile.get("id")
    try:
        parsed_id = UUID(str(user_id))
    except (TypeError, ValueError) as exc:
        raise AuthError("Invalid user profile") from exc
    username = profile.get("username")
    display_name = profile.get("display_name")
    return UserIdentity(
        id=parsed_id,
        username=username if isinstance(username, str) and username else None,
        display_name=display_name if isinstance(display_name, str) and display_name else None,
    )


class UserDirectory:
    """Resolves bearer tokens into identities and caches public profiles."""

    def __init__(
        self,
        settings: Settings,
        validator: TokenValidator,
        backend: BackendClient,
    ) -> None:
        self.settings = settings
        self.validator = validator
        self.backend = backend
        self._cache: dict[UUID, tuple[float, UserIdentity]] = {}

    async def identity_for_token(self, token: str) -> UserIdentity:
        if self.settings.auth_mode == "backend":
            profile = await self._fetch_profile(token)
            identity = identity_from_profile(profile)
            self.remember(identity)
            return identity

        base = await self.validator.validate(token)
        if not self.settings.auth_enrich_userinfo:
            return base

        cached = self.cached(base.id)
        if cached is not None:
            return UserIdentity(
                id=base.id,
                username=base.username or cached.username,
                display_name=base.display_name or cached.display_name,
            )

        try:
            profile = await self._fetch_profile(token)
        except AuthError:
            raise
        except BackendError as exc:
            if exc.status_code in (401, 403):
                raise AuthError() from exc
            logger.warning("Could not enrich user %s profile: %s", base.id, exc.detail)
            return base

        identity = identity_from_profile(profile)
        self.remember(identity)
        return UserIdentity(
            id=base.id,
            username=identity.username or base.username,
            display_name=identity.display_name or base.display_name,
        )

    async def _fetch_profile(self, token: str) -> dict:
        try:
            profile = await self.backend.userinfo(token)
        except BackendError as exc:
            if exc.status_code in (401, 403):
                raise AuthError() from exc
            raise
        if not isinstance(profile, dict):
            raise AuthError("Invalid user profile")
        return profile

    def cached(self, user_id: UUID) -> UserIdentity | None:
        entry = self._cache.get(user_id)
        if entry is None:
            return None
        expires_at, identity = entry
        if expires_at <= time.monotonic():
            self._cache.pop(user_id, None)
            return None
        return identity

    def snapshot(self, user_id: UUID) -> UserIdentity:
        cached = self.cached(user_id)
        if cached is not None:
            return cached
        return UserIdentity(id=user_id)

    def remember(self, identity: UserIdentity) -> None:
        if not identity.username and not identity.display_name:
            return
        self._cache[identity.id] = (
            time.monotonic() + self.settings.userinfo_cache_ttl_seconds,
            identity,
        )
        if len(self._cache) > 10_000:
            self._prune()

    def invalidate(self, user_id: UUID) -> None:
        self._cache.pop(user_id, None)

    def _prune(self) -> None:
        now = time.monotonic()
        expired = [key for key, (expires_at, _) in self._cache.items() if expires_at <= now]
        for key in expired:
            self._cache.pop(key, None)
        if len(self._cache) > 10_000:
            self._cache.clear()
