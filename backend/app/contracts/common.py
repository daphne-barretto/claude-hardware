"""Shared pieces of the backend/frontend data contracts (see `contracts/README.md`)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACTS_DIR = REPO_ROOT / "contracts"
FIXTURES_DIR = REPO_ROOT / "fixtures"
DEMOS_DIR = REPO_ROOT / "demos"

# Stable IDs of faces, edges and features. Opaque to the frontend; never reused for a
# different face within one mesh. Board references keep their case (`opening_Q4`).
StableId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.:\-]*$", max_length=200)]
Point3 = tuple[float, float, float]


class ContractError(ValueError):
    """Data does not match its contract."""


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BoardRef(Contract):
    """A board revision, as named in its `demos/<board>/*.json` file."""

    name: str = Field(min_length=1)
    revision: str = Field(min_length=1)


class Location(Contract):
    """Where on the model something is: a point (mm, board coordinates) and the faces and edges
    it concerns, by their stable mesh IDs."""

    point: Point3
    part: str | None = Field(default=None, description="Part node name, e.g. `lid`")
    faces: list[StableId] = Field(default_factory=list)
    edges: list[StableId] = Field(default_factory=list)


def check_location(location: Location, mesh, where: str) -> None:
    """Every face and edge a location names exists in the mesh (a `MeshInfo`)."""
    for face in location.faces:
        if face not in mesh.face_parts:
            raise ContractError(f"{where}: unknown face id {face!r}")
        if location.part is not None and mesh.face_parts[face] != location.part:
            raise ContractError(f"{where}: face {face!r} is not on part {location.part!r}")
    for edge in location.edges:
        if edge not in mesh.edge_parts:
            raise ContractError(f"{where}: unknown edge id {edge!r}")
    if location.part is not None and location.part not in mesh.parts:
        raise ContractError(f"{where}: unknown part {location.part!r}")
