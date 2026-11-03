import logging
from typing import Any

import httpx

from app.core.config import Settings

logger = logging.getLogger(__name__)


class BackendError(Exception):
    """Error returned by (or while contacting) the xam_backend auth API."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _detail_from_response(response: httpx.Response, fallback: str) -> str:
    try:
        payload = response.json()
    except ValueError:
        return fallback
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str) and detail:
            return detail
    return fallback


class BackendClient:
    """Thin async client for xam_backend (OAuth2 token issuer + user directory)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.backend_base,
            timeout=settings.backend_timeout_seconds,
            headers={"User-Agent": "xam_call_service/1.0"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        json: dict | None = None,
        params: dict | None = None,
    ) -> Any:
        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = await self._client.request(
                method, path, headers=headers, json=json, params=params
            )
        except httpx.HTTPError as exc:
            logger.warning("xam_backend request failed: %s", exc.__class__.__name__)
            raise BackendError(502, "Auth service is unavailable") from exc

        if response.status_code >= 400:
            fallback = f"Auth service returned {response.status_code}"
            raise BackendError(
                response.status_code,
                _detail_from_response(response, fallback),
            )

        try:
            return response.json()
        except ValueError as exc:
            raise BackendError(502, "Auth service returned invalid JSON") from exc

    async def login(self, username: str, password: str) -> dict:
        return await self._request(
            "POST",
            "/api/v1/auth/login",
            json={"username": username, "password": password},
        )

    async def register(self, username: str, password: str, display_name: str) -> dict:
        return await self._request(
            "POST",
            "/api/v1/auth/register",
            json={
                "username": username,
                "password": password,
                "display_name": display_name,
            },
        )

    async def userinfo(self, token: str) -> dict:
        return await self._request("GET", self.settings.backend_userinfo_path, token=token)

    async def search_users(self, token: str, query: str, limit: int = 20) -> list[dict]:
        payload = await self._request(
            "GET",
            "/api/v1/users/search",
            token=token,
            params={"q": query, "limit": limit},
        )
        if not isinstance(payload, list):
            raise BackendError(502, "Auth service returned an unexpected response")
        return [item for item in payload if isinstance(item, dict)]
