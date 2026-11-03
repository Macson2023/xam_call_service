import time
from uuid import uuid4

import jwt
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.auth import AuthError, TokenValidator
from app.core.config import settings
from tests.conftest import SECRET, auth_headers, connect, make_user, ws_ticket, ws_url


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_me_requires_token(client: TestClient) -> None:
    assert client.get("/api/v1/users/me").status_code == 401
    assert (
        client.get("/api/v1/users/me", headers={"Authorization": "Bearer bad"}).status_code == 401
    )


def test_me_returns_identity(client: TestClient) -> None:
    user = make_user()
    response = client.get("/api/v1/users/me", headers=auth_headers(user["token"]))
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == user["id_str"]
    assert body["online"] is False
    assert body["in_call"] is False


def test_websocket_requires_ticket_or_token(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/calls"):
            pass
    assert exc_info.value.code == 1008


def test_websocket_rejects_invalid_ticket(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(ws_url("unknown-ticket")):
            pass
    assert exc_info.value.code == 1008


def test_websocket_accepts_token_fallback(client: TestClient) -> None:
    user = make_user()
    with client.websocket_connect(f"/ws/calls?token={user['token']}") as websocket:
        event = websocket.receive_json()
    assert event["type"] == "ready"
    assert event["data"]["user"]["id"] == user["id_str"]


def test_ws_ticket_is_single_use(client: TestClient) -> None:
    user = make_user()
    ticket = ws_ticket(client, user["token"])
    with client.websocket_connect(ws_url(ticket)) as websocket:
        assert websocket.receive_json()["type"] == "ready"

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(ws_url(ticket)):
            pass
    assert exc_info.value.code == 1008


def test_ws_ticket_requires_auth(client: TestClient) -> None:
    assert client.post("/api/v1/calls/ws-ticket").status_code == 401


async def test_validator_accepts_valid_token() -> None:
    user = make_user()
    identity = await TokenValidator(settings).validate(user["token"])
    assert identity.id == user["id"]


async def test_validator_rejects_expired_token() -> None:
    user_id = uuid4()
    now = int(time.time())
    token = jwt.encode(
        {"sub": str(user_id), "type": "access", "iat": now - 100, "exp": now - 10},
        SECRET,
        algorithm="HS256",
    )
    with pytest.raises(AuthError):
        await TokenValidator(settings).validate(token)


async def test_validator_rejects_wrong_signature() -> None:
    user_id = uuid4()
    token = jwt.encode(
        {"sub": str(user_id), "type": "access", "exp": int(time.time()) + 3600},
        "another-secret-key-with-32-bytes!!",
        algorithm="HS256",
    )
    with pytest.raises(AuthError):
        await TokenValidator(settings).validate(token)


async def test_validator_rejects_non_access_type() -> None:
    user_id = uuid4()
    token = jwt.encode(
        {"sub": str(user_id), "type": "refresh", "exp": int(time.time()) + 3600},
        SECRET,
        algorithm="HS256",
    )
    with pytest.raises(AuthError):
        await TokenValidator(settings).validate(token)


async def test_validator_rejects_non_uuid_subject() -> None:
    token = jwt.encode(
        {"sub": "not-a-uuid", "type": "access", "exp": int(time.time()) + 3600},
        SECRET,
        algorithm="HS256",
    )
    with pytest.raises(AuthError):
        await TokenValidator(settings).validate(token)


def test_ticket_connect_helper_works(client: TestClient) -> None:
    user = make_user()
    with connect(client, user) as websocket:
        event = websocket.receive_json()
    assert event["type"] == "ready"
