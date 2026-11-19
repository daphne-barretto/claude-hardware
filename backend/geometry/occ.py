"""Small OpenCascade helpers shared by tagging and checks.

They work on raw `TopoDS_Shape`s, so nothing here depends on which backend built a shape.
Every measurement is on the exact B-rep (solid booleans, exact distances, ray-surface
intersections), never on a mesh.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Iterator

from OCP.Bnd import Bnd_Box
from OCP.BRep import BRep_Tool
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_MakeFace,
    BRepBuilderAPI_MakePolygon,
    BRepBuilderAPI_MakeVertex,
    BRepBuilderAPI_Transform,
)
from OCP.BRepCheck import BRepCheck_Analyzer, BRepCheck_NoError, BRepCheck_Shell
from OCP.BRepClass import BRepClass_FaceClassifier
from OCP.BRepExtrema import BRepExtrema_DistShapeShape
from OCP.BRepGProp import BRepGProp, BRepGProp_Face
from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakePrism
from OCP.BRepTools import BRepTools
from OCP.gp import gp_Ax1, gp_Ax2, gp_Dir, gp_Lin, gp_Pnt, gp_Pnt2d, gp_Trsf, gp_Vec
from OCP.GProp import GProp_GProps
from OCP.IntCurvesFace import IntCurvesFace_ShapeIntersector
from OCP.TopAbs import TopAbs_FACE, TopAbs_IN, TopAbs_SHELL, TopAbs_SOLID
from OCP.TopExp import TopExp
from OCP.TopoDS import TopoDS, TopoDS_Face, TopoDS_Shape
from OCP.TopTools import TopTools_IndexedMapOfShape

Point = tuple[float, float, float]


def subshapes(shape: TopoDS_Shape, kind) -> list[TopoDS_Shape]:
    """Unique sub-shapes in a stable order (OpenCascade's indexed map order)."""
    found = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, kind, found)
    return [found.FindKey(i) for i in range(1, found.Extent() + 1)]


def faces(shape: TopoDS_Shape) -> list[TopoDS_Face]:
    return [TopoDS.Face_s(f) for f in subshapes(shape, TopAbs_FACE)]


def solids(shape: TopoDS_Shape) -> list[TopoDS_Shape]:
    return subshapes(shape, TopAbs_SOLID)


def is_closed_valid_solid(shape: TopoDS_Shape) -> bool:
    """One valid solid whose shells are all closed."""
    if len(solids(shape)) != 1 or not BRepCheck_Analyzer(shape).IsValid():
        return False
    for shell in subshapes(shape, TopAbs_SHELL):
        if BRepCheck_Shell(TopoDS.Shell_s(shell)).Closed() != BRepCheck_NoError:
            return False
    return volume(shape) > 0


def volume(shape: TopoDS_Shape | None) -> float:
    if shape is None:
        return 0.0
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    return abs(props.Mass())


def common(a: TopoDS_Shape, b: TopoDS_Shape) -> TopoDS_Shape:
    op = BRepAlgoAPI_Common(a, b)
    op.Build()
    if not op.IsDone():
        raise RuntimeError("boolean common failed")
    return op.Shape()


def cut(a: TopoDS_Shape, b: TopoDS_Shape) -> TopoDS_Shape:
    op = BRepAlgoAPI_Cut(a, b)
    op.Build()
    if not op.IsDone():
        raise RuntimeError("boolean cut failed")
    return op.Shape()


def translated(shape: TopoDS_Shape, d: Point) -> TopoDS_Shape:
    trsf = gp_Trsf()
    trsf.SetTranslation(gp_Vec(*d))
    return BRepBuilderAPI_Transform(shape, trsf, True).Shape()


def rotated(shape: TopoDS_Shape, origin: Point, axis: Point, degrees: float) -> TopoDS_Shape:
    trsf = gp_Trsf()
    trsf.SetRotation(gp_Ax1(gp_Pnt(*origin), gp_Dir(*axis)), math.radians(degrees))
    return BRepBuilderAPI_Transform(shape, trsf, True).Shape()


def overlap(a: TopoDS_Shape, b: TopoDS_Shape) -> float:
    """Volume shared by two solids (an exact boolean)."""
    return volume(common(a, b))


def bbox(shape: TopoDS_Shape) -> tuple[Point, Point]:
    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, box, False, False)
    x0, y0, z0, x1, y1, z1 = box.Get()
    return (x0, y0, z0), (x1, y1, z1)


def distance(a: TopoDS_Shape, b: TopoDS_Shape) -> tuple[float, Point]:
    """Exact minimum distance between two shapes and the point on `a` where it occurs."""
    d = BRepExtrema_DistShapeShape(a, b)
    d.Perform()
    if not d.IsDone() or d.NbSolution() == 0:
        raise RuntimeError("distance computation failed")
    p = d.PointOnShape1(1)
    return d.Value(), (p.X(), p.Y(), p.Z())


def vertex(p: Point) -> TopoDS_Shape:
    return BRepBuilderAPI_MakeVertex(gp_Pnt(*p)).Vertex()


def vertex_point(v: TopoDS_Shape) -> Point:
    p = BRep_Tool.Pnt_s(TopoDS.Vertex_s(v))
    return p.X(), p.Y(), p.Z()


def prism(polygon: Iterable[tuple[float, float]], z0: float, z1: float) -> TopoDS_Shape:
    """A straight prism over a 2D polygon from z0 to z1 (for probing, not for building)."""
    poly = BRepBuilderAPI_MakePolygon()
    for x, y in polygon:
        poly.Add(gp_Pnt(x, y, z0))
    poly.Close()
    face = BRepBuilderAPI_MakeFace(poly.Wire()).Face()
    return BRepPrimAPI_MakePrism(face, gp_Vec(0, 0, z1 - z0)).Shape()


def cylinder(x: float, y: float, radius: float, z0: float, z1: float) -> TopoDS_Shape:
    axis = gp_Ax2(gp_Pnt(x, y, z0), gp_Dir(0, 0, 1))
    return BRepPrimAPI_MakeCylinder(axis, radius, z1 - z0).Shape()


def _face_uv_grid(face: TopoDS_Face, n: int) -> Iterator[tuple[float, float]]:
    u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
    for i in range(n):
        for j in range(n):
            yield u0 + (u1 - u0) * (i + 0.5) / n, v0 + (v1 - v0) * (j + 0.5) / n


def face_samples(face: TopoDS_Face, n: int = 5) -> list[tuple[Point, Point]]:
    """Points inside a face with the outward normal at each: [(point, normal), ...].

    Points come from an n x n grid over the face's parameter range, kept only where they fall
    inside the trimmed face. A face too thin for the grid falls back to finer grids.
    """
    surface = BRep_Tool.Surface_s(face)
    props = BRepGProp_Face(face)
    out: list[tuple[Point, Point]] = []
    for grid in (n, 3 * n, 9 * n):
        for u, v in _face_uv_grid(face, grid):
            state = BRepClass_FaceClassifier(face, gp_Pnt2d(u, v), 1e-7).State()
            if state != TopAbs_IN:
                continue
            p, normal = gp_Pnt(), gp_Vec()
            props.Normal(u, v, p, normal)
            if normal.Magnitude() < 1e-12:
                continue
            normal.Normalize()
            q = surface.Value(u, v)
            out.append(((q.X(), q.Y(), q.Z()), (normal.X(), normal.Y(), normal.Z())))
        if out:
            return out
    return out


def interior_point(face: TopoDS_Face) -> Point:
    """The sample point of a face farthest from its boundary edges."""
    samples = face_samples(face, 5)
    if not samples:
        raise ValueError("face has no interior sample")
    best, best_d = samples[0][0], -1.0
    for p, _ in samples:
        d, _ = distance(vertex(p), _boundary(face))
        if d > best_d:
            best, best_d = p, d
    return best


def _boundary(face: TopoDS_Face) -> TopoDS_Shape:
    from OCP.BRep import BRep_Builder
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopoDS import TopoDS_Compound

    compound = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(compound)
    for edge in subshapes(face, TopAbs_EDGE):
        builder.Add(compound, edge)
    return compound


class RayCaster:
    """Exact ray-surface intersections against one shape."""

    def __init__(self, shape: TopoDS_Shape) -> None:
        self._hits = IntCurvesFace_ShapeIntersector()
        self._hits.Load(shape, 1e-7)

    def first_hit(self, origin: Point, direction: Point, start: float = 1e-4) -> float | None:
        """Distance along the ray to the first surface beyond `start`, or None."""
        line = gp_Lin(gp_Pnt(*origin), gp_Dir(*direction))
        self._hits.Perform(line, start, 1e6)
        if not self._hits.IsDone() or self._hits.NbPnt() == 0:
            return None
        return min(self._hits.WParameter(i) for i in range(1, self._hits.NbPnt() + 1))


def draft_degrees(normal: Point) -> float:
    """Angle between a face and the vertical pull direction (90 for a horizontal face)."""
    return math.degrees(math.asin(min(1.0, abs(normal[2]))))
