"""Exporters: a build as files someone can open, print, mold or rebuild, plus a build note.

`export_build()` writes, for one build:

| Format | File | What |
|---|---|---|
| `step` | `enclosure.step` | Each part (and the board) an exact labelled body; a design record |
| `stl` | `enclosure.stl` | Binary STL of every part (mm) |
| `3mf` | `enclosure.3mf` | One mesh object per part, named, in millimetres |
| `glb` | `enclosure.glb` | The contract mesh (`contracts/README.md`), face and edge IDs included |
| `obj` | `enclosure.obj` | One named object per part (mm) |
| `dxf` | `enclosure.dxf` | Flat profiles: each part cut just inside its top and bottom |
| `pdf` | `enclosure-drawing.pdf` | A sheet per part: four views, key dimensions, title, revisions |
| `source` | `enclosure.py`, `parameters.json` | The generator code and the parameters it ran with |
| `build_note` | `build-note.md` | Checks passed and failed, what was not tested, the files |

Exports are of the whole assembly by default; `parts` picks some of its parts (any of the
build's parts and `board`), so one part can be exported on its own. The board is only in the
STEP: the other formats are of the build's parts.

Meshes come from `geometry.tessellate`, so every mesh format has the same triangles the 3D canvas
shows. Coordinates are the board's, in millimetres, Z up (the GLB follows glTF and is Y up, as
the mesh contract says). The build note is written from the check report (`geometry.report`), so
it says exactly what the checks said.
"""

from __future__ import annotations

import base64
import json
import math
import re
import struct
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from OCP.BRepAlgoAPI import BRepAlgoAPI_Section
from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
from OCP.gp import gp_Dir, gp_Pln, gp_Pnt, gp_Trsf, gp_Vec

from app.boards import Board
from app.standards import StandardsFile
from geometry import occ
from geometry.assembly import BOARD_PART, board_shape
from geometry.files import ExportedFile, Revision
from geometry.interface import ENCLOSURE, BuildResult
from geometry.tessellate import PartMesh, Tessellation, tessellate

Vec = tuple[float, float, float]
STEM = "enclosure"


@dataclass(frozen=True)
class ExportFormat:
    name: str
    media_type: str
    description: str


FORMATS: dict[str, ExportFormat] = {
    f.name: f
    for f in (
        ExportFormat("step", "model/step", "Every part as a separate exact body (STEP AP214)"),
        ExportFormat("stl", "model/stl", "Binary STL mesh of every part"),
        ExportFormat("3mf", "model/3mf", "3MF mesh, one object per part"),
        ExportFormat("glb", "model/gltf-binary", "glTF binary with face and edge IDs"),
        ExportFormat("obj", "model/obj", "Wavefront OBJ, one object per part"),
        ExportFormat("dxf", "image/vnd.dxf", "Flat profiles of every part"),
        ExportFormat("pdf", "application/pdf", "Production drawing, a sheet per part"),
        ExportFormat("source", "text/x-python", "Generator code and its parameters"),
        ExportFormat("build_note", "text/markdown", "Checks passed, failed and not tested"),
    )
}

FILE_NAMES: dict[str, tuple[str, ...]] = {
    "step": (f"{STEM}.step",),
    "stl": (f"{STEM}.stl",),
    "3mf": (f"{STEM}.3mf",),
    "glb": (f"{STEM}.glb",),
    "obj": (f"{STEM}.obj",),
    "dxf": (f"{STEM}.dxf",),
    "pdf": (f"{STEM}-drawing.pdf",),
    "source": (f"{STEM}.py", "parameters.json"),
    "build_note": ("build-note.md",),
}


def export_build(
    board: Board | None,
    standards: StandardsFile,
    result: BuildResult,
    code: str,
    params: dict[str, Any],
    out_dir: Path | str,
    report: dict,
    mesh: Tessellation | None = None,
    formats: Iterable[str] | None = None,
    parts: Iterable[str] | None = None,
    history: Sequence[Revision] | None = None,
    units: str = "mm",
    face_ids: dict[str, list[str]] | None = None,
) -> list[ExportedFile]:
    """Write the build in every requested format (default: all) to `out_dir`.

    `code` and `params` are what produced `result`; `report` is its check report
    (`geometry.report.check_report`); `mesh` its tessellation (made here if not given).
    `parts` exports only those parts of the assembly (default: the whole assembly, board
    included); `board` names the board, which only the STEP carries. A design without a board
    (`board` None) exports its own parts only. `history` is the build history (oldest first)
    the drawing's revision table lists (`revision_table`).
    `units` (`mm` or `in`) is the length unit of the build note, the one file read by people
    rather than CAD tools; every other format stays in its native unit, millimetres.
    `face_ids` are the build's face IDs by part (`geometry.tessellate`), for a `mesh` made here:
    the IDs the session's mesh has, which the STEP file records for an import to keep.
    """
    if units not in NOTE_UNITS:
        raise ValueError(f"unknown units {units!r}; choose from {list(NOTE_UNITS)}")
    wanted = list(dict.fromkeys(formats if formats is not None else FORMATS))
    unknown = [f for f in wanted if f not in FORMATS]
    if unknown:
        raise ValueError(f"unknown export formats {unknown}; choose from {list(FORMATS)}")
    names = list(result.parts) + ([BOARD_PART] if board is not None else [])
    chosen = names if parts is None else list(dict.fromkeys(parts))
    unknown = [p for p in chosen if p not in names]
    if unknown or not chosen:
        raise ValueError(f"unknown parts {unknown}; choose from {names}")
    built = [p for p in chosen if p in result.parts]
    if not built and set(wanted) - {"step", "source", "build_note"}:
        raise ValueError("only the STEP carries the board; choose a part of the build too")
    if len(built) < len(result.parts):
        result = result.subset(built) if built else result
        if mesh is not None:
            mesh = Tessellation(
                {n: m for n, m in mesh.parts.items() if n in built}, mesh.counters, mesh.source
            )
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    if mesh is None and {"stl", "3mf", "glb", "obj", "step"} & set(wanted):
        mesh = tessellate(result, source={"build_id": report["build_id"]}, face_ids=face_ids)
    board_body = (
        {BOARD_PART: board_shape(board)} if board is not None and BOARD_PART in chosen else {}
    )

    writers = {
        "step": lambda: [write_step(board, result, params, built, board_body, mesh, out)],
        "stl": lambda: [write_stl(mesh, out / FILE_NAMES["stl"][0])],
        "3mf": lambda: [write_3mf(mesh, out / FILE_NAMES["3mf"][0])],
        "glb": lambda: [write_glb(mesh, out / FILE_NAMES["glb"][0])],
        "obj": lambda: [write_obj(mesh, out / FILE_NAMES["obj"][0])],
        "dxf": lambda: [write_dxf(result, standards, out / FILE_NAMES["dxf"][0])],
        "pdf": lambda: [write_drawing(board, result, report, out / FILE_NAMES["pdf"][0], history)],
        "source": lambda: write_source(code, params, out),
    }
    files: list[ExportedFile] = []
    for name in wanted:
        if name == "build_note":
            continue
        for path in writers[name]():
            files.append(ExportedFile(name, Path(path).name, _media_type(name, path), Path(path)))
    if "build_note" in wanted:
        note = out / FILE_NAMES["build_note"][0]
        note.write_text(build_note(board, result, report, files, units))
        files.append(ExportedFile("build_note", note.name, FORMATS["build_note"].media_type, note))
    return files


def write_step(
    board: Board | None,
    result: BuildResult,
    params: dict[str, Any],
    built: list[str],
    board_body: dict[str, Any],
    mesh: Tessellation,
    out: Path,
) -> Path:
    """The STEP file of `built` parts (and the board's bodies, if `board_body`), with the design
    record an import reads back (`geometry.step_manifest`): every part's material, kind,
    placement and face features, the features' tools, the board and the object it fits. So a
    design exported and imported again is the same build, and checks the same."""
    from geometry import step_manifest

    path = result.export_step(out / FILE_NAMES["step"][0], built, extra=board_body)
    fit_object = params.get("object") if isinstance(params.get("object"), dict) else None
    record = step_manifest.describe(
        result,
        built,
        (board.name, board.revision) if board is not None else None,
        BOARD_PART if board_body else None,
        fit_object,
        mesh,
    )
    step_manifest.embed(path, record)
    return path


def _media_type(fmt: str, path: Path) -> str:
    return "application/json" if Path(path).suffix == ".json" else FORMATS[fmt].media_type


# --- Meshes ------------------------------------------------------------------------------------


def welded(part: PartMesh, digits: int = 6) -> tuple[list[Vec], list[tuple[int, int, int]]]:
    """A part's triangles with the vertices faces share merged, so the mesh is closed.

    The contract mesh repeats a vertex for every face it touches (each face keeps its own ID);
    printing and molding formats want one vertex per point.
    """
    index: dict[Vec, int] = {}
    vertices: list[Vec] = []
    remap = []
    for p in part.positions:
        key = tuple(round(v, digits) + 0.0 for v in p)
        if key not in index:
            index[key] = len(vertices)
            vertices.append(tuple(float(v) for v in p))
        remap.append(index[key])
    triangles = []
    for i in range(0, len(part.indices), 3):
        a, b, c = (remap[part.indices[i + k]] for k in range(3))
        if a != b and b != c and a != c:
            triangles.append((a, b, c))
    return vertices, triangles


def _normal(a: Sequence[float], b: Sequence[float], c: Sequence[float]) -> Vec:
    u = [b[i] - a[i] for i in range(3)]
    v = [c[i] - a[i] for i in range(3)]
    n = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
    length = math.sqrt(sum(x * x for x in n)) or 1.0
    return (n[0] / length, n[1] / length, n[2] / length)


def write_stl(mesh: Tessellation, path: Path | str) -> Path:
    """Binary STL of every part, in millimetres."""
    path = Path(path)
    triangles = []
    for part in mesh.parts.values():
        vertices, tris = welded(part)
        triangles += [(vertices[a], vertices[b], vertices[c]) for a, b, c in tris]
    header = f"claude-hardware {STEM}: {', '.join(mesh.parts)} (mm)".encode()[:80]
    out = bytearray(header.ljust(80, b" "))
    out += struct.pack("<I", len(triangles))
    for a, b, c in triangles:
        out += struct.pack("<12fH", *_normal(a, b, c), *a, *b, *c, 0)
    path.write_bytes(bytes(out))
    return path


def write_obj(mesh: Tessellation, path: Path | str) -> Path:
    """Wavefront OBJ with one object per part, in millimetres."""
    path = Path(path)
    lines = [f"# claude-hardware {STEM}, units: mm"]
    offset = 1
    for name, part in mesh.parts.items():
        vertices, tris = welded(part)
        lines.append(f"o {name}")
        lines += [f"v {x:.6f} {y:.6f} {z:.6f}" for x, y, z in vertices]
        lines += [f"f {a + offset} {b + offset} {c + offset}" for a, b, c in tris]
        offset += len(vertices)
    path.write_text("\n".join(lines) + "\n")
    return path


_3MF_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
</Types>
"""
_3MF_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Target="/3D/3dmodel.model" Id="rel0" \
Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>
"""


def write_3mf(mesh: Tessellation, path: Path | str) -> Path:
    """3MF package with one named mesh object per part, in millimetres."""
    path = Path(path)
    objects, items = [], []
    for number, (name, part) in enumerate(mesh.parts.items(), start=1):
        vertices, tris = welded(part)
        vs = "".join(f'<vertex x="{x:.6f}" y="{y:.6f}" z="{z:.6f}"/>' for x, y, z in vertices)
        ts = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in tris)
        objects.append(
            f'<object id="{number}" name={quoteattr(name)} type="model"><mesh>'
            f"<vertices>{vs}</vertices><triangles>{ts}</triangles></mesh></object>"
        )
        items.append(f'<item objectid="{number}"/>')
    model = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<model unit="millimeter" xml:lang="en-US" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        f'<metadata name="Title">{escape(STEM)}</metadata>'
        f"<resources>{''.join(objects)}</resources><build>{''.join(items)}</build></model>\n"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _3MF_TYPES)
        z.writestr("_rels/.rels", _3MF_RELS)
        z.writestr("3D/3dmodel.model", model)
    return path


def write_glb(mesh: Tessellation, path: Path | str) -> Path:
    """The contract mesh as one binary glTF file (face and edge IDs kept)."""
    path = Path(path)
    doc = mesh.to_gltf()
    (buffer,) = doc["buffers"]
    uri = buffer.pop("uri")
    blob = base64.b64decode(uri.split(",", 1)[1])
    blob += b"\0" * (-len(blob) % 4)
    buffer["byteLength"] = len(blob)
    text = json.dumps(doc, separators=(",", ":")).encode()
    text += b" " * (-len(text) % 4)
    total = 12 + 8 + len(text) + 8 + len(blob)
    out = struct.pack("<4sII", b"glTF", 2, total)
    out += struct.pack("<I4s", len(text), b"JSON") + text
    out += struct.pack("<I4s", len(blob), b"BIN\0") + blob
    path.write_bytes(out)
    return path


# --- Flat profiles (DXF) -----------------------------------------------------------------------


def profile_planes(result: BuildResult, standards: StandardsFile) -> dict[str, float]:
    """Where each part is cut for its flat profiles: `<part>_bottom` and `<part>_top` -> z.

    Half the minimum wall inside the part's lowest and highest faces, so each cut runs through
    the plate or rim there (the openings and holes in it show).
    """
    inset = float(standards.get("min_wall").value) / 2
    planes = {}
    for name, shape in result.parts.items():
        (_, _, z0), (_, _, z1) = occ.bbox(shape)
        planes[f"{name}_bottom"] = z0 + inset
        planes[f"{name}_top"] = z1 - inset
    return planes


def profile(shape, z: float):
    """The edges where the plane at height `z` cuts `shape`, moved down to z = 0."""
    section = BRepAlgoAPI_Section(shape, gp_Pln(gp_Pnt(0, 0, z), gp_Dir(0, 0, 1)))
    section.Build()
    if not section.IsDone():
        raise RuntimeError(f"section at z {z} failed")
    move = gp_Trsf()
    move.SetTranslation(gp_Vec(0, 0, -z))
    return BRepBuilderAPI_Transform(section.Shape(), move, True).Shape()


def write_dxf(result: BuildResult, standards: StandardsFile, path: Path | str) -> Path:
    """Flat profiles of every part (`profile_planes`), one DXF layer each, in millimetres."""
    from build123d import Compound, ExportDXF, Unit

    path = Path(path)
    exporter = ExportDXF(unit=Unit.MM)
    for layer, z in profile_planes(result, standards).items():
        part = layer.rsplit("_", 1)[0]
        exporter.add_layer(layer)
        exporter.add_shape(Compound(profile(result.parts[part], z)).edges(), layer=layer)
    exporter.write(path)
    return path


# --- Drawing (PDF) -----------------------------------------------------------------------------


def revision_letter(index: int) -> str:
    """The drawing revision of the `index`-th build in the history (0-based): A, B, ..., Z, AA."""
    letters = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        letters = chr(ord("A") + rest) + letters
    return letters


def revision_table(
    board: Board | None, report: dict, history: Sequence[Revision] | None = None
) -> list[tuple[str, Revision]]:
    """The drawing's revisions, oldest first: the builds of `history` up to and including this
    one (`report["build_id"]`), each with its revision letter. A build missing from the history
    is added as its latest revision, dated today."""
    build_id = report["build_id"]
    rows = list(history or [])
    ids = [r.build_id for r in rows]
    if build_id in ids:
        rows = rows[: ids.index(build_id) + 1]
    else:
        what = f"{board.name} {board.revision}" if board is not None else "No board"
        rows.append(Revision(build_id, date.today(), what))
    return [(revision_letter(i), r) for i, r in enumerate(rows)]


WALL_RAYS = ((1, 0), (-1, 0), (0, 1), (0, -1))


def wall_thickness(shape) -> float:
    """The side wall of a molded part: rays from outside the part, square to each side, through
    the middle of its plan at half its height; the wall is the thinnest of the four first solid
    stretches they cross (exact ray-surface intersections)."""
    (x0, y0, z0), (x1, y1, z1) = occ.bbox(shape)
    centre = ((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2)
    rays = occ.RayCaster(shape)
    margin = 1.0
    walls = []
    for dx, dy in WALL_RAYS:
        reach = (x1 - x0) / 2 + margin if dx else (y1 - y0) / 2 + margin
        origin = (centre[0] - dx * reach, centre[1] - dy * reach, centre[2])
        enter = rays.first_hit(origin, (dx, dy, 0.0), 0.0)
        if enter is None:
            continue
        leave = rays.first_hit(origin, (dx, dy, 0.0), enter + 1e-6)
        if leave is not None:
            walls.append(leave - enter)
    if not walls:
        raise ValueError("no side wall found")
    return min(walls)


def key_dimensions(result: BuildResult, part: str) -> list[tuple[str, float]]:
    """The dimensions a part's drawing states, in millimetres: its overall size, its wall (a
    molded part), and the centre and size in plan of every opening on it (from the faces the cut
    created) and every boss (its tool where it is part of the part), in the board's
    coordinates."""
    shape = result.parts[part]
    (x0, y0, z0), (x1, y1, z1) = occ.bbox(shape)
    dims = [
        ("Overall length (X)", x1 - x0),
        ("Overall width (Y)", y1 - y0),
        ("Overall height (Z)", z1 - z0),
    ]
    if result.kind(part) == ENCLOSURE:
        dims.append(("Wall", wall_thickness(shape)))
    for tag in result.tags:
        feature = result.feature(tag)
        if feature is None or feature.part != part:
            continue
        if tag.startswith("boss_"):
            # The boss as built: its tool where it is part of the part (exact boolean).
            b = [occ.bbox(occ.common(t, shape)) for t in feature.tools]
        elif tag.startswith("opening_"):
            # The opening's walls: the faces the cut created (a drafted opening's full extent).
            b = [occ.bbox(f) for p, f in result.faces_of(tag) if p == part]
        else:
            continue
        if not b:
            continue
        (fx0, fy0), (fx1, fy1) = (
            (min(f[0][0] for f in b), min(f[0][1] for f in b)),
            (max(f[1][0] for f in b), max(f[1][1] for f in b)),
        )
        dims += [
            (f"{tag} centre X", (fx0 + fx1) / 2),
            (f"{tag} centre Y", (fy0 + fy1) / 2),
            (f"{tag} size X", fx1 - fx0),
            (f"{tag} size Y", fy1 - fy0),
        ]
    return dims


def _edge_lines(shape) -> list[list[Vec]]:
    """Every edge of a shape as a polyline (from its B-rep curve)."""
    from build123d import Shape

    lines = []
    for edge in Shape.cast(shape).edges():
        n = 2 if edge.geom_type.name == "LINE" else 24
        lines.append([tuple(edge.position_at(i / n)) for i in range(n + 1)])
    return lines


_COS30, _SIN30 = math.cos(math.radians(30)), math.sin(math.radians(30))


def _project(view: str, p: Sequence[float]) -> tuple[float, float]:
    """A point in a view's 2D drawing plane (third-angle projection; isometric from +X-Y+Z)."""
    x, y, z = p
    if view == "top":
        return x, y
    if view == "front":
        return x, z
    if view == "side":
        return y, z
    return (x + y) * _COS30, (y - x) * _SIN30 + z


DRAWING_VIEWS = {
    # name: (title, axes rectangle on the A4 landscape sheet)
    "top": ("Top", (0.03, 0.47, 0.36, 0.43)),
    "front": ("Front", (0.03, 0.08, 0.36, 0.33)),
    "side": ("Right side", (0.42, 0.08, 0.20, 0.33)),
    "iso": ("Isometric", (0.42, 0.47, 0.20, 0.43)),
}


def write_drawing(
    board: Board | None,
    result: BuildResult,
    report: dict,
    path: Path | str,
    history: Sequence[Revision] | None = None,
) -> Path:
    """The production drawing: one A4 sheet per part with its front, top, side and isometric
    views, its key dimensions (`key_dimensions`), a title block (part, material, revision, date,
    units) and the revision table from the build history (`revision_table`)."""
    import matplotlib
    from matplotlib.backends.backend_pdf import PdfPages
    from matplotlib.figure import Figure

    path = Path(path)
    revisions = revision_table(board, report, history)
    with (
        matplotlib.rc_context({"pdf.fonttype": 42, "font.size": 7}),
        PdfPages(path, metadata={"Title": f"{STEM} drawings"}) as pdf,
    ):
        for part, shape in result.parts.items():
            fig = Figure(figsize=(11.69, 8.27))  # A4 landscape
            _drawing_sheet(fig, board, result, report, part, shape, revisions)
            pdf.savefig(fig)
    return path


def _drawing_sheet(fig, board, result, report, part, shape, revisions) -> None:
    lines = _edge_lines(shape)
    (x0, y0, z0), (x1, y1, z1) = occ.bbox(shape)
    lo, hi = (x0, y0, z0), (x1, y1, z1)
    fig.text(0.03, 0.955, f"{part}: production drawing", fontsize=12, weight="bold")
    fig.text(
        0.03,
        0.93,
        "Dimensions in mm, board coordinates. Third-angle projection. "
        "Untoleranced: GD&T not in scope.",
    )
    axes_of = {"top": (0, 1), "front": (0, 2), "side": (1, 2)}
    for view, (title, rect) in DRAWING_VIEWS.items():
        ax = fig.add_axes(rect)
        ax.set_title(title)
        ax.set_aspect("equal")
        ax.axis("off")
        for line in lines:
            pts = [_project(view, p) for p in line]
            ax.plot([p[0] for p in pts], [p[1] for p in pts], color="#1f3b73", lw=0.4)
        if view in axes_of:
            u, v = axes_of[view]
            _dimension(ax, (lo[u], lo[v]), (hi[u], lo[v]), hi[u] - lo[u], True, hi[v] - lo[v])
            _dimension(ax, (hi[u], lo[v]), (hi[u], hi[v]), hi[v] - lo[v], False, hi[u] - lo[u])

    dims = key_dimensions(result, part)
    table = fig.add_axes((0.66, 0.40, 0.31, 0.52))
    table.axis("off")
    table.set_title("Key dimensions (mm)")
    rows = max(len(dims), 30)
    size = min(7.0, 400 / rows)
    for i, (label, value) in enumerate(dims):
        y = 1 - (i + 0.5) / rows
        table.text(0, y, label, fontsize=size, va="center")
        table.text(1, y, f"{value:.2f}", fontsize=size, va="center", ha="right")

    shown = revisions[-8:]  # the latest revisions fit the sheet
    rev = fig.add_axes((0.66, 0.20, 0.31, 0.17))
    rev.axis("off")
    rev.set_title("Revisions")
    step = 1 / 9
    for j, head in enumerate(("Rev", "Date", "Build", "Description")):
        rev.text((0, 0.08, 0.28, 0.55)[j], 1 - step / 2, head, weight="bold", va="center")
    for i, (letter, r) in enumerate(shown):
        y = 1 - (i + 1.5) * step
        cells = (letter, r.date.isoformat(), r.build_id, r.description)
        for j, cell in enumerate(cells):
            rev.text((0, 0.08, 0.28, 0.55)[j], y, cell, va="center", fontsize=6)

    letter, current = revisions[-1]
    block = fig.add_axes((0.66, 0.03, 0.31, 0.15))
    block.set_xticks([])
    block.set_yticks([])
    block.set_title("Title block")
    material = result.materials.get(part, "unspecified")
    fields = [
        ("Part", part),
        ("Material", material),
        ("Revision", letter),
        ("Date", current.date.isoformat()),
        ("Units", "mm"),
        ("Board", f"{board.name} {board.revision}" if board is not None else "None"),
        ("Build", report["build_id"]),
    ]
    for i, (label, value) in enumerate(fields):
        y = 1 - (i + 0.5) / len(fields)
        block.text(0.03, y, label, va="center", weight="bold")
        block.text(0.30, y, value, va="center")


def _dimension(ax, a, b, value: float, below: bool, span: float) -> None:
    """A dimension line from a to b, offset outside the view, with its value."""
    gap = 0.12 * max(span, value, 1.0)
    if below:
        (x0, y), (x1, _) = a, b
        y -= gap
        ax.plot([x0, x1], [y, y], color="black", lw=0.5, marker="|", markersize=6)
        ax.text((x0 + x1) / 2, y, f"{value:.2f}", ha="center", va="top")
    else:
        (x, y0), (_, y1) = a, b
        x += gap
        ax.plot([x, x], [y0, y1], color="black", lw=0.5, marker="_", markersize=6)
        ax.text(x, (y0 + y1) / 2, f"{value:.2f}", ha="left", va="center", rotation=90)


# --- Source ------------------------------------------------------------------------------------


def write_source(code: str, params: dict[str, Any], out: Path) -> list[Path]:
    """The generator code and the parameters it ran with: `build(params)` rebuilds the model."""
    code_path, params_path = (out / n for n in FILE_NAMES["source"])
    code_path.write_text(code if code.endswith("\n") else code + "\n")
    params_path.write_text(json.dumps(params, indent=2, sort_keys=True) + "\n")
    return [code_path, params_path]


# --- Build note --------------------------------------------------------------------------------

NOTE_SECTIONS = {
    "pass": "Checks passed",
    "fail": "Checks failed",
    "untested": "Not tested",
    "not_applicable": "Not applicable",
}

MM_PER_INCH = 25.4
NOTE_UNITS = ("mm", "in")
# Check units that are lengths, and the power of the length in each.
_LENGTH_POWER = {"mm": 1, "mm2": 2, "mm3": 3}
_IN_UNIT = {"mm": "in", "mm2": "in2", "mm3": "in3"}
# A length written in text: an optional sign, the number, then mm (not mm2 or mm3).
_MM_TEXT = re.compile(r"(?<![\w.])([±+-]?)(\d+(?:\.\d+)?)\s?mm\b(?![²³23])")


def _inches(mm: float, power: int = 1) -> str:
    """A millimetre value (of a length, area or volume) in inches, with enough digits."""
    value = mm / MM_PER_INCH**power
    return f"{value:.4g}" if abs(value) < 1 else f"{value:.3f}".rstrip("0").rstrip(".")


def lengths_in(text: str, units: str) -> str:
    """`text` with every length written in mm converted to `units`."""
    if units == "mm":
        return text
    return _MM_TEXT.sub(lambda m: f"{m[1]}{_inches(float(m[2]))} in", text)


def _in_units(v: dict | None, units: str) -> dict | None:
    """A measured or required value in `units`: lengths, areas and volumes convert."""
    if not v or units == "mm" or v.get("unit") not in _LENGTH_POWER:
        return v
    value = v.get("value")
    if not isinstance(value, int | float):
        return v
    return {
        **v,
        "value": float(_inches(value, _LENGTH_POWER[v["unit"]])),
        "unit": _IN_UNIT[v["unit"]],
    }


def _cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _value(v: dict | None) -> str:
    if not v:
        return ""
    value = v.get("value")
    text = f"{value:g}" if isinstance(value, int | float) else str(value)
    unit = f" {v['unit']}" if v.get("unit") else ""
    kind = v.get("kind")
    if kind == "tolerance":
        return f"±{text}{unit}"
    return f"{kind} {text}{unit}" if kind in ("min", "max") else f"{text}{unit}"


def build_note(
    board: Board | None,
    result: BuildResult,
    report: dict,
    files: Sequence[ExportedFile],
    units: str = "mm",
) -> str:
    """The build note (Markdown): every check in the report under passed, failed or not tested,
    with its measurement and limit, and the exported files. Lengths are in `units` (mm or in),
    in the tables and in the text; the geometry files stay in millimetres."""
    summary = report["summary"]
    native = "" if units == "mm" else " The geometry files are in mm."
    lines = [
        f"# Build note: {report['build_id']}",
        "",
        (f"Board: {board.name} {board.revision}. " if board else "No board. ")
        + f"Geometry backend: {result.backend}. Units: {units}.{native}",
        "",
        f"{summary['pass']} checks passed, {summary['fail']} failed, "
        f"{summary['untested']} not tested"
        + (
            f", {summary['not_applicable']} not applicable."
            if summary.get("not_applicable")
            else "."
        ),
    ]
    for status, heading in NOTE_SECTIONS.items():
        rows = [r for r in report["results"] if r["status"] == status]
        if status == "not_applicable" and not rows:
            continue  # only a design some standards don't apply to (no board, no object)
        lines += ["", f"## {heading}", ""]
        if not rows:
            lines.append("None.")
            continue
        if status == "not_applicable":
            lines += ["| ID | Check | Why it does not apply |", "|---|---|---|"]
            lines += [
                f"| {_cell(r['id'])} | {_cell(r['title'])} | {_cell(r['reason'])} |" for r in rows
            ]
            continue
        if status == "untested":
            lines += ["| ID | Check | What is not covered |", "|---|---|---|"]
            for r in rows:
                covers = lengths_in(r["covers"], units)
                lines.append(f"| {_cell(r['id'])} | {_cell(r['title'])} | {_cell(covers)} |")
            continue
        lines += [
            "| ID | Check | Standard | Measured | Required | Summary |",
            "|---|---|---|---|---|---|",
        ]
        for r in rows:
            lines.append(
                "| "
                + " | ".join(
                    _cell(x)
                    for x in (
                        r["id"],
                        r["title"],
                        r["standard"] or "",
                        _value(_in_units(r["measured"], units)),
                        _value(_in_units(r["required"], units)),
                        lengths_in(r["summary"], units),
                    )
                )
                + " |"
            )
    lines += ["", "## Files", "", "| File | Format | What |", "|---|---|---|"]
    for f in files:
        what = FORMATS[f.format].description
        if f.format == "pdf":
            what += f" ({', '.join(result.parts)})"
        lines.append(f"| {_cell(f.name)} | {f.format} | {_cell(what)} |")
    lines.append(f"| {FILE_NAMES['build_note'][0]} | build_note | This note |")
    lines += [
        "",
        f"To rebuild: run `{FILE_NAMES['source'][0]}` with the {result.backend} backend, calling "
        f"`build(params)` with the contents of `{FILE_NAMES['source'][1]}`.",
    ]
    return "\n".join(lines) + "\n"


__all__ = [
    "FILE_NAMES",
    "FORMATS",
    "ExportedFile",
    "Revision",
    "build_note",
    "export_build",
    "key_dimensions",
    "lengths_in",
    "profile",
    "profile_planes",
    "revision_letter",
    "revision_table",
    "wall_thickness",
    "write_drawing",
    "welded",
]
