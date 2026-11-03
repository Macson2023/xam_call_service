import asyncio
import logging
from dataclasses import dataclass
from uuid import UUID

import httpx
import jwt

from app.core.config import Settings

logger = logging.getLogger(__name__)

TOKEN_MAX_LENGTH = 8192


class AuthError(Exception):
    """Raised when an access token cannot be accepted."""

    def __init__(self, detail: str = "Invalid or expired token") -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True, slots=True)
class UserIdentity:
    id: UUID
    username: str | None = None
    display_name: str | None = None

    def to_public(self) -> dict:
        return {
            "id": str(self.id),
            "username": self.username,
            "display_name": self.display_name,
        }


def _claim_text(payload: dict, *names: str) -> str | None:
    for name in names:
        value = payload.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _uuid_subject(payload: dict) -> UUID:
    subject = payload.get("sub")
    if not subject:
        raise AuthError("Token has no subject")
    try:
        return UUID(str(subject))
    except (TypeError, ValueError) as exc:
        raise AuthError("Invalid token subject") from exc


def _check_token_type(payload: dict) -> None:
    token_type = payload.get("type")
    if token_type is not None and token_type != "access":
        raise AuthError("Invalid token type")


class TokenValidator:
    """Validates OAuth2 bearer tokens (JWT or RFC 7662 introspection)."""

    def __init__(self, settings: Settings, http_client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.http_client = http_client
        self._jwks_client: jwt.PyJWKClient | None = None
        if settings.oauth2_jwks_url:
            self._jwks_client = jwt.PyJWKClient(
                settings.oauth2_jwks_url,
                cache_keys=True,
                lifespan=settings.userinfo_cache_ttl_seconds,
            )

    async def validate(self, token: str) -> UserIdentity:
        if not token or len(token) > TOKEN_MAX_LENGTH:
            raise AuthError()
        if self.settings.auth_mode == "introspect":
            return await self._introspect(token)
        return await self._decode_jwt(token)

    async def _decode_jwt(self, token: str) -> UserIdentity:
        options: dict = {"verify_aud": bool(self.settings.jwt_audience)}
        try:
            if self._jwks_client is not None:
                signing_key = await asyncio.to_thread(
                    self._jwks_client.get_signing_key_from_jwt, token
                )
                key: object = signing_key.key
            else:
                key = self.settings.secret_key

            payload = jwt.decode(
                token,
                key,
                algorithms=list(self.settings.jwt_algorithms),
                audience=self.settings.jwt_audience or None,
                issuer=self.settings.jwt_issuer or None,
                options=options,
            )
        except jwt.PyJWTError as exc:
            logger.debug("JWT validation failed: %s", exc)
            raise AuthError() from exc

        _check_token_type(payload)
        return UserIdentity(
            id=_uuid_subject(payload),
            username=_claim_text(payload, "username", "preferred_username"),
            display_name=_claim_text(payload, "display_name", "name"),
        )

    async def _introspect(self, token: str) -> UserIdentity:
        if self.http_client is None:
            raise AuthError("Token introspection is not configured")

        auth = None
        if self.settings.oauth2_client_id:
            auth = (
                self.settings.oauth2_client_id,
                self.settings.oauth2_client_secret or "",
            )
        try:
            response = await self.http_client.post(
                self.settings.oauth2_introspection_url or "",
                data={"token": token},
                auth=auth,
            )
        except httpx.HTTPError as exc:
            logger.warning("Token introspection request failed: %s", exc.__class__.__name__)
            raise AuthError("Authorization service is unavailable") from exc

        if response.status_code != 200:
            raise AuthError()
        try:
            payload = response.json()
        except ValueError as exc:
            raise AuthError("Invalid introspection response") from exc
        if not isinstance(payload, dict) or not payload.get("active"):
            raise AuthError()
        _check_token_type(payload)
        return UserIdentity(
            id=_uuid_subject(payload),
            username=_claim_text(payload, "username", "preferred_username"),
            display_name=_claim_text(payload, "name", "display_name"),
        )
