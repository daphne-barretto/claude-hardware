"""The assembly: a build's parts plus the board, each with a name, kind, material and placement.

A build returns the enclosure's parts (`base`, `lid`) and the hardware pressed into them (the
heat-set inserts). The board joins them as the part `board`: its outline through its thickness,
less the mounting holes, with every component's body as a box on its side of the board (the
same bodies the component clearance check measures). Its material is the board file's
`pcb.material` and it sits at the origin, since board coordinates are the assembly's.

The checks between parts (`geometry.checks`) and the STEP export work on this assembly.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

from OCP.BRep import BRep_Builder
from OCP.TopoDS import TopoDS_Compound, TopoDS_Shape

from app.boards import Board
from app.boards.schema import Component
from geometry import occ
from geometry.interface import BOARD, ORIGIN, BuildResult, PartKind, Placement
from geometry.params import opening_corners

BOARD_PART = BOARD


@dataclass(frozen=True)
class AssemblyPart:
    name: str
    kind: PartKind
    material: str | None
    placement: Placement
    shape: TopoDS_Shape

    def to_json(self) -> dict:
        """Name, kind, material and placement (no geometry)."""
        return {
            "name": self.name,
            "kind": self.kind,
            "material": self.material,
            "placement": list(self.placement),
        }


def pcb_shape(board: Board) -> TopoDS_Shape:
    """The bare board: its outline through its thickness, less the mounting holes."""
    t = board.pcb.thickness
    shape = occ.prism(list(board.pcb.outline), 0.0, t)
    for h in board.mounting_holes:
        shape = occ.cut(shape, occ.cylinder(*h.position, h.diameter / 2, -1.0, t + 1.0))
    return shape


def component_box(board: Board, c: Component) -> TopoDS_Shape:
    """A component's body: a box of its size and height on its side of the board."""
    corners = opening_corners(
        {"x": c.position[0], "y": c.position[1], "rotation": c.rotation, "size": c.size}
    )
    height = max(c.height, 1e-3)
    if c.side == "top":
        z0 = board.pcb.thickness
        return occ.prism(corners, z0, z0 + height)
    return occ.prism(corners, -height, 0.0)


def board_shape(board: Board) -> TopoDS_Shape:
    """The board part: the bare board and every component body, as one compound."""
    compound = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(compound)
    builder.Add(compound, pcb_shape(board))
    for c in board.components:
        builder.Add(compound, component_box(board, c))
    return compound


def assembly(result: BuildResult, board: Board | None) -> dict[str, AssemblyPart]:
    """Every part of the build, then the board (when the design has one), by name."""
    parts = {
        name: AssemblyPart(
            name, result.kind(name), result.materials.get(name), result.placement(name), shape
        )
        for name, shape in result.parts.items()
    }
    if board is None:
        return parts
    parts[BOARD_PART] = AssemblyPart(
        BOARD_PART, BOARD, board.pcb.material, ORIGIN, board_shape(board)
    )
    return parts


def boxes_touch(a: TopoDS_Shape, b: TopoDS_Shape, eps: float = 1e-6) -> bool:
    (a0, a1), (b0, b1) = occ.bbox(a), occ.bbox(b)
    return all(a0[i] <= b1[i] + eps and b0[i] <= a1[i] + eps for i in range(3))


def neighbours(parts: dict[str, AssemblyPart]) -> list[tuple[str, str]]:
    """Every pair of parts whose bounding boxes meet: the only pairs that can interfere.

    Pairs whose boxes are apart cannot share material, so leaving them out is exact."""
    return [
        (a, b) for a, b in combinations(parts, 2) if boxes_touch(parts[a].shape, parts[b].shape)
    ]


__all__ = [
    "BOARD_PART",
    "AssemblyPart",
    "assembly",
    "board_shape",
    "boxes_touch",
    "component_box",
    "neighbours",
    "pcb_shape",
]
