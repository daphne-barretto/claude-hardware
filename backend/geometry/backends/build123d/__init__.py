"""The build123d backend (the default)."""

from __future__ import annotations

from typing import Any

from OCP.TopoDS import TopoDS_Shape

from geometry.interface import BuildError, GeometryBackend


class Build123dBackend(GeometryBackend):
    name = "build123d"

    def to_shape(self, obj: Any) -> TopoDS_Shape:
        if isinstance(obj, TopoDS_Shape):
            return obj
        wrapped = getattr(obj, "wrapped", None)
        if isinstance(wrapped, TopoDS_Shape):
            return wrapped
        raise BuildError(f"expected a build123d shape, got {type(obj).__name__}")
