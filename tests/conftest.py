import asyncio
import os
import tempfile
import time
from pathlib import Path
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi.testclient import TestClient

DB_PATH = Path(tempfile.gettempdir()) / "xam_call_service_test.db"
if DB_PATH.exists():
    DB_PATH.unlink()

os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{DB_PATH.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key-with-at-least-32-bytes"
os.environ["JWT_ALGORITHMS"] = "HS256"
os.environ["AUTH_MODE"] = "jwt"
os.environ["AUTH_ENRICH_USERINFO"] = "false"
os.environ["BACKEND_BASE_URL"] = "http://127.0.0.1:9"
os.environ["AUTO_CREATE_TABLES"] = "true"
os.environ["RING_TIMEOUT_SECONDS"] = "30"
os.environ["DISCONNECT_GRACE_SECONDS"] = "0"
os.environ["MAX_CALL_DURATION_SECONDS"] = "3600"
os.environ["CORS_ORIGINS"] = "http://localhost:5173"

from app.core.config import settings  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.main import app  # noqa: E402

SECRET = settings.secret_key


async def _reset_database() -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)


@pytest.fixture()
def client():
    asyncio.run(_reset_database())
    with TestClient(app) as test_client:
        yield test_client


def make_user(user_id: UUID | None = None) -> dict:
    user_id = user_id or uuid4()
    now = int(time.time())
    token = jwt.encode(
        {
            "sub": str(user_id),
            "type": "access",
            "iat": now,
            "exp": now + 3600,
        },
        SECRET,
        algorithm="HS256",
    )
    return {"id": user_id, "id_str": str(user_id), "token": token}


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def ws_ticket(client: TestClient, token: str) -> str:
    response = client.post("/api/v1/calls/ws-ticket", headers=auth_headers(token))
    assert response.status_code == 200, response.text
    return response.json()["ticket"]


def ws_url(ticket: str) -> str:
    return f"/ws/calls?ticket={ticket}"


def connect(client: TestClient, user: dict):
    return client.websocket_connect(ws_url(ws_ticket(client, user["token"])))


def receive_type(websocket, event_type: str) -> dict:
    event = websocket.receive_json()
    assert event["type"] == event_type, event
    return event
