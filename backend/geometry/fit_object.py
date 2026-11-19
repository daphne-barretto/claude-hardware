"""The object a design fits: a photographed or described object the part is made around
(a stand, a cover, a mount), as the design's parameters give it.

The design keeps it in `params["object"]`: `width`, `depth` and `height` in mm, an optional
`shape` (`box`, the default, or `cylinder`; anything else is checked as its bounding box) and
an optional `at`, the centre of the object's bottom face in the design's coordinates
(default the origin). The `object_fit` check (`geometry.checks`) models it as a solid there.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from OCP.TopoDS import TopoDS_Shape

from geometry import occ

DIMENSIONS = ("width", "depth", "height")


def _length(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and value > 0


@dataclass(frozen=True)
class FitObject:
    shape: str
    width: float
    depth: float
    height: float
    at: tuple[float, float, float] = (0.0, 0.0, 0.0)

    @classmethod
    def from_params(cls, params: dict[str, Any] | None) -> FitObject | None:
        """The object in a design's parameters, or None when the design fits no object."""
        found = (params or {}).get("object")
        if not isinstance(found, dict) or not all(_length(found.get(d)) for d in DIMENSIONS):
            return None
        at = found.get("at")
        if not (
            isinstance(at, list | tuple)
            and len(at) == 3
            and all(isinstance(v, int | float) and not isinstance(v, bool) for v in at)
        ):
            at = (0.0, 0.0, 0.0)
        shape = found.get("shape") if found.get("shape") in ("box", "cylinder") else "box"
        return cls(shape, *(float(found[d]) for d in DIMENSIONS), tuple(float(v) for v in at))

    def solid(self) -> TopoDS_Shape:
        x, y, z = self.at
        if self.shape == "cylinder":
            return occ.cylinder(x, y, min(self.width, self.depth) / 2, z, z + self.height)
        w, d = self.width / 2, self.depth / 2
        corners = [(x - w, y - d), (x + w, y - d), (x + w, y + d), (x - w, y + d)]
        return occ.prism(corners, z, z + self.height)

    def describe(self) -> str:
        size = " × ".join(f"{v:g}" for v in (self.width, self.depth, self.height))
        return f"the {self.shape} object ({size} mm)"
