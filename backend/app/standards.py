"""Team standards: load `standards.yaml` and validate it against a schema.

Every standard must name the check that enforces it and the designs it applies to
(`applies_to`). The schema rejects a standard without either, so the loader fails
instead of silently letting the agent ignore an unchecked rule.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.json_schema import SkipJsonSchema

REPO_ROOT = Path(__file__).resolve().parents[2]

Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
AppliesTo = Literal["any", "board", "object"]


class StandardsError(ValueError):
    """The standards file is missing or does not match the schema."""


class Derivation(BaseModel):
    """How a value was obtained when it is measured rather than chosen."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1)
    method: str = Field(min_length=1)


class Standard(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Identifier
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    kind: Literal["min", "max", "tolerance", "spec"]
    value: float | str
    unit: Literal["mm", "deg"] | None = None
    check: Identifier
    applies_to: AppliesTo = Field(
        description="Which designs the standard applies to: `any` design, only designs "
        "built around a `board`, or only designs made to fit an `object` (a photographed or "
        "described object the part goes around: the design's `object` parameters)"
    )
    derivation: Derivation | None = None
    dimensions: dict[Identifier, Annotated[float, Field(gt=0)]] | None = Field(
        default=None, description="Named sizes in mm that make a spec measurable"
    )
    material: str | None = Field(
        default=None,
        min_length=1,
        description="Material of the bought-in part a spec names (e.g. the insert), which the "
        "assembly gives that part",
    )
    aliases: list[Annotated[str, Field(min_length=1)]] = Field(
        default_factory=list,
        description="Words that name the standard in a description (`app.spec`)",
    )
    enabled: bool = Field(
        default=True,
        description="False: the rule is turned off in its set. It is not checked (the report "
        "lists it as not run) and Claude doesn't design to it; only the user turns it on again",
    )

    @model_validator(mode="after")
    def _value_matches_kind(self) -> Self:
        if self.kind == "spec":
            if not isinstance(self.value, str) or not self.value.strip():
                raise ValueError(f"{self.id}: a spec standard needs a text value")
        else:
            if isinstance(self.value, str):
                raise ValueError(f"{self.id}: a {self.kind} standard needs a numeric value")
            if self.unit is None:
                raise ValueError(f"{self.id}: a {self.kind} standard needs a unit")
            if self.kind == "tolerance" and self.value <= 0:
                raise ValueError(f"{self.id}: a tolerance must be positive")
        return self

    def applies(self, has_board: bool, has_object: bool = False) -> bool:
        """Whether the standard applies to a design with (or without) a board, and that fits
        (or doesn't fit) an object."""
        if self.applies_to == "board":
            return has_board
        if self.applies_to == "object":
            return has_object
        return True

    def not_applicable_reason(self) -> str:
        """Why the standard does not apply to a design that lacks what it needs."""
        if self.applies_to == "object":
            return (
                f"{self.title} applies only to designs made to fit an object; this design has "
                "no object parameters."
            )
        return f"{self.title} applies only to designs built around a board; this design has none."


class StandardsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    id: Identifier = Field(description="Stable ID of the set; the session API names it by this")
    name: str = Field(min_length=1, description="What the standards picker shows")
    # A set the user made may be empty while they build it; the file must have rules
    # (`parse_standards`).
    standards: list[Standard]
    # Values the design code falls back on for rules this set doesn't have (`get()`): the
    # built-in set's, so a generator that needs a wall thickness still has one. Never checked,
    # never shown as a rule. Empty for the built-in set itself.
    defaults: SkipJsonSchema[list[Standard]] = Field(default_factory=list, exclude=True)

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        seen: set[str] = set()
        for standard in self.standards:
            if standard.id in seen:
                raise ValueError(f"duplicate standard id: {standard.id}")
            seen.add(standard.id)
        return self

    def get(self, standard_id: str) -> Standard:
        """The set's rule with this ID (on or off), else the fallback value (`defaults`)."""
        for standard in (*self.standards, *self.defaults):
            if standard.id == standard_id:
                return standard
        raise KeyError(standard_id)

    def has(self, standard_id: str) -> bool:
        """Whether the set itself has this rule (on or off), not just a fallback value."""
        return any(s.id == standard_id for s in self.standards)

    @property
    def enabled(self) -> list[Standard]:
        """The rules that are on: what the checks enforce and Claude designs to."""
        return [s for s in self.standards if s.enabled]

    @property
    def checks(self) -> set[str]:
        """Names of every check the rules that are on require."""
        return {standard.check for standard in self.enabled}


def to_payload(standards: StandardsFile) -> dict:
    """The set as JSON, its fallback values included: what a worker process gets, since it
    can't look a browser's own set up (`app.standards_sets`)."""
    return {
        **standards.model_dump(mode="json"),
        "defaults": [s.model_dump(mode="json") for s in standards.defaults],
    }


def from_payload(data: dict) -> StandardsFile:
    """The set `to_payload` wrote."""
    body = {k: v for k, v in data.items() if k != "defaults"}
    defaults = [Standard.model_validate(s) for s in data.get("defaults", [])]
    return StandardsFile.model_validate(body).model_copy(update={"defaults": defaults})


def default_path() -> Path:
    """`STANDARDS_PATH` if set (the Docker image sets it), else the repo's file."""
    return Path(os.environ.get("STANDARDS_PATH", REPO_ROOT / "standards.yaml"))


def parse_standards(data: object) -> StandardsFile:
    try:
        standards = StandardsFile.model_validate(data)
    except ValueError as error:
        raise StandardsError(str(error)) from error
    if not standards.standards:
        raise StandardsError("standards: the file needs at least one standard")
    return standards


def load_standards(path: Path | str | None = None) -> StandardsFile:
    path = Path(path) if path is not None else default_path()
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as error:
        raise StandardsError(f"cannot read {path}: {error}") from error
    return parse_standards(data)


def json_schema() -> dict:
    """The JSON Schema for `standards.yaml`, for tools and documentation."""
    return StandardsFile.model_json_schema()
