"""The CadQuery backend."""

from __future__ import annotations

from typing import Any

from OCP.TopoDS import TopoDS_Shape

from geometry.interface import BuildError, GeometryBackend


class CadQueryBackend(GeometryBackend):
    name = "cadquery"

    def to_shape(self, obj: Any) -> TopoDS_Shape:
        import cadquery as cq

        if isinstance(obj, TopoDS_Shape):
            return obj
        if isinstance(obj, cq.Workplane):
            shapes = [o for o in obj.vals() if isinstance(o, cq.Shape)]
            if len(shapes) != 1:
                raise BuildError(f"a Workplane must hold exactly one shape, not {len(shapes)}")
            obj = shapes[0]
        if isinstance(obj, cq.Shape):
            return obj.wrapped
        raise BuildError(f"expected a CadQuery shape or Workplane, got {type(obj).__name__}")
