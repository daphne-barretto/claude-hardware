"""Examples a session can open, listed in `demos/<board>/examples.yaml`.

A new session starts empty. An example is one design the general tool handles, opened on
purpose from New (`GET /api/examples`, then `POST /api/sessions` with `{"example": id}`). A board
revision example names a changes file in `demos/<board>/` (`CHANGES.md` format): the session
opens on the existing design for the previous revision, with the new revision ready to load.

An example may bring constraints of its own (`standards`: a file next to its changes file, rules
in the `standards.yaml` format), such as a limit measured from its existing design. They are
added to whichever constraint set a session on the example uses (`Example.with_standards`),
never to the default set, so other sessions never get them.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.boards import BOARDS_DIR, Board, load_board
from app.boards.changes import parse_changes_md
from app.standards import Standard, StandardsError, StandardsFile

EXAMPLES_FILE = "examples.yaml"


class ExampleEntry(BaseModel):
    """One entry of `demos/<board>/examples.yaml`."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    description: str = Field(min_length=1)
    changes: str = Field(min_length=1, description="The changes file in demos/<board>/")
    standards: str | None = Field(
        default=None,
        min_length=1,
        description="A file of constraints the example brings, next to the changes file",
    )


class ExamplesFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    examples: list[ExampleEntry]


class Example(BaseModel):
    """An example as the API lists it, plus where its data is."""

    id: str
    title: str
    description: str
    changes: str  # the changes file in demos/<board>/
    standards: str | None = None  # the example's own constraints file, if it has one

    def changes_text(self, boards_dir: Path | None = None) -> str:
        return ((boards_dir or BOARDS_DIR) / self.changes).read_text()

    def boards(self, boards_dir: Path | None = None) -> tuple[Board, Board]:
        """The previous and the new board revision the changes file names."""
        directory = boards_dir or BOARDS_DIR
        doc = parse_changes_md(self.changes_text(directory))
        return load_board(directory / doc.old_file), load_board(directory / doc.new_file)

    def extra_standards(self, boards_dir: Path | None = None) -> list[Standard]:
        """The constraints the example brings (its `standards` file); none without one."""
        if self.standards is None:
            return []
        path = (boards_dir or BOARDS_DIR) / self.standards
        try:
            doc = yaml.safe_load(path.read_text()) or {}
            return [Standard.model_validate(s) for s in doc.get("standards", [])]
        except (OSError, yaml.YAMLError, ValueError) as error:
            raise StandardsError(f"cannot read {path}: {error}") from error

    def with_standards(self, base: StandardsFile) -> StandardsFile:
        """`base` plus the example's own constraints. A rule `base` already has (on or off)
        stays as `base` has it, so applying this twice changes nothing."""
        extra = [s for s in self.extra_standards() if not base.has(s.id)]
        if not extra:
            return base
        return base.model_copy(update={"standards": [*base.standards, *extra]})


def load_examples(boards_dir: Path | None = None) -> list[Example]:
    """The examples in `demos/<board>/examples.yaml`; none when the file is missing."""
    directory = boards_dir or BOARDS_DIR
    path = directory / EXAMPLES_FILE
    if not path.exists():
        return []
    doc = ExamplesFile.model_validate(yaml.safe_load(path.read_text()) or {"examples": []})
    ids = [e.id for e in doc.examples]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{path}: duplicate example ids")
    out = []
    for entry in doc.examples:
        partial = Example(
            id=entry.id,
            title=entry.id,
            description=entry.description,
            changes=entry.changes,
            standards=entry.standards,
        )
        previous, new = partial.boards(directory)
        title = f"{new.name} {previous.revision} to {new.revision}"
        out.append(partial.model_copy(update={"title": title}))
    return out


@cache
def examples() -> tuple[Example, ...]:
    """The examples from `BOARDS_DIR`, read once."""
    return tuple(load_examples())


def find_example(example_id: str) -> Example:
    """The example with this ID. Raises `KeyError` if there is none."""
    for example in examples():
        if example.id == example_id:
            return example
    raise KeyError(example_id)


def example_standards(example: Example | None = None) -> StandardsFile:
    """The default constraint set with what `example` (default: the first) brings: the set a
    session on that example starts with."""
    from app.standards import load_standards

    chosen = example or examples()[0]
    return chosen.with_standards(load_standards())


def example_for_board(name: str, revision: str) -> Example:
    """The board revision example whose new revision is this board, else the one whose
    previous revision is (a session still on it). Raises `KeyError`."""
    for which in (1, 0):
        for example in examples():
            board = example.boards()[which]
            if (board.name, board.revision) == (name, revision):
                return example
    raise KeyError(f"{name} {revision}")


__all__ = [
    "Example",
    "example_for_board",
    "example_standards",
    "examples",
    "find_example",
    "load_examples",
]
