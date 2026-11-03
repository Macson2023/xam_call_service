from uuid import uuid4

from fastapi.testclient import TestClient

from app.services.backend import BackendError
from tests.conftest import auth_headers, connect, make_user, receive_type


class FakeBackend:
    def __init__(self) -> None:
        self.user_id = uuid4()

    def _user(self) -> dict:
        return {
            "id": str(self.user_id),
            "username": "alice",
            "display_name": "Alice",
            "avatar_url": None,
            "created_at": "2026-01-01T00:00:00Z",
            "last_seen": None,
        }

    async def login(self, username: str, password: str) -> dict:
        if username != "alice" or password != "password123":
            raise BackendError(401, "Invalid username or password")
        return {
            "access_token": "backend-token",
            "token_type": "bearer",
            "expires_in": 3600,
            "user": self._user(),
        }

    async def register(self, username: str, password: str, display_name: str) -> dict:
        if username == "taken":
            raise BackendError(409, "Username already taken")
        return {
            "access_token": "backend-token",
            "token_type": "bearer",
            "expires_in": 3600,
            "user": {
                "id": str(uuid4()),
                "username": username,
                "display_name": display_name,
                "avatar_url": None,
                "created_at": "2026-01-01T00:00:00Z",
                "last_seen": None,
            },
        }

    async def search_users(self, token: str, query: str, limit: int = 20) -> list[dict]:
        return [
            {
                "id": str(self.user_id),
                "username": "alice",
                "display_name": "Alice",
                "avatar_url": None,
                "created_at": "2026-01-01T00:00:00Z",
                "last_seen": None,
            },
            {
                "id": str(uuid4()),
                "username": "bob",
                "display_name": "Bob",
                "avatar_url": None,
                "created_at": "2026-01-01T00:00:00Z",
                "last_seen": None,
            },
        ][:limit]


def test_login_proxy(client: TestClient) -> None:
    client.app.state.backend = FakeBackend()
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "alice", "password": "password123"},
    )
    assert response.status_code == 200
    assert response.json()["access_token"] == "backend-token"
    assert response.json()["user"]["username"] == "alice"


def test_login_proxy_passes_backend_error(client: TestClient) -> None:
    client.app.state.backend = FakeBackend()
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "alice", "password": "wrong"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid username or password"


def test_register_proxy(client: TestClient) -> None:
    client.app.state.backend = FakeBackend()
    response = client.post(
        "/api/v1/auth/register",
        json={"username": "newuser", "password": "password123", "display_name": "New"},
    )
    assert response.status_code == 201
    assert response.json()["user"]["username"] == "newuser"


def test_search_proxy_enriches_presence(client: TestClient) -> None:
    backend = FakeBackend()
    client.app.state.backend = backend
    alice = make_user(backend.user_id)

    with connect(client, alice) as websocket:
        receive_type(websocket, "ready")
        response = client.get(
            "/api/v1/users/search?q=a",
            headers=auth_headers(alice["token"]),
        )
        assert response.status_code == 200
        users = response.json()
        assert users[0]["online"] is True
        assert users[1]["online"] is False
