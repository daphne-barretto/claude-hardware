"""The geometry backend interface: code and parameters in; shapes, feature tags and STEP out.

A backend wraps one CAD library (build123d or CadQuery). It runs generator code written for
that library and converts what the code returns into plain OpenCascade shapes. Everything
downstream (face tags, checks, maps, exports) works on those shapes and never needs to know
which backend made them.

The code contract, the same for every backend (each backend's `prompt.md` restates it in that
library's terms):

- The code defines `build(params: dict) -> dict`.
- The returned dict has `parts` (part name -> solid), `features` (a list, in the order the
  features were applied, of `{"tag", "part", "tools"}`; `tools` are the solids the feature
  added or cut) and `materials` (part name -> material). Optional: `kinds` (part name ->
  `enclosure`, the default, or `insert` for bought-in hardware such as heat-set inserts) and
  `placements` (part name -> `[x, y, z]`, where the part's own origin sits in the assembly;
  the solids themselves are always returned in place, in board coordinates).
- The parts are an assembly: each part has a name, a material and a placement, and checks run
  between parts. The board itself joins the assembly as the part `board` (`geometry.assembly`).
- Feature tags are stable names derived from the board data, such as `opening_Q4` or `boss_1`.

While the code runs, the backend records the lines that created each feature (see
`geometry.sites`): `Feature.lines`.
"""

from __future__ import annotations

import traceback
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

from OCP.TopoDS import TopoDS_Shape

from geometry import occ
from geometry.parts import (  # noqa: F401 - re-exported
    BOARD,
    ENCLOSURE,
    INSERT,
    ORIGIN,
    PART_KINDS,
    PartKind,
    Placement,
)
from geometry.sites import HOOK, FeatureSites, Lines, instrument

CODE_FILENAME = "<build>"


class BuildError(Exception):
    """Generator code failed. `line` is the line in the code where it failed, if known."""

    def __init__(self, message: str, line: int | None = None) -> None:
        super().__init__(f"line {line}: {message}" if line else message)
        self.message = message
        self.line = line


@dataclass(frozen=True)
class Feature:
    tag: str
    part: str
    tools: tuple[TopoDS_Shape, ...]
    lines: Lines | None = None
    """First and last line (1-based, inclusive) of the statement in the code that created it."""


@dataclass(frozen=True)
class FaceRef:
    """One face of a part: its index in `occ.faces(part)` and the feature that created it."""

    part: str
    index: int
    feature: str


@dataclass
class BuildResult:
    backend: str
    parts: dict[str, TopoDS_Shape]
    features: list[Feature]
    materials: dict[str, str] = field(default_factory=dict)
    kinds: dict[str, PartKind] = field(default_factory=dict)
    """Part name -> kind; a part not listed is `enclosure`."""
    placements: dict[str, Placement] = field(default_factory=dict)
    """Part name -> where its origin sits in the assembly; a part not listed is at the origin."""

    def kind(self, part: str) -> PartKind:
        return self.kinds.get(part, ENCLOSURE)

    def placement(self, part: str) -> Placement:
        return self.placements.get(part, ORIGIN)

    @property
    def enclosure(self) -> dict[str, TopoDS_Shape]:
        """The molded parts (base, lid): the parts the molding standards apply to."""
        return {n: s for n, s in self.parts.items() if self.kind(n) == ENCLOSURE}

    @property
    def tags(self) -> list[str]:
        return list(dict.fromkeys(f.tag for f in self.features))

    def subset(self, parts: list[str]) -> BuildResult:
        """The same build with only the named parts (and their features)."""
        unknown = [p for p in parts if p not in self.parts]
        if unknown:
            raise ValueError(f"unknown parts {unknown}; the build has {list(self.parts)}")
        keep = set(parts)
        return BuildResult(
            self.backend,
            {n: s for n, s in self.parts.items() if n in keep},
            [f for f in self.features if f.part in keep],
            {n: m for n, m in self.materials.items() if n in keep},
            {n: k for n, k in self.kinds.items() if n in keep},
            {n: at for n, at in self.placements.items() if n in keep},
        )

    def feature_sources(self) -> list[dict[str, Any]]:
        """`{tag, part, lines}` for every feature in the order applied (the session API's
        `BuildSource.features`). Raises BuildError if a feature's lines weren't recorded."""
        out = []
        for f in self.features:
            if f.lines is None:
                raise BuildError(f"feature {f.tag!r}: the code that created it wasn't found")
            out.append({"tag": f.tag, "part": f.part, "lines": list(f.lines)})
        return out

    def feature(self, tag: str) -> Feature | None:
        return next((f for f in self.features if f.tag == tag), None)

    @cached_property
    def face_refs(self) -> dict[str, list[FaceRef]]:
        """Every face of every part with the feature that created it."""
        return {name: _attribute(name, shape, self.features) for name, shape in self.parts.items()}

    def faces_of(self, tag: str) -> list[tuple[str, Any]]:
        """(part, face) pairs for every face a feature created."""
        out = []
        for name, refs in self.face_refs.items():
            part_faces = occ.faces(self.parts[name])
            out.extend((name, part_faces[r.index]) for r in refs if r.feature == tag)
        return out

    def export_step(
        self,
        path: Path | str,
        parts: list[str] | None = None,
        extra: dict[str, TopoDS_Shape] | None = None,
    ) -> Path:
        """Write parts to one STEP file, each part a separate body labelled with its name.

        `parts` picks the parts (default: all); `extra` adds more named shapes (the board).
        """
        from build123d import Compound, Shape, export_step

        shapes = {n: s for n, s in self.parts.items() if parts is None or n in parts}
        shapes.update(extra or {})
        if not shapes:
            raise ValueError("no parts to export")
        children = []
        for name, shape in shapes.items():
            body = Shape.cast(shape)
            body.label = name
            children.append(body)
        path = Path(path)
        if not export_step(Compound(children=children), path):
            raise RuntimeError(f"STEP export to {path} failed")
        return path


def _attribute(part: str, shape: TopoDS_Shape, features: list[Feature]) -> list[FaceRef]:
    """Which feature created each face of a part.

    A face belongs to the last-applied feature whose tool has a face through the face's
    interior point: a cut's walls lie on the cutter, an added boss's walls lie on the boss.
    Exact point-to-shape distances on the B-rep, so it works for any backend's shapes.
    """
    mine = [f for f in features if f.part == part]
    refs = []
    for index, face in enumerate(occ.faces(shape)):
        point = occ.vertex(occ.interior_point(face))
        tag = "unknown"
        for f in reversed(mine):
            if any(_on_boundary(point, tool) for tool in f.tools):
                tag = f.tag
                break
        refs.append(FaceRef(part, index, tag))
    return refs


def _on_boundary(point: TopoDS_Shape, tool: TopoDS_Shape, tol: float = 1e-5) -> bool:
    for face in occ.faces(tool):
        d, _ = occ.distance(point, face)
        if d <= tol:
            return True
    return False


class GeometryBackend(ABC):
    """One CAD library behind the common interface."""

    name: str

    @property
    def directory(self) -> Path:
        return Path(__file__).parent / "backends" / self.name

    def prompt(self) -> str:
        """How to write generator code for this backend, for Claude."""
        return (self.directory / "prompt.md").read_text()

    def examples(self) -> dict[str, str]:
        """Example generator code for this backend, by name."""
        return {p.stem: p.read_text() for p in sorted((self.directory / "examples").glob("*.py"))}

    def example(self, name: str) -> str:
        return self.examples()[name]

    @abstractmethod
    def to_shape(self, obj: Any) -> TopoDS_Shape:
        """Convert one of this library's objects to an OpenCascade shape."""

    def build(self, code: str, params: dict) -> BuildResult:
        """Run generator code with parameters and return its parts and features.

        Runs in this process. Untrusted code must go through the sandboxed build runner.
        """
        try:
            sites = instrument(code, CODE_FILENAME)
            compiled = compile(sites.tree, CODE_FILENAME, "exec")
        except SyntaxError as error:
            raise BuildError(f"syntax error: {error.msg}", error.lineno) from error
        namespace: dict[str, Any] = {"__name__": "geometry_build", HOOK: sites.hook}
        try:
            exec(compiled, namespace)
            build = namespace.get("build")
            if not callable(build):
                raise BuildError("the code must define build(params)")
            output = build(params)
        except BuildError:
            raise
        except Exception as error:
            raise BuildError(f"{type(error).__name__}: {error}", _code_line(error)) from error
        return self._result(output, sites)

    def _result(self, output: Any, sites: FeatureSites | None = None) -> BuildResult:
        if not isinstance(output, dict) or "parts" not in output or "features" not in output:
            raise BuildError("build(params) must return a dict with 'parts' and 'features'")
        parts = {str(name): self.to_shape(obj) for name, obj in output["parts"].items()}
        features = []
        for f in output["features"]:
            if f["part"] not in parts:
                raise BuildError(f"feature {f['tag']!r} names unknown part {f['part']!r}")
            tools = tuple(self.to_shape(t) for t in f["tools"])
            lines = sites.lines(f) if sites else None
            features.append(Feature(str(f["tag"]), str(f["part"]), tools, lines))
        materials = {str(k): str(v) for k, v in output.get("materials", {}).items()}
        kinds = {str(k): str(v) for k, v in output.get("kinds", {}).items()}
        placements = {}
        for name, at in output.get("placements", {}).items():
            try:
                x, y, z = (float(v) for v in at)
            except (TypeError, ValueError):
                raise BuildError(f"placement of {name!r} must be [x, y, z]") from None
            placements[str(name)] = (x, y, z)
        for name in [*materials, *kinds, *placements]:
            if name not in parts:
                raise BuildError(f"materials, kinds or placements name unknown part {name!r}")
        for name, kind in kinds.items():
            if kind not in (ENCLOSURE, INSERT):
                raise BuildError(f"part {name!r}: kind must be {ENCLOSURE!r} or {INSERT!r}")
        if BOARD in parts:
            raise BuildError(f"{BOARD!r} is the board's part name; the board is added for you")
        return BuildResult(self.name, parts, features, materials, kinds, placements)


def _code_line(error: BaseException) -> int | None:
    line = None
    for frame in traceback.extract_tb(error.__traceback__):
        if frame.filename == CODE_FILENAME:
            line = frame.lineno
    return line
