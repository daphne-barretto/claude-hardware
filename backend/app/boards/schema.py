"""The board JSON schema. `backend/app/boards/SCHEMA.md` documents it for people."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1

Point = tuple[float, float]
Side = Literal["top", "bottom"]
OpeningKind = Literal["sensor_window", "vent", "button", "antenna_keepout"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Source(_Model):
    repo: str
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    path: str
    license: str
    kicad_origin: Point = Field(
        description="KiCad coordinates of the board origin; board y = origin y - KiCad y"
    )


class Pcb(_Model):
    thickness: float = Field(gt=0)
    outline: list[Point] = Field(min_length=3, description="Closed polygon, counter-clockwise")
    material: str | None = Field(
        default=None, description="Substrate material, e.g. from the KiCad stackup"
    )


class MountingHole(_Model):
    id: str
    position: Point
    diameter: float = Field(gt=0)


class Component(_Model):
    ref: str
    value: str
    footprint: str
    role: str | None = None
    side: Side
    position: Point = Field(description="Centre of the body on the board")
    rotation: float = Field(ge=0, lt=360, description="Degrees counter-clockwise")
    size: Point = Field(description="Body width and length along its own x and y axes")
    height: float = Field(ge=0, description="Height above the board surface on its side")


class Opening(_Model):
    id: str
    kind: OpeningKind
    component: str
    side: Side
    position: Point
    rotation: float = Field(ge=0, lt=360)
    size: Point


class Board(_Model):
    schema_version: Literal[1]
    name: str
    revision: str
    source: Source
    units: Literal["mm"]
    pcb: Pcb
    mounting_holes: list[MountingHole]
    components: list[Component]
    openings: list[Opening]

    def component(self, ref: str) -> Component:
        for c in self.components:
            if c.ref == ref:
                return c
        raise KeyError(ref)


def load_board(path: Path | str) -> Board:
    return Board.model_validate(json.loads(Path(path).read_text()))
