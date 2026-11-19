"""Convert a KiCad 5 or 6+ `.kicad_pcb` file into the board JSON schema.

KiCad files carry no component heights and no notion of what the enclosure must leave open,
so those come from `demos/<board>/source/footprints.json`, keyed by footprint. The list of
revisions to convert lives in `demos/<board>/source/revisions.json`.

Regenerate every board file:

    cd backend && .venv/bin/python -m app.boards.kicad

Check that the committed files are up to date (exit status 1 if not):

    cd backend && .venv/bin/python -m app.boards.kicad --check
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.boards.schema import SCHEMA_VERSION, Board

SExpr = list[Any]

_TOKEN = re.compile(r'\s*(\(|\)|"(?:[^"\\]|\\.)*"|[^\s()]+)')
_EPS = 1e-3
_ARC_STEP = math.radians(5)


def parse_sexpr(text: str) -> SExpr:
    stack: list[SExpr] = [[]]
    for match in _TOKEN.finditer(text):
        tok = match.group(1)
        if tok == "(":
            stack.append([])
        elif tok == ")":
            node = stack.pop()
            stack[-1].append(node)
        elif tok.startswith('"'):
            stack[-1].append(tok[1:-1].replace('\\"', '"'))
        else:
            stack[-1].append(tok)
    if len(stack) != 1 or len(stack[0]) != 1:
        raise ValueError("unbalanced s-expression")
    return stack[0][0]


def _children(node: SExpr, key: str) -> list[SExpr]:
    return [x for x in node if isinstance(x, list) and x and x[0] == key]


def _child(node: SExpr, key: str) -> SExpr | None:
    found = _children(node, key)
    return found[0] if found else None


def _xy(node: SExpr | None) -> tuple[float, float]:
    assert node is not None
    return float(node[1]), float(node[2])


def _layers(node: SExpr) -> list[str]:
    layer = _child(node, "layer")
    if layer is not None:
        return [layer[1]]
    layers = _child(node, "layers")
    return list(layers[1:]) if layers is not None else []


# --- Geometry helpers (all in KiCad coordinates: mm, y pointing down) -----------------------


def _rotate(x: float, y: float, degrees: float) -> tuple[float, float]:
    """Rotate a vector as KiCad does: positive angles turn counter-clockwise on screen."""
    a = math.radians(degrees)
    return x * math.cos(a) + y * math.sin(a), -x * math.sin(a) + y * math.cos(a)


def _arc_points_center(center, start, angle_deg: float) -> list[tuple[float, float]]:
    """KiCad 5 arc: centre, start point and sweep (positive is clockwise on screen)."""
    cx, cy = center
    dx, dy = start[0] - cx, start[1] - cy
    sweep = math.radians(angle_deg)
    n = max(2, math.ceil(abs(sweep) / _ARC_STEP))
    pts = []
    for i in range(n + 1):
        a = sweep * i / n
        pts.append(
            (cx + dx * math.cos(a) - dy * math.sin(a), cy + dx * math.sin(a) + dy * math.cos(a))
        )
    return pts


def _arc_points_three(start, mid, end) -> list[tuple[float, float]]:
    """KiCad 6+ arc: start, a point on the arc, end."""
    (ax, ay), (bx, by), (cx, cy) = start, mid, end
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    ux = (
        (ax * ax + ay * ay) * (by - cy)
        + (bx * bx + by * by) * (cy - ay)
        + (cx * cx + cy * cy) * (ay - by)
    ) / d
    uy = (
        (ax * ax + ay * ay) * (cx - bx)
        + (bx * bx + by * by) * (ax - cx)
        + (cx * cx + cy * cy) * (bx - ax)
    ) / d
    a0 = math.atan2(ay - uy, ax - ux)
    am = math.atan2(by - uy, bx - ux)
    a1 = math.atan2(cy - uy, cx - ux)

    def ccw(frm: float, to: float) -> float:
        return (to - frm) % (2 * math.pi)

    sweep = ccw(a0, a1)
    if ccw(a0, am) > sweep:  # the mid point is on the other way round
        sweep -= 2 * math.pi
    r = math.hypot(ax - ux, ay - uy)
    n = max(2, math.ceil(abs(sweep) / _ARC_STEP))
    pts = [
        (ux + r * math.cos(a0 + sweep * i / n), uy + r * math.sin(a0 + sweep * i / n))
        for i in range(n + 1)
    ]
    pts[0], pts[-1] = start, end
    return pts


def _chain(segments: list[list[tuple[float, float]]]) -> list[tuple[float, float]]:
    """Join open polylines end to end into one closed loop."""
    remaining = [list(s) for s in segments]
    loop = remaining.pop(0)

    def close(p, q) -> bool:
        return math.dist(p, q) < _EPS

    while remaining:
        for i, seg in enumerate(remaining):
            if close(seg[0], loop[-1]):
                loop.extend(seg[1:])
            elif close(seg[-1], loop[-1]):
                loop.extend(reversed(seg[:-1]))
            else:
                continue
            remaining.pop(i)
            break
        else:
            raise ValueError("board outline is not a single closed loop")
    if not close(loop[0], loop[-1]):
        raise ValueError("board outline does not close")
    return loop[:-1]


# --- Board-level extraction ------------------------------------------------------------------


@dataclass
class _Outline:
    loop: list[tuple[float, float]]
    holes: list[tuple[tuple[float, float], float]]  # centre, diameter


def _outline(pcb: SExpr) -> _Outline:
    segments: list[list[tuple[float, float]]] = []
    holes: list[tuple[tuple[float, float], float]] = []
    for node in pcb:
        if not (isinstance(node, list) and node and str(node[0]).startswith("gr_")):
            continue
        if "Edge.Cuts" not in _layers(node):
            continue
        kind = node[0]
        if kind == "gr_line":
            segments.append([_xy(_child(node, "start")), _xy(_child(node, "end"))])
        elif kind == "gr_arc" and _child(node, "mid") is not None:
            segments.append(
                _arc_points_three(
                    _xy(_child(node, "start")), _xy(_child(node, "mid")), _xy(_child(node, "end"))
                )
            )
        elif kind == "gr_arc":
            angle = _child(node, "angle")
            assert angle is not None
            segments.append(
                _arc_points_center(
                    _xy(_child(node, "start")), _xy(_child(node, "end")), float(angle[1])
                )
            )
        elif kind == "gr_circle":
            center = _xy(_child(node, "center"))
            holes.append((center, 2 * math.dist(center, _xy(_child(node, "end")))))
        else:
            raise ValueError(f"unsupported Edge.Cuts item {kind}")
    return _Outline(_chain(segments), holes)


@dataclass
class _Footprint:
    lib_id: str
    ref: str
    value: str
    at: tuple[float, float]
    rotation: float
    side: str
    bbox: tuple[float, float, float, float]  # footprint-local, KiCad axes


_BODY_LAYERS = ("CrtYd", "Fab", "SilkS")


def _footprint(node: SExpr) -> _Footprint:
    at = _child(node, "at")
    assert at is not None
    x, y = float(at[1]), float(at[2])
    rotation = float(at[3]) if len(at) > 3 else 0.0
    layer = _child(node, "layer")
    assert layer is not None
    side = "bottom" if layer[1].startswith("B.") else "top"
    ref = value = ""
    for item in node:
        if not isinstance(item, list) or len(item) < 3:
            continue
        if item[0] == "fp_text" and item[1] in ("reference", "value"):
            ref, value = (item[2], value) if item[1] == "reference" else (ref, item[2])
        elif item[0] == "property" and item[1] in ("Reference", "Value"):
            ref, value = (item[2], value) if item[1] == "Reference" else (ref, item[2])
    return _Footprint(node[1], ref, value, (x, y), rotation, side, _body_bbox(node, rotation))


def _graphic_points(item: SExpr) -> list[tuple[float, float]]:
    pts = [_xy(_child(item, k)) for k in ("start", "end", "mid") if _child(item, k) is not None]
    if item[0] == "fp_circle":
        c, e = _xy(_child(item, "center")), _xy(_child(item, "end"))
        r = math.dist(c, e)
        pts = [(c[0] - r, c[1] - r), (c[0] + r, c[1] + r)]
    for poly in _children(item, "pts"):
        pts.extend(_xy(xy) for xy in _children(poly, "xy"))
    return pts


def _pad_points(pad: SExpr, fp_rotation: float) -> list[tuple[float, float]]:
    at = _child(pad, "at")
    size = _child(pad, "size")
    assert at is not None and size is not None
    px, py = float(at[1]), float(at[2])
    # Pad angles in the file include the footprint rotation.
    local_rot = (float(at[3]) if len(at) > 3 else 0.0) - fp_rotation
    w, h = float(size[1]) / 2, float(size[2]) / 2
    corners = [_rotate(sx * w, sy * h, local_rot) for sx in (-1, 1) for sy in (-1, 1)]
    return [(px + cx, py + cy) for cx, cy in corners]


def _body_bbox(node: SExpr, rotation: float) -> tuple[float, float, float, float]:
    """Footprint-local bounding box of the body: courtyard, else fab, else silkscreen, else pads."""
    by_layer: dict[str, list[tuple[float, float]]] = {}
    for item in node:
        if (
            isinstance(item, list)
            and item
            and item[0] in ("fp_line", "fp_rect", "fp_circle", "fp_arc", "fp_poly")
        ):
            for lay in _layers(item):
                suffix = lay.split(".", 1)[-1]
                by_layer.setdefault(suffix, []).extend(_graphic_points(item))
    pads = [p for pad in _children(node, "pad") for p in _pad_points(pad, rotation)]
    for suffix in _BODY_LAYERS:
        if by_layer.get(suffix):
            pts = by_layer[suffix]
            break
    else:
        pts = pads
    if not pts:
        raise ValueError(f"footprint {node[1]} has no geometry")
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


# --- Conversion ------------------------------------------------------------------------------


def _r(v: float) -> float:
    return round(v + 0.0, 3) + 0.0


def _norm_angle(deg: float) -> float:
    a = _r(deg % 360)
    return 0.0 if a >= 360 else a


def convert(pcb_text: str, revision: dict[str, Any], footprints: dict[str, Any]) -> dict[str, Any]:
    pcb = parse_sexpr(pcb_text)
    if pcb[0] != "kicad_pcb":
        raise ValueError("not a KiCad board file")
    general = _child(pcb, "general")
    thickness_node = _child(general, "thickness") if general is not None else None
    thickness = float(thickness_node[1]) if thickness_node is not None else 1.6

    outline = _outline(pcb)
    ox = min(p[0] for p in outline.loop)
    oy = max(p[1] for p in outline.loop)

    def to_board(x: float, y: float) -> tuple[float, float]:
        return _r(x - ox), _r(oy - y)

    loop = [to_board(*p) for p in outline.loop]
    area = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(loop, loop[1:] + loop[:1], strict=True))
    if area < 0:
        loop.reverse()
    start = min(range(len(loop)), key=lambda i: (loop[i][1], loop[i][0]))
    loop = loop[start:] + loop[:start]

    holes = sorted((to_board(*c), _r(d)) for c, d in outline.holes)
    mounting_holes = [
        {"id": f"H{i}", "position": list(c), "diameter": d} for i, (c, d) in enumerate(holes, 1)
    ]

    components: list[dict[str, Any]] = []
    openings: list[dict[str, Any]] = []
    for node in pcb:
        if not (isinstance(node, list) and node and node[0] in ("module", "footprint")):
            continue
        fp = _footprint(node)
        meta = footprints.get(fp.lib_id)
        if meta is None:
            raise KeyError(f"footprint {fp.lib_id} ({fp.ref}) missing from footprints.json")
        if meta.get("ignore"):
            continue
        offset = float(meta.get("rotation_offset", 0))
        if offset % 90:
            raise ValueError(f"{fp.lib_id}: rotation_offset must be a multiple of 90")
        x0, y0, x1, y1 = fp.bbox
        lx, ly = _rotate((x0 + x1) / 2, (y0 + y1) / 2, fp.rotation)
        position = to_board(fp.at[0] + lx, fp.at[1] + ly)
        w, h = x1 - x0, y1 - y0
        if offset % 180:
            w, h = h, w
        rotation = _norm_angle(fp.rotation + offset)
        components.append(
            {
                "ref": fp.ref,
                "value": fp.value,
                "footprint": fp.lib_id,
                "role": meta.get("role"),
                "side": fp.side,
                "position": list(position),
                "rotation": rotation,
                "size": [_r(w), _r(h)],
                "height": float(meta["height"]),
            }
        )
        for spec in meta.get("openings", []):
            dx, dy = spec.get("offset", (0.0, 0.0))
            a = math.radians(rotation)
            pos = (
                position[0] + dx * math.cos(a) - dy * math.sin(a),
                position[1] + dx * math.sin(a) + dy * math.cos(a),
            )
            size = spec.get("size", (w, h))
            openings.append(
                {
                    "id": f"{fp.ref}-{spec['kind']}",
                    "kind": spec["kind"],
                    "component": fp.ref,
                    "side": spec.get("side", fp.side),
                    "position": [_r(pos[0]), _r(pos[1])],
                    "rotation": rotation,
                    "size": [_r(size[0]), _r(size[1])],
                }
            )

    components.sort(key=_ref_key)
    openings.sort(key=lambda o: (_ref_key({"ref": o["component"]}), o["kind"]))
    board = {
        "schema_version": SCHEMA_VERSION,
        "name": revision["name"],
        "revision": revision["revision"],
        "source": {
            "repo": revision["repo"],
            "commit": revision["commit"],
            "path": revision["path"],
            "license": revision["license"],
            "kicad_origin": [_r(ox), _r(oy)],
        },
        "units": "mm",
        "pcb": {
            "thickness": thickness,
            "outline": [list(p) for p in loop],
            **({"material": revision["pcb_material"]} if "pcb_material" in revision else {}),
        },
        "mounting_holes": mounting_holes,
        "components": components,
        "openings": openings,
    }
    Board.model_validate(board)
    return board


def _ref_key(c: dict[str, Any]) -> tuple[str, int]:
    m = re.match(r"([^\d]*)(\d*)", c["ref"])
    assert m is not None
    return m.group(1), int(m.group(2) or 0)


def dump(board: dict[str, Any]) -> str:
    """Stable, diff-friendly JSON: one point or one object per line."""
    text = json.dumps(board, indent=2) + "\n"
    # Collapse [x, y] pairs onto one line.
    return re.sub(r"\[\s+(-?[\d.]+),\s+(-?[\d.]+)\s+\]", r"[\1, \2]", text)


def convert_all(boards_dir: Path, check: bool = False) -> list[Path]:
    """Convert every revision in `source/revisions.json`; return the files that changed."""
    source = boards_dir / "source"
    footprints = json.loads((source / "footprints.json").read_text())["footprints"]
    revisions = json.loads((source / "revisions.json").read_text())["revisions"]
    changed = []
    for rev in revisions:
        out = boards_dir / rev["output"]
        text = dump(convert((source / rev["kicad_file"]).read_text(), rev, footprints))
        if not out.exists() or out.read_text() != text:
            changed.append(out)
            if not check:
                out.write_text(text)
    return changed


def main(argv: list[str] | None = None) -> int:
    from app.boards import BOARDS_DIR

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--boards-dir", type=Path, default=BOARDS_DIR)
    parser.add_argument("--check", action="store_true", help="fail if a board file is out of date")
    args = parser.parse_args(argv)
    changed = convert_all(args.boards_dir, check=args.check)
    for path in changed:
        print(("out of date: " if args.check else "wrote ") + str(path))
    return 1 if args.check and changed else 0


if __name__ == "__main__":
    sys.exit(main())
