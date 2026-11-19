"""Analysis-map contract: what the analysis produces and the color maps show.

See `contracts/README.md` ("Analysis maps"). A map has one value per vertex of each part's
triangle primitive in the mesh it was computed on, so `validate_analysis_map()` needs that mesh.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, ValidationError, model_validator

from app.contracts.common import Contract, ContractError, Location, StableId, check_location
from app.contracts.mesh import MeshInfo
from app.standards import StandardsFile

MAPS_CONTRACT = "analysis-map/1"

# Map type -> the standard it is judged against and its unit.
MAP_TYPES = {"wall_thickness": ("min_wall", "mm"), "draft": ("min_draft", "deg")}
MapType = Literal["wall_thickness", "draft"]


class Legend(Contract):
    """The color scale. Values outside [min, max] clamp to the end colors."""

    min: float
    max: float
    limit: float | None = Field(
        default=None, description="The standard's limit, drawn as a mark on the legend"
    )
    unit: Literal["mm", "deg"]

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if not self.min < self.max:
            raise ValueError("legend min must be below max")
        return self


class PartValues(Contract):
    part: StableId
    values: list[float | None] = Field(
        description="One per vertex of the part's triangle primitive, in vertex order. "
        "Null where the analysis does not apply."
    )


class FlaggedSpot(Contract):
    id: StableId = Field(description="Unique in the map")
    kind: Literal["thin", "thick", "low_draft"]
    value: float = Field(description="The worst value in the spot, in the map's unit")
    message: str = Field(min_length=1)
    location: Location


class AnalysisMap(Contract):
    contract: Literal["analysis-map/1"]
    build_id: StableId
    type: MapType
    standard: StableId = Field(description="The standard the map is judged against")
    standard_title: str | None = Field(
        default=None, description="That standard's title, as people read it"
    )
    unit: Literal["mm", "deg"]
    legend: Legend
    parts: list[PartValues] = Field(min_length=1)
    flagged: list[FlaggedSpot] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        standard, unit = MAP_TYPES[self.type]
        if self.standard != standard or self.unit != unit or self.legend.unit != unit:
            raise ValueError(f"a {self.type} map is judged against {standard} in {unit}")
        ids = [s.id for s in self.flagged]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate flagged spot ids")
        return self


def validate_analysis_map(data: dict, mesh: MeshInfo, standards: StandardsFile) -> AnalysisMap:
    """Validate a map against the mesh it was computed on; raises `ContractError`."""
    try:
        amap = AnalysisMap.model_validate(data)
    except ValidationError as error:
        raise ContractError(f"analysis map: {error}") from error
    try:
        limit = standards.get(amap.standard)
    except KeyError:
        raise ContractError(f"unknown standard {amap.standard!r}") from None
    if amap.legend.limit is not None and amap.legend.limit != limit.value:
        raise ContractError(f"legend limit {amap.legend.limit} is not {amap.standard}'s value")
    seen = set()
    for pv in amap.parts:
        if pv.part not in mesh.parts:
            raise ContractError(f"unknown part {pv.part!r}")
        if pv.part in seen:
            raise ContractError(f"part {pv.part!r} listed twice")
        seen.add(pv.part)
        expected = mesh.parts[pv.part].vertex_count
        if len(pv.values) != expected:
            raise ContractError(
                f"part {pv.part!r}: {len(pv.values)} values for {expected} mesh vertices"
            )
    for spot in amap.flagged:
        check_location(spot.location, mesh, spot.id)
        if not spot.location.faces:
            raise ContractError(f"{spot.id}: a flagged spot names at least one face")
    return amap
