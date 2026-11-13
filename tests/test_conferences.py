from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from tests.conftest import auth_headers, connect, make_user, receive_type


def _joined(ws, count: int) -> list[dict]:
    return [receive_type(ws, "conference.joined") for _ in range(count)]


def test_conference_create_join_signal_leave_and_history(client: TestClient) -> None:
    alice = make_user()
    bob = make_user()
    carol = make_user()

    with connect(client, alice) as alice_ws:
        receive_type(alice_ws, "ready")
        with connect(client, bob) as bob_ws:
            receive_type(bob_ws, "ready")
            with connect(client, carol) as carol_ws:
                receive_type(carol_ws, "ready")

                alice_ws.send_json(
                    {
                        "type": "conference.create",
                        "data": {
                            "media": "video",
                            "title": "  Team sync  ",
                            "invitee_ids": [bob["id_str"], carol["id_str"]],
                        },
                    }
                )
                created = receive_type(alice_ws, "conference.created")
                conference_id = created["data"]["conference"]["id"]
                assert created["data"]["conference"]["title"] == "Team sync"
                assert created["data"]["conference"]["media"] == "video"
                assert created["data"]["conference"]["host_id"] == alice["id_str"]

                invited_bob = receive_type(bob_ws, "conference.invited")
                invited_carol = receive_type(carol_ws, "conference.invited")
                assert invited_bob["data"]["conference"]["id"] == conference_id
                assert invited_carol["data"]["invited_by"]["id"] == alice["id_str"]

                bob_ws.send_json(
                    {
                        "type": "conference.join",
                        "data": {"conference_id": conference_id},
                    }
                )
                joined_a = receive_type(alice_ws, "conference.joined")
                joined_b = receive_type(bob_ws, "conference.joined")
                assert joined_b["data"]["participant"]["user"]["id"] == bob["id_str"]
                assert joined_b["data"]["conference"]["joined_ids"] == [
                    alice["id_str"],
                    bob["id_str"],
                ]
                assert joined_a["data"]["conference"]["id"] == conference_id

                carol_ws.send_json(
                    {
                        "type": "conference.join",
                        "data": {"conference_id": conference_id},
                    }
                )
                _joined(alice_ws, 1)
                _joined(bob_ws, 1)
                carol_joined = receive_type(carol_ws, "conference.joined")
                assert set(carol_joined["data"]["conference"]["joined_ids"]) == {
                    alice["id_str"],
                    bob["id_str"],
                    carol["id_str"],
                }

                bob_ws.send_json(
                    {
                        "type": "webrtc.offer",
                        "data": {
                            "conference_id": conference_id,
                            "to_user_id": alice["id_str"],
                            "sdp": "v=0\r\noffer",
                        },
                    }
                )
                offer = receive_type(alice_ws, "webrtc.offer")
                assert offer["data"]["from_user_id"] == bob["id_str"]
                assert offer["data"]["to_user_id"] == alice["id_str"]
                assert offer["data"]["conference_id"] == conference_id
                assert "call_id" not in offer["data"]

                carol_ws.send_text("ping")
                assert carol_ws.receive_json() == {"type": "pong"}

                alice_ws.send_json(
                    {
                        "type": "webrtc.answer",
                        "data": {
                            "conference_id": conference_id,
                            "to_user_id": bob["id_str"],
                            "sdp": "v=0\r\nanswer",
                        },
                    }
                )
                answer = receive_type(bob_ws, "webrtc.answer")
                assert answer["data"]["from_user_id"] == alice["id_str"]

                alice_ws.send_json(
                    {
                        "type": "webrtc.ice",
                        "data": {
                            "conference_id": conference_id,
                            "to_user_id": bob["id_str"],
                            "candidate": {
                                "candidate": "candidate:1 1 udp 1 192.168.0.2 5000 typ host",
                                "sdpMid": "0",
                                "sdpMLineIndex": 0,
                            },
                        },
                    }
                )
                ice = receive_type(bob_ws, "webrtc.ice")
                assert ice["data"]["candidate"]["candidate"].startswith("candidate:1")

                active = client.get(
                    "/api/v1/calls/conference",
                    headers=auth_headers(bob["token"]),
                )
                assert active.status_code == 200
                assert active.json()["id"] == conference_id
                assert len(active.json()["joined_ids"]) == 3

                me = client.get("/api/v1/users/me", headers=auth_headers(bob["token"]))
                assert me.json()["in_call"] is True

                bob_ws.send_json(
                    {
                        "type": "conference.leave",
                        "data": {"conference_id": conference_id},
                    }
                )
                left_a = receive_type(alice_ws, "conference.left")
                left_c = receive_type(carol_ws, "conference.left")
                assert left_a["data"]["participant"]["user"]["id"] == bob["id_str"]
                assert left_c["data"]["conference"]["id"] == conference_id
                assert bob["id_str"] not in left_a["data"]["conference"]["joined_ids"]

                carol_ws.send_json(
                    {
                        "type": "conference.leave",
                        "data": {"conference_id": conference_id},
                    }
                )
                receive_type(alice_ws, "conference.left")

                alice_ws.send_json(
                    {
                        "type": "conference.end",
                        "data": {"conference_id": conference_id},
                    }
                )
                ended = receive_type(alice_ws, "conference.ended")
                assert ended["data"]["conference"]["end_reason"] == "ended"

                final = client.get(
                    "/api/v1/calls/conference",
                    headers=auth_headers(alice["token"]),
                )
                assert final.json() is None


def test_conference_host_end_notifies_participants(client: TestClient) -> None:
    alice = make_user()
    bob = make_user()
    with connect(client, alice) as alice_ws:
        receive_type(alice_ws, "ready")
        with connect(client, bob) as bob_ws:
            receive_type(bob_ws, "ready")
            alice_ws.send_json(
                {
                    "type": "conference.create",
                    "data": {"media": "audio", "invitee_ids": [bob["id_str"]]},
                }
            )
            created = receive_type(alice_ws, "conference.created")
            conference_id = created["data"]["conference"]["id"]
            receive_type(bob_ws, "conference.invited")
            bob_ws.send_json({"type": "conference.join", "data": {"conference_id": conference_id}})
            receive_type(alice_ws, "conference.joined")
            receive_type(bob_ws, "conference.joined")

            bob_ws.send_json({"type": "conference.end", "data": {"conference_id": conference_id}})
            error = receive_type(bob_ws, "error")
            assert error["data"]["code"] == "forbidden"

            alice_ws.send_json({"type": "conference.end", "data": {"conference_id": conference_id}})
            ended_a = receive_type(alice_ws, "conference.ended")
            ended_b = receive_type(bob_ws, "conference.ended")
            assert ended_a["data"]["conference"]["id"] == conference_id
            assert ended_b["data"]["conference"]["end_reason"] == "ended"


def test_conference_decline_and_disconnect(client: TestClient) -> None:
    alice = make_user()
    bob = make_user()
    carol = make_user()
    with connect(client, alice) as alice_ws:
        receive_type(alice_ws, "ready")
        with connect(client, bob) as bob_ws:
            receive_type(bob_ws, "ready")
            with connect(client, carol) as carol_ws:
                receive_type(carol_ws, "ready")
                alice_ws.send_json(
                    {
                        "type": "conference.create",
                        "data": {
                            "media": "video",
                            "invitee_ids": [bob["id_str"], carol["id_str"]],
                        },
                    }
                )
                conference_id = receive_type(alice_ws, "conference.created")["data"]["conference"][
                    "id"
                ]
                receive_type(bob_ws, "conference.invited")
                receive_type(carol_ws, "conference.invited")

                bob_ws.send_json(
                    {"type": "conference.decline", "data": {"conference_id": conference_id}}
                )
                declined = receive_type(alice_ws, "conference.declined")
                assert declined["data"]["participant"]["user"]["id"] == bob["id_str"]

                carol_ws.send_json(
                    {"type": "conference.join", "data": {"conference_id": conference_id}}
                )
                receive_type(alice_ws, "conference.joined")
                receive_type(carol_ws, "conference.joined")

            left = receive_type(alice_ws, "conference.left")
            assert left["data"]["participant"]["user"]["id"] == carol["id_str"]
            assert left["data"]["conference"]["end_reason"] is None


def test_conference_requires_room_capacity_and_free_users(client: TestClient) -> None:
    alice = make_user()
    bob = make_user()
    carol = make_user()
    with connect(client, alice) as alice_ws:
        receive_type(alice_ws, "ready")
        with connect(client, bob) as bob_ws:
            receive_type(bob_ws, "ready")
            with connect(client, carol) as carol_ws:
                receive_type(carol_ws, "ready")

                alice_ws.send_json(
                    {
                        "type": "call.invite",
                        "data": {"to_user_id": bob["id_str"], "media": "audio"},
                    }
                )
                receive_type(alice_ws, "call.ringing")
                receive_type(bob_ws, "call.incoming")

                carol_ws.send_json(
                    {
                        "type": "conference.create",
                        "data": {"media": "video", "invitee_ids": [bob["id_str"]]},
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

                call_id = client.get(
                    "/api/v1/calls/active", headers=auth_headers(alice["token"])
                ).json()["id"]
                alice_ws.send_json({"type": "call.cancel", "data": {"call_id": call_id}})
                receive_type(alice_ws, "call.cancelled")
                receive_type(bob_ws, "call.cancelled")

                carol_ws.send_json(
                    {
                        "type": "conference.create",
                        "data": {
                            "media": "video",
                            "invitee_ids": [alice["id_str"], bob["id_str"]],
                        },
                    }
                )
                created = receive_type(carol_ws, "conference.created")
                conference_id = created["data"]["conference"]["id"]
                receive_type(alice_ws, "conference.invited")
                receive_type(bob_ws, "conference.invited")

                alice_ws.send_json(
                    {"type": "conference.join", "data": {"conference_id": conference_id}}
                )
                receive_type(alice_ws, "conference.joined")
                receive_type(carol_ws, "conference.joined")

                alice_ws.send_json(
                    {
                        "type": "call.invite",
                        "data": {"to_user_id": bob["id_str"], "media": "audio"},
                    }
                )
                assert alice_ws.receive_json()["data"]["code"] == "caller_busy"

                registry = client.app.state.conferences
                conference = registry.get(UUID(conference_id))
                assert conference is not None
                old_max = conference.max_participants
                conference.max_participants = 3
                try:
                    carol_ws.send_json(
                        {
                            "type": "conference.invite",
                            "data": {
                                "conference_id": conference_id,
                                "user_ids": [make_user()["id_str"]],
                            },
                        }
                    )
                    # The room already has carol, alice and bob, i.e. at the limit.
                    assert carol_ws.receive_json()["data"]["code"] == "conference_full"
                finally:
                    conference.max_participants = old_max


def test_conference_signal_rejects_self_and_outsiders(client: TestClient) -> None:
    alice = make_user()
    bob = make_user()
    stranger = make_user()
    with connect(client, alice) as alice_ws:
        receive_type(alice_ws, "ready")
        with connect(client, bob) as bob_ws:
            receive_type(bob_ws, "ready")
            with connect(client, stranger) as stranger_ws:
                receive_type(stranger_ws, "ready")
                alice_ws.send_json(
                    {
                        "type": "conference.create",
                        "data": {"media": "video", "invitee_ids": [bob["id_str"]]},
                    }
                )
                conference_id = receive_type(alice_ws, "conference.created")["data"]["conference"][
                    "id"
                ]
                receive_type(bob_ws, "conference.invited")
                bob_ws.send_json(
                    {"type": "conference.join", "data": {"conference_id": conference_id}}
                )
                receive_type(alice_ws, "conference.joined")
                receive_type(bob_ws, "conference.joined")

                alice_ws.send_json(
                    {
                        "type": "webrtc.offer",
                        "data": {
                            "conference_id": conference_id,
                            "to_user_id": alice["id_str"],
                            "sdp": "v=0",
                        },
                    }
                )
                assert alice_ws.receive_json()["data"]["code"] == "invalid_payload"

                alice_ws.send_json(
                    {
                        "type": "webrtc.offer",
                        "data": {
                            "conference_id": conference_id,
                            "to_user_id": stranger["id_str"],
                            "sdp": "v=0",
                        },
                    }
                )
                assert alice_ws.receive_json()["data"]["code"] == "forbidden"

                stranger_ws.send_json(
                    {
                        "type": "webrtc.offer",
                        "data": {
                            "conference_id": conference_id,
                            "to_user_id": alice["id_str"],
                            "sdp": "v=0",
                        },
                    }
                )
                assert stranger_ws.receive_json()["data"]["code"] == "forbidden"


def test_conference_participant_can_invite(client: TestClient) -> None:
    alice = make_user()
    bob = make_user()
    carol = make_user()
    with connect(client, alice) as alice_ws:
        receive_type(alice_ws, "ready")
        with connect(client, bob) as bob_ws:
            receive_type(bob_ws, "ready")
            with connect(client, carol) as carol_ws:
                receive_type(carol_ws, "ready")
                alice_ws.send_json(
                    {
                        "type": "conference.create",
                        "data": {"media": "video", "invitee_ids": [bob["id_str"]]},
                    }
                )
                conference_id = receive_type(alice_ws, "conference.created")["data"]["conference"][
                    "id"
                ]
                receive_type(bob_ws, "conference.invited")
                bob_ws.send_json(
                    {"type": "conference.join", "data": {"conference_id": conference_id}}
                )
                receive_type(alice_ws, "conference.joined")
                receive_type(bob_ws, "conference.joined")

                bob_ws.send_json(
                    {
                        "type": "conference.invite",
                        "data": {
                            "conference_id": conference_id,
                            "user_ids": [carol["id_str"]],
                        },
                    }
                )
                invited = receive_type(carol_ws, "conference.invited")
                assert invited["data"]["conference"]["id"] == conference_id
                assert invited["data"]["invited_by"]["id"] == bob["id_str"]

                carol_ws.send_json(
                    {"type": "conference.join", "data": {"conference_id": conference_id}}
                )
                joined = receive_type(alice_ws, "conference.joined")
                assert set(joined["data"]["conference"]["joined_ids"]) == {
                    alice["id_str"],
                    bob["id_str"],
                    carol["id_str"],
                }
                receive_type(bob_ws, "conference.joined")
                receive_type(carol_ws, "conference.joined")


def test_conference_resync_and_renegotiate(client: TestClient) -> None:
    alice = make_user()
    bob = make_user()
    stranger = make_user()
    with connect(client, alice) as alice_ws:
        receive_type(alice_ws, "ready")
        with connect(client, bob) as bob_ws:
            receive_type(bob_ws, "ready")
            with connect(client, stranger) as stranger_ws:
                receive_type(stranger_ws, "ready")
                alice_ws.send_json(
                    {
                        "type": "conference.create",
                        "data": {"media": "video", "invitee_ids": [bob["id_str"]]},
                    }
                )
                conference_id = receive_type(alice_ws, "conference.created")["data"]["conference"][
                    "id"
                ]
                receive_type(bob_ws, "conference.invited")
                bob_ws.send_json(
                    {"type": "conference.join", "data": {"conference_id": conference_id}}
                )
                receive_type(alice_ws, "conference.joined")
                receive_type(bob_ws, "conference.joined")

                bob_ws.send_json(
                    {
                        "type": "conference.resync",
                        "data": {"conference_id": conference_id, "renegotiate": True},
                    }
                )
                reconnected = receive_type(alice_ws, "conference.reconnected")
                assert reconnected["data"]["renegotiate"] is True
                assert reconnected["data"]["participant"]["user"]["id"] == bob["id_str"]

                bob_ws.send_json(
                    {
                        "type": "conference.resync",
                        "data": {"conference_id": conference_id, "renegotiate": "yes"},
                    }
                )
                assert bob_ws.receive_json()["data"]["code"] == "invalid_payload"

                bob_ws.send_json(
                    {
                        "type": "conference.renegotiate",
                        "data": {
                            "conference_id": conference_id,
                            "to_user_id": alice["id_str"],
                        },
                    }
                )
                renegotiate = receive_type(alice_ws, "conference.renegotiate")
                assert renegotiate["data"]["from_user_id"] == bob["id_str"]
                assert renegotiate["data"]["to_user_id"] == alice["id_str"]

                stranger_ws.send_json(
                    {
                        "type": "conference.renegotiate",
                        "data": {
                            "conference_id": conference_id,
                            "to_user_id": alice["id_str"],
                        },
                    }
                )
                assert stranger_ws.receive_json()["data"]["code"] == "forbidden"

                alice_ws.send_json(
                    {
                        "type": "conference.renegotiate",
                        "data": {
                            "conference_id": conference_id,
                            "to_user_id": stranger["id_str"],
                        },
                    }
                )
                assert alice_ws.receive_json()["data"]["code"] == "forbidden"


def test_conference_host_transfer_on_leave(client: TestClient) -> None:
    alice = make_user()
    bob = make_user()
    with connect(client, alice) as alice_ws:
        receive_type(alice_ws, "ready")
        with connect(client, bob) as bob_ws:
            receive_type(bob_ws, "ready")
            alice_ws.send_json(
                {
                    "type": "conference.create",
                    "data": {"media": "audio", "invitee_ids": [bob["id_str"]]},
                }
            )
            conference_id = receive_type(alice_ws, "conference.created")["data"]["conference"]["id"]
            receive_type(bob_ws, "conference.invited")
            bob_ws.send_json({"type": "conference.join", "data": {"conference_id": conference_id}})
            receive_type(alice_ws, "conference.joined")
            receive_type(bob_ws, "conference.joined")

            alice_ws.send_json(
                {"type": "conference.leave", "data": {"conference_id": conference_id}}
            )
            left = receive_type(bob_ws, "conference.left")
            assert left["data"]["participant"]["user"]["id"] == alice["id_str"]
            assert left["data"]["conference"]["host_id"] == bob["id_str"]

            bob_ws.send_json({"type": "conference.end", "data": {"conference_id": conference_id}})
            ended = receive_type(bob_ws, "conference.ended")
            assert ended["data"]["conference"]["end_reason"] == "ended"


def test_conference_reconnect_notifies_other_participants(client: TestClient) -> None:
    alice = make_user()
    bob = make_user()
    old_grace = client.app.state.settings.disconnect_grace_seconds
    client.app.state.settings.disconnect_grace_seconds = 5
    try:
        with connect(client, alice) as alice_ws:
            receive_type(alice_ws, "ready")
            with connect(client, bob) as bob_ws:
                receive_type(bob_ws, "ready")
                alice_ws.send_json(
                    {
                        "type": "conference.create",
                        "data": {"media": "video", "invitee_ids": [bob["id_str"]]},
                    }
                )
                conference_id = receive_type(alice_ws, "conference.created")["data"]["conference"][
                    "id"
                ]
                receive_type(bob_ws, "conference.invited")
                bob_ws.send_json(
                    {"type": "conference.join", "data": {"conference_id": conference_id}}
                )
                receive_type(alice_ws, "conference.joined")
                receive_type(bob_ws, "conference.joined")

            with connect(client, bob) as bob_ws:
                ready = receive_type(bob_ws, "ready")
                assert ready["data"]["conference"]["id"] == conference_id
                reconnected = receive_type(alice_ws, "conference.reconnected")
                assert reconnected["data"]["participant"]["user"]["id"] == bob["id_str"]
                assert reconnected["data"]["conference"]["id"] == conference_id
    finally:
        client.app.state.settings.disconnect_grace_seconds = old_grace


def test_ready_restores_conference_invitation(client: TestClient) -> None:
    alice = make_user()
    bob = make_user()
    with connect(client, alice) as alice_ws:
        receive_type(alice_ws, "ready")
        with connect(client, bob) as bob_ws:
            receive_type(bob_ws, "ready")
            alice_ws.send_json(
                {
                    "type": "conference.create",
                    "data": {"media": "video", "invitee_ids": [bob["id_str"]]},
                }
            )
            created = receive_type(alice_ws, "conference.created")
            conference_id = created["data"]["conference"]["id"]
            receive_type(bob_ws, "conference.invited")

        invited_view = client.get(
            "/api/v1/calls/conference",
            headers=auth_headers(bob["token"]),
        )
        assert invited_view.json()["id"] == conference_id
        invited_participant = next(
            item
            for item in invited_view.json()["participants"]
            if item["user"]["id"] == bob["id_str"]
        )
        assert invited_participant["state"] == "invited"

        with connect(client, bob) as bob_ws:
            ready = receive_type(bob_ws, "ready")
            conference = ready["data"]["conference"]
            assert conference["id"] == conference_id
            participant = next(
                item for item in conference["participants"] if item["user"]["id"] == bob["id_str"]
            )
            assert participant["state"] == "invited"

            bob_ws.send_json({"type": "conference.join", "data": {"conference_id": conference_id}})
            receive_type(alice_ws, "conference.joined")
            joined = receive_type(bob_ws, "conference.joined")
            assert bob["id_str"] in joined["data"]["conference"]["joined_ids"]


@pytest.mark.parametrize("media", ["screen", ""])
def test_conference_rejects_invalid_media(client: TestClient, media: str) -> None:
    alice = make_user()
    with connect(client, alice) as alice_ws:
        receive_type(alice_ws, "ready")
        alice_ws.send_json({"type": "conference.create", "data": {"media": media}})
        assert alice_ws.receive_json()["data"]["code"] == "invalid_media"
