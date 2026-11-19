"""Loading the next board revision: what the chat shows for it, and the fixed prompt the
run starts with. Shared by the session API and the mock, and named from the board data."""

from __future__ import annotations

from typing import Protocol


class Revision(Protocol):
    """A board revision: a `Board` or a `BoardRef`."""

    name: str
    revision: str


def revision_message(previous: Revision, board: Revision) -> str:
    """The user message that stands for the load: the user's action, in words."""
    return f"Loaded {board.name} revision {board.revision} (was {previous.revision})."


def revision_prompt(previous: Revision, board: Revision) -> str:
    """The prompt the run starts with, so nobody has to type it."""
    return (
        f"A new revision of the board has arrived: {board.name} {board.revision}. The enclosure "
        f"was designed for revision {previous.revision}. Say what changed on the board, which "
        "checks now fail and where, then update the enclosure so every check passes."
    )
