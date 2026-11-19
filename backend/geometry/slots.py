"""How many builds run at once on this instance, how much memory each may take, and who goes next.

Every process that loads the CAD kernel (a sandboxed build, `geometry.sandbox`, or a geometry
worker, `geometry.worker`) runs inside a build slot. There are `BUILD_CONCURRENCY` slots per
instance, sized from the machine when unset (`geometry.capacity`); a build that finds them all
taken waits for one and, first, calls the `on_queued` callback of its context, which the agent
uses to say in the chat that the build is queued. `asyncio.to_thread` copies the context, so a
callback set in the coroutine reaches the thread that waits.

The slots are shared fairly between owners (`slot_owner`, the browser a build is for, set per
request by `app.owners`): when a slot frees up, it goes to the waiting build whose owner has the
fewest builds running, the earliest of those first (`fair_order`). So one person's many
builds (a change set's per-change builds, several sessions) can't keep another's waiting.

`BUILD_MEMORY_MB` is the memory a sandboxed build may use on top of the loaded libraries
(`docs/architecture.md` explains the default). Nothing here loads the CAD kernel.
"""

from __future__ import annotations

import itertools
import threading
from collections import Counter
from collections.abc import Callable, Hashable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar

from geometry.capacity import (
    CONCURRENCY_ENV,
    DEFAULT_MEMORY_MB,
    MEMORY_ENV,
    build_memory_mb,
    build_slots,
)

on_queued: ContextVar[Callable[[], None] | None] = ContextVar("on_queued", default=None)
# Whose build this is (a browser's owner token); None: nobody's in particular.
slot_owner: ContextVar[str | None] = ContextVar("slot_owner", default=None)


def fair_order[K: Hashable](waiting: Sequence[tuple[K, Hashable]], running: Counter) -> list[K]:
    """The order in which `waiting` (key, owner) pairs, oldest first, get the next slots, given
    how many each owner has `running`: each next slot goes to the owner with the fewest running
    (counting those already given one), the earliest waiting of theirs first. Round-robin
    across owners, first come first served within one."""
    held = Counter(running)
    left = list(waiting)
    order: list[K] = []
    while left:
        i = min(range(len(left)), key=lambda n: (held[left[n][1]], n))
        key, owner = left.pop(i)
        held[owner] += 1
        order.append(key)
    return order


class FairSlots:
    """`size` slots that threads take and give back, handed out by `fair_order`."""

    def __init__(self, size: int):
        self.size = size
        self._cond = threading.Condition()
        self._running: Counter = Counter()
        self._waiting: list[tuple[int, Hashable]] = []
        self._tickets = itertools.count()

    @property
    def taken(self) -> int:
        return sum(self._running.values())

    def waiting(self) -> int:
        with self._cond:
            return len(self._waiting)

    def _next(self) -> int | None:
        order = fair_order(self._waiting, self._running)
        return order[0] if order else None

    def acquire(self, owner: Hashable, on_wait: Callable[[], None] | None = None) -> None:
        with self._cond:
            if self.taken < self.size and not self._waiting:
                self._running[owner] += 1
                return
            ticket = next(self._tickets)
            self._waiting.append((ticket, owner))
        if on_wait is not None:
            on_wait()
        with self._cond:
            try:
                while self.taken >= self.size or self._next() != ticket:
                    self._cond.wait()
            finally:
                self._waiting.remove((ticket, owner))
                self._cond.notify_all()
            self._running[owner] += 1

    def release(self, owner: Hashable) -> None:
        with self._cond:
            self._running[owner] -= 1
            if self._running[owner] <= 0:
                del self._running[owner]
            self._cond.notify_all()


_lock = threading.Lock()
_slots: FairSlots | None = None


def concurrency() -> int:
    """`BUILD_CONCURRENCY`: how many builds may run at once (sized from the machine if unset)."""
    return build_slots()


def _pool() -> FairSlots:
    global _slots
    n = concurrency()
    with _lock:
        if _slots is None or _slots.size != n:
            _slots = FairSlots(n)
        return _slots


@contextmanager
def build_slot() -> Iterator[None]:
    """Hold one of the instance's build slots, waiting (after `on_queued`) if none is free."""
    pool, owner = _pool(), slot_owner.get()
    pool.acquire(owner, on_queued.get())
    try:
        yield
    finally:
        pool.release(owner)


__all__ = [
    "CONCURRENCY_ENV",
    "DEFAULT_MEMORY_MB",
    "MEMORY_ENV",
    "FairSlots",
    "build_memory_mb",
    "build_slot",
    "concurrency",
    "fair_order",
    "on_queued",
    "slot_owner",
]
