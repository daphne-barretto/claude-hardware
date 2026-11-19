"""Parameter store: hand edits lock values; locked values change only with an approval."""

from datetime import UTC, datetime, timedelta

import pytest

from app.parameters import Approval, LockedParameterError, ParameterStore

START = datetime(2026, 1, 1, tzinfo=UTC)
APPROVAL = Approval(by="Demo user", reason="accept the proposed change")


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(clock: Clock) -> ParameterStore:
    return ParameterStore(clock=clock)


def test_agent_set_records_who_and_when_and_stays_unlocked(
    store: ParameterStore, clock: Clock
) -> None:
    param = store.set("wall", 2.5, by="claude")
    assert (param.value, param.locked, param.set_by, param.set_at) == (
        2.5,
        False,
        "claude",
        clock.now,
    )
    assert store.value("wall") == 2.5
    store.set("wall", 3.0, by="claude")
    assert store.value("wall") == 3.0


def test_setting_by_hand_locks_the_parameter(store: ParameterStore) -> None:
    store.set("wall", 2.5, by="claude")
    param = store.set("wall", 3.0, by="Demo user", by_hand=True)
    assert param.locked
    assert param.set_by == "Demo user"
    assert [p.name for p in store.locked()] == ["wall"]


@pytest.mark.parametrize("by_hand", [False, True])
def test_locked_value_refuses_changes_without_approval(
    store: ParameterStore, by_hand: bool
) -> None:
    store.set("wall", 3.0, by="Demo user", by_hand=True)
    with pytest.raises(LockedParameterError, match="wall"):
        store.set("wall", 2.0, by="claude", by_hand=by_hand)
    assert store.value("wall") == 3.0
    assert store.get("wall").set_by == "Demo user"


def test_locked_value_changes_with_approval_and_stays_locked(
    store: ParameterStore, clock: Clock
) -> None:
    store.set("wall", 3.0, by="Demo user", by_hand=True)
    param = store.set("wall", 2.0, by="claude", approval=APPROVAL)
    assert param.value == 2.0
    assert param.locked
    assert param.approval == APPROVAL
    assert param.set_at == clock.now


def test_restore_puts_back_an_earlier_record_only_for_the_current_holder(
    store: ParameterStore,
) -> None:
    hand = store.set("wall", 3.0, by="Demo user", by_hand=True)
    approved = store.set("wall", 2.0, by="claude", approval=APPROVAL)
    assert store.restore(hand, replacing=approved) == hand
    assert store.get("wall") == hand and store.get("wall").approval is None
    # The value changed since `approved`, so whoever held it can't put an old record back.
    with pytest.raises(LockedParameterError):
        store.restore(approved, replacing=approved)
    assert store.get("wall") == hand


def test_unlock_needs_approval_and_returns_control(store: ParameterStore) -> None:
    store.set("wall", 3.0, by="Demo user", by_hand=True)
    with pytest.raises(LockedParameterError):
        store.unlock("wall", approval=None)  # type: ignore[arg-type]
    param = store.unlock("wall", approval=APPROVAL)
    assert not param.locked
    assert param.value == 3.0
    store.set("wall", 2.0, by="claude")
    assert store.value("wall") == 2.0


def test_store_round_trips_through_json(store: ParameterStore) -> None:
    store.set("wall", 3.0, by="Demo user", by_hand=True)
    store.set("material", "ABS", by="claude")
    store.set("vents", True, by="claude")
    restored = ParameterStore.from_json(store.to_json())
    assert list(restored) == list(store)
    with pytest.raises(LockedParameterError):
        restored.set("wall", 1.0, by="claude")
