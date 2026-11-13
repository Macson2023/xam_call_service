import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_turn_is_appended_to_ice_servers() -> None:
    settings = Settings(
        _env_file=None,
        ice_servers=[{"urls": ["stun:stun.example.org:3478"]}],
        turn_urls=["turn:turn.example.org:3478?transport=udp"],
        turn_username="user",
        turn_credential="secret",
    )

    assert settings.resolved_ice_servers == [
        {"urls": ["stun:stun.example.org:3478"]},
        {
            "urls": ["turn:turn.example.org:3478?transport=udp"],
            "username": "user",
            "credential": "secret",
        },
    ]


def test_conference_participant_limit_is_bounded() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, max_conference_participants=1)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, max_conference_participants=13)
    assert (
        Settings(_env_file=None, max_conference_participants=12).max_conference_participants == 12
    )


def test_turn_urls_string_is_split_on_commas() -> None:
    settings = Settings(
        _env_file=None,
        turn_urls="turn:a.example.org:3478?transport=udp, turn:b.example.org:3478?transport=tcp",
    )

    assert settings.turn_urls == [
        "turn:a.example.org:3478?transport=udp",
        "turn:b.example.org:3478?transport=tcp",
    ]
