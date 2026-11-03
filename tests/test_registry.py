from uuid import uuid4

import pytest

from app.core.auth import UserIdentity
from app.realtime.registry import ACTIVE, ENDED, RINGING, CallError, CallRegistry


def user(name: str | None = None) -> UserIdentity:
    user_id = uuid4()
    return UserIdentity(id=user_id, username=name, display_name=name)


def test_create_and_accept_call() -> None:
    registry = CallRegistry()
    caller, callee = user("caller"), user("callee")

    call = registry.create(caller, callee, "video")
    assert call.state == RINGING
    assert call.status == "ringing"
    assert registry.active_for(caller.id) is call
    assert registry.is_in_call(callee.id)

    accepted = registry.accept(call.id, callee.id)
    assert accepted.state == ACTIVE
    assert accepted.status == "active"
    assert accepted.answered_at is not None


def test_finish_and_release_call() -> None:
    registry = CallRegistry()
    caller, callee = user("caller"), user("callee")
    call = registry.create(caller, callee, "audio")
    registry.accept(call.id, callee.id)

    ended = registry.finish(call.id, "hangup")
    assert ended is not None
    assert ended.state == ENDED
    assert ended.status == "completed"
    assert ended.ended_at is not None

    assert not registry.is_in_call(caller.id)
    assert registry.active_for(callee.id) is None
    registry.release(call.id)
    assert registry.get(call.id) is None


def test_reject_and_cancel_statuses() -> None:
    registry = CallRegistry()
    caller, callee = user("caller"), user("callee")

    first = registry.create(caller, callee, "audio")
    assert registry.finish(first.id, "rejected") is not None
    assert first.status == "rejected"
    registry.release(first.id)

    second = registry.create(caller, callee, "audio")
    assert registry.finish(second.id, "cancelled") is not None
    assert second.status == "cancelled"
    registry.release(second.id)

    third = registry.create(caller, callee, "audio")
    assert registry.finish(third.id, "timeout") is not None
    assert third.status == "missed"
    registry.release(third.id)


def test_cannot_call_self() -> None:
    registry = CallRegistry()
    alice = user("alice")
    with pytest.raises(CallError) as exc_info:
        registry.create(alice, alice, "audio")
    assert exc_info.value.code == "self_call"


def test_invalid_media_rejected() -> None:
    registry = CallRegistry()
    with pytest.raises(CallError) as exc_info:
        registry.create(user(), user(), "screen")
    assert exc_info.value.code == "invalid_media"


def test_busy_users_are_rejected() -> None:
    registry = CallRegistry()
    alice, bob, carol = user("alice"), user("bob"), user("carol")
    call = registry.create(alice, bob, "video")

    with pytest.raises(CallError) as caller_busy:
        registry.create(alice, carol, "audio")
    assert caller_busy.value.code == "caller_busy"

    with pytest.raises(CallError) as callee_busy:
        registry.create(carol, bob, "audio")
    assert callee_busy.value.code == "callee_busy"

    registry.release(call.id)


def test_roles_and_states_are_enforced() -> None:
    registry = CallRegistry()
    alice, bob, carol = user("alice"), user("bob"), user("carol")
    call = registry.create(alice, bob, "video")

    with pytest.raises(CallError) as not_participant:
        registry.expect(call.id, carol.id)
    assert not_participant.value.code == "forbidden"

    with pytest.raises(CallError) as wrong_role:
        registry.accept(call.id, alice.id)
    assert wrong_role.value.code == "forbidden"

    with pytest.raises(CallError) as wrong_state:
        registry.accept(call.id, bob.id)
        registry.accept(call.id, bob.id)
    assert wrong_state.value.code == "invalid_state"

    with pytest.raises(CallError) as missing:
        registry.expect(uuid4(), alice.id)
    assert missing.value.code == "call_not_found"
