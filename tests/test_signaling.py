import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tests.conftest import auth_headers, connect, make_user, receive_type


def _ready(websocket) -> dict:
    return receive_type(websocket, "ready")


def test_ping_pong(client: TestClient) -> None:
    user = make_user()
    with connect(client, user) as websocket:
        _ready(websocket)
        websocket.send_text("ping")
        assert websocket.receive_json() == {"type": "pong"}
        websocket.send_json({"type": "ping"})
        assert websocket.receive_json() == {"type": "pong"}


def test_idle_session_is_closed(client: TestClient) -> None:
    user = make_user()
    old_timeout = client.app.state.settings.ws_idle_timeout_seconds
    client.app.state.settings.ws_idle_timeout_seconds = 1
    try:
        with connect(client, user) as websocket:
            _ready(websocket)
            with pytest.raises(WebSocketDisconnect) as excinfo:
                websocket.receive_json()
            assert excinfo.value.code == 1001
    finally:
        client.app.state.settings.ws_idle_timeout_seconds = old_timeout


def test_invalid_json_returns_error(client: TestClient) -> None:
    user = make_user()
    with connect(client, user) as websocket:
        _ready(websocket)
        websocket.send_text("not-json")
        event = websocket.receive_json()
    assert event == {
        "type": "error",
        "data": {"code": "invalid_json", "detail": "Invalid JSON"},
    }


def test_unknown_event_returns_error(client: TestClient) -> None:
    user = make_user()
    with connect(client, user) as websocket:
        _ready(websocket)
        websocket.send_json({"type": "something.else"})
        event = websocket.receive_json()
    assert event["data"]["code"] == "unknown_event"


def test_full_call_flow_with_sdp_ice_and_history(client: TestClient) -> None:
    alice = make_user()
    bob = make_user()

    with connect(client, alice) as alice_ws:
        _ready(alice_ws)
        with connect(client, bob) as bob_ws:
            _ready(bob_ws)

            alice_ws.send_json(
                {
                    "type": "call.invite",
                    "data": {"to_user_id": bob["id_str"], "media": "video"},
                }
            )
            ringing = receive_type(alice_ws, "call.ringing")
            incoming = receive_type(bob_ws, "call.incoming")
            call_id = ringing["data"]["call"]["id"]
            assert incoming["data"]["call"]["caller"]["id"] == alice["id_str"]

            bob_ws.send_json({"type": "call.accept", "data": {"call_id": call_id}})
            accepted_a = receive_type(alice_ws, "call.accepted")
            accepted_b = receive_type(bob_ws, "call.accepted")
            assert accepted_a["data"]["call"]["state"] == "active"
            assert accepted_b["data"]["call"]["id"] == call_id

            alice_ws.send_json(
                {
                    "type": "webrtc.offer",
                    "data": {"call_id": call_id, "sdp": "v=0\r\no=- 1 1 IN IP4 0.0.0.0\r\n"},
                }
            )
            offer = receive_type(bob_ws, "webrtc.offer")
            assert offer["data"]["from_user_id"] == alice["id_str"]
            assert offer["data"]["sdp"].startswith("v=0")

            bob_ws.send_json(
                {
                    "type": "webrtc.answer",
                    "data": {"call_id": call_id, "sdp": "v=0\r\nanswer"},
                }
            )
            answer = receive_type(alice_ws, "webrtc.answer")
            assert answer["data"]["from_user_id"] == bob["id_str"]

            alice_ws.send_json(
                {
                    "type": "webrtc.ice",
                    "data": {
                        "call_id": call_id,
                        "candidate": {
                            "candidate": "candidate:1 1 udp 2122260223 192.168.0.2 5000 typ host",
                            "sdpMid": "0",
                            "sdpMLineIndex": 0,
                            "usernameFragment": "ufrag",
                            "injected": "ignored",
                        },
                    },
                }
            )
            ice = receive_type(bob_ws, "webrtc.ice")
            assert ice["data"]["candidate"]["candidate"].startswith("candidate:1")
            assert "injected" not in ice["data"]["candidate"]

            active = client.get("/api/v1/calls/active", headers=auth_headers(alice["token"]))
            assert active.status_code == 200
            assert active.json()["id"] == call_id

            bob_ws.send_json({"type": "call.hangup", "data": {"call_id": call_id}})
            ended_a = receive_type(alice_ws, "call.ended")
            ended_b = receive_type(bob_ws, "call.ended")
            assert ended_a["data"]["call"]["end_reason"] == "hangup"
            assert ended_b["data"]["call"]["status"] == "completed"

    history_a = client.get("/api/v1/calls/history", headers=auth_headers(alice["token"]))
    assert history_a.status_code == 200
    item = history_a.json()["items"][0]
    assert item["id"] == call_id
    assert item["direction"] == "outgoing"
    assert item["status"] == "completed"
    assert item["peer"]["id"] == bob["id_str"]

    history_b = client.get("/api/v1/calls/history", headers=auth_headers(bob["token"]))
    item_b = history_b.json()["items"][0]
    assert item_b["direction"] == "incoming"
    assert item_b["peer"]["id"] == alice["id_str"]

    detail_a = client.get(f"/api/v1/calls/{call_id}", headers=auth_headers(alice["token"]))
    assert detail_a.status_code == 200
    assert detail_a.json()["caller"]["id"] == alice["id_str"]

    stranger = make_user()
    detail_stranger = client.get(
        f"/api/v1/calls/{call_id}", headers=auth_headers(stranger["token"])
    )
    assert detail_stranger.status_code == 404

    active_after = client.get("/api/v1/calls/active", headers=auth_headers(alice["token"]))
    assert active_after.status_code == 200
    assert active_after.json() is None


def test_reject_flow(client: TestClient) -> None:
    alice = make_user()
    bob = make_user()
    with connect(client, alice) as alice_ws:
        _ready(alice_ws)
        with connect(client, bob) as bob_ws:
            _ready(bob_ws)
            alice_ws.send_json(
                {
                    "type": "call.invite",
                    "data": {"to_user_id": bob["id_str"], "media": "audio"},
                }
            )
            ringing = receive_type(alice_ws, "call.ringing")
            receive_type(bob_ws, "call.incoming")
            call_id = ringing["data"]["call"]["id"]

            bob_ws.send_json({"type": "call.reject", "data": {"call_id": call_id}})
            rejected_a = receive_type(alice_ws, "call.rejected")
            rejected_b = receive_type(bob_ws, "call.rejected")
            assert rejected_a["data"]["call"]["status"] == "rejected"
            assert rejected_b["data"]["call"]["id"] == call_id


def test_cancel_flow(client: TestClient) -> None:
    alice = make_user()
    bob = make_user()
    with connect(client, alice) as alice_ws:
        _ready(alice_ws)
        with connect(client, bob) as bob_ws:
            _ready(bob_ws)
            alice_ws.send_json(
                {
                    "type": "call.invite",
                    "data": {"to_user_id": bob["id_str"], "media": "video"},
                }
            )
            ringing = receive_type(alice_ws, "call.ringing")
            receive_type(bob_ws, "call.incoming")
            call_id = ringing["data"]["call"]["id"]

            alice_ws.send_json({"type": "call.cancel", "data": {"call_id": call_id}})
            cancelled_a = receive_type(alice_ws, "call.cancelled")
            cancelled_b = receive_type(bob_ws, "call.cancelled")
            assert cancelled_a["data"]["call"]["status"] == "cancelled"
            assert cancelled_b["data"]["call"]["id"] == call_id


def test_offline_target_is_rejected(client: TestClient) -> None:
    alice = make_user()
    with connect(client, alice) as websocket:
        _ready(websocket)
        websocket.send_json(
            {
                "type": "call.invite",
                "data": {"to_user_id": str(uuid4()), "media": "audio"},
            }
        )
        event = websocket.receive_json()
    assert event["data"]["code"] == "user_offline"


def test_self_call_is_rejected(client: TestClient) -> None:
    alice = make_user()
    with connect(client, alice) as websocket:
        _ready(websocket)
        websocket.send_json(
            {
                "type": "call.invite",
                "data": {"to_user_id": alice["id_str"], "media": "audio"},
            }
        )
        event = websocket.receive_json()
    assert event["data"]["code"] == "self_call"


def test_busy_users_are_rejected(client: TestClient) -> None:
    alice = make_user()
    bob = make_user()
    carol = make_user()
    with connect(client, alice) as alice_ws:
        _ready(alice_ws)
        with connect(client, bob) as bob_ws:
            _ready(bob_ws)
            with connect(client, carol) as carol_ws:
                _ready(carol_ws)

                alice_ws.send_json(
                    {
                        "type": "call.invite",
                        "data": {"to_user_id": bob["id_str"], "media": "video"},
                    }
                )
                receive_type(alice_ws, "call.ringing")
                receive_type(bob_ws, "call.incoming")

                carol_ws.send_json(
                    {
                        "type": "call.invite",
                        "data": {"to_user_id": bob["id_str"], "media": "audio"},
                    }
                )
                assert carol_ws.receive_json()["data"]["code"] == "callee_busy"

                carol_ws.send_json(
                    {
                        "type": "call.invite",
                        "data": {"to_user_id": alice["id_str"], "media": "audio"},
                    }
                )
                assert carol_ws.receive_json()["data"]["code"] == "callee_busy"

                alice_ws.send_json(
                    {
                        "type": "call.invite",
                        "data": {"to_user_id": carol["id_str"], "media": "audio"},
                    }
                )
                assert alice_ws.receive_json()["data"]["code"] == "caller_busy"


def test_signal_before_accept_is_rejected(client: TestClient) -> None:
    alice = make_user()
    bob = make_user()
    with connect(client, alice) as alice_ws:
        _ready(alice_ws)
        with connect(client, bob) as bob_ws:
            _ready(bob_ws)
            alice_ws.send_json(
                {
                    "type": "call.invite",
                    "data": {"to_user_id": bob["id_str"], "media": "video"},
                }
            )
            ringing = receive_type(alice_ws, "call.ringing")
            receive_type(bob_ws, "call.incoming")
            call_id = ringing["data"]["call"]["id"]

            alice_ws.send_json(
                {
                    "type": "webrtc.offer",
                    "data": {"call_id": call_id, "sdp": "v=0"},
                }
            )
            event = alice_ws.receive_json()
            assert event["data"]["code"] == "invalid_state"


def test_third_party_cannot_signal(client: TestClient) -> None:
    alice = make_user()
    bob = make_user()
    carol = make_user()
    with connect(client, alice) as alice_ws:
        _ready(alice_ws)
        with connect(client, bob) as bob_ws:
            _ready(bob_ws)
            with connect(client, carol) as carol_ws:
                _ready(carol_ws)

                alice_ws.send_json(
                    {
                        "type": "call.invite",
                        "data": {"to_user_id": bob["id_str"], "media": "video"},
                    }
                )
                receive_type(alice_ws, "call.ringing")
                incoming = receive_type(bob_ws, "call.incoming")
                call_id = incoming["data"]["call"]["id"]
                bob_ws.send_json({"type": "call.accept", "data": {"call_id": call_id}})
                receive_type(alice_ws, "call.accepted")
                receive_type(bob_ws, "call.accepted")

                carol_ws.send_json(
                    {
                        "type": "webrtc.offer",
                        "data": {"call_id": call_id, "sdp": "v=0"},
                    }
                )
                assert carol_ws.receive_json()["data"]["code"] == "forbidden"


def test_presence_watch_online_and_offline(client: TestClient) -> None:
    alice = make_user()
    bob = make_user()
    with connect(client, alice) as alice_ws:
        _ready(alice_ws)
        alice_ws.send_json({"type": "presence.watch", "data": {"user_ids": [bob["id_str"]]}})
        initial = alice_ws.receive_json()
        assert initial["type"] == "presence.state"
        assert initial["data"]["online"] is False

        with connect(client, bob) as bob_ws:
            _ready(bob_ws)
            online = alice_ws.receive_json()
            assert online["type"] == "presence.state"
            assert online["data"]["online"] is True

        offline = alice_ws.receive_json()
        assert offline["type"] == "presence.state"
        assert offline["data"]["online"] is False


def test_presence_in_call_flag(client: TestClient) -> None:
    alice = make_user()
    bob = make_user()
    with connect(client, alice) as alice_ws:
        _ready(alice_ws)
        with connect(client, bob) as bob_ws:
            _ready(bob_ws)
            alice_ws.send_json({"type": "presence.watch", "data": {"user_ids": [bob["id_str"]]}})
            assert alice_ws.receive_json()["data"]["in_call"] is False

            alice_ws.send_json(
                {
                    "type": "call.invite",
                    "data": {"to_user_id": bob["id_str"], "media": "video"},
                }
            )
            ringing = receive_type(alice_ws, "call.ringing")
            call_id = ringing["data"]["call"]["id"]
            receive_type(bob_ws, "call.incoming")
            in_call = alice_ws.receive_json()
            assert in_call["type"] == "presence.state"
            assert in_call["data"]["in_call"] is True
            assert in_call["data"]["user_id"] == bob["id_str"]

            bob_ws.send_json({"type": "call.reject", "data": {"call_id": call_id}})
            receive_type(alice_ws, "call.rejected")
            receive_type(bob_ws, "call.rejected")
            not_in_call = alice_ws.receive_json()
            assert not_in_call["type"] == "presence.state"
            assert not_in_call["data"]["in_call"] is False


def test_disconnect_ends_active_call(client: TestClient) -> None:
    alice = make_user()
    bob = make_user()
    with connect(client, alice) as alice_ws:
        _ready(alice_ws)
        with connect(client, bob) as bob_ws:
            _ready(bob_ws)
            alice_ws.send_json(
                {
                    "type": "call.invite",
                    "data": {"to_user_id": bob["id_str"], "media": "video"},
                }
            )
            ringing = receive_type(alice_ws, "call.ringing")
            incoming = receive_type(bob_ws, "call.incoming")
            call_id = incoming["data"]["call"]["id"]
            assert ringing["data"]["call"]["id"] == call_id
            bob_ws.send_json({"type": "call.accept", "data": {"call_id": call_id}})
            receive_type(alice_ws, "call.accepted")
            receive_type(bob_ws, "call.accepted")

        ended = receive_type(alice_ws, "call.ended")
        assert ended["data"]["call"]["id"] == call_id
        assert ended["data"]["call"]["end_reason"] == "disconnected"


def test_ring_timeout_marks_call_missed(client: TestClient) -> None:
    alice = make_user()
    bob = make_user()
    old_timeout = client.app.state.settings.ring_timeout_seconds
    client.app.state.settings.ring_timeout_seconds = 1
    try:
        with connect(client, alice) as alice_ws:
            _ready(alice_ws)
            with connect(client, bob) as bob_ws:
                _ready(bob_ws)
                alice_ws.send_json(
                    {
                        "type": "call.invite",
                        "data": {"to_user_id": bob["id_str"], "media": "video"},
                    }
                )
                ringing = receive_type(alice_ws, "call.ringing")
                call_id = ringing["data"]["call"]["id"]
                receive_type(bob_ws, "call.incoming")

                time.sleep(1.3)
                ended_a = receive_type(alice_ws, "call.ended")
                ended_b = receive_type(bob_ws, "call.ended")
                assert ended_a["data"]["call"]["end_reason"] == "timeout"
                assert ended_b["data"]["call"]["id"] == call_id

                history = client.get("/api/v1/calls/history", headers=auth_headers(alice["token"]))
                assert history.json()["items"][0]["status"] == "missed"
    finally:
        client.app.state.settings.ring_timeout_seconds = old_timeout


def test_call_config_requires_auth_and_lists_media(client: TestClient) -> None:
    assert client.get("/api/v1/calls/config").status_code == 401
    user = make_user()
    response = client.get("/api/v1/calls/config", headers=auth_headers(user["token"]))
    assert response.status_code == 200
    body = response.json()
    assert body["media"] == ["audio", "video"]
    assert body["ice_servers"]
    assert body["ring_timeout_seconds"] >= 1


def test_history_requires_auth(client: TestClient) -> None:
    assert client.get("/api/v1/calls/history").status_code == 401
