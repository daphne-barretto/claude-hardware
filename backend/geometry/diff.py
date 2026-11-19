"""What changed between two builds: parameters changed, features added, removed or moved.

Features are compared by tag (stable names from the board data, see `geometry.params`). A
feature's place is the geometry it added or cut (its tools): the centroid of their volume, their
bounding-box size, their volume and the direction of their long axis in the board plane. The
summary is plain JSON (`feature_summary`), so a stored build can be compared without rebuilding.

A feature counts as moved when its centroid moves more than `POSITION_TOLERANCE`, rotated when
its long axis turns more than `ROTATION_TOLERANCE` (modulo 180 degrees: the tools are
rectangles and round holes), and resized when its size or volume changes. These are the
tolerances `demos/<board>/CHANGES.md` is computed with (`app.boards.changes`), so a board
difference and the enclosure change it causes are judged alike.
"""

from __future__ import annotations

import math
from typing import Any

from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps

from app.boards.changes import POSITION_TOLERANCE, ROTATION_TOLERANCE
from geometry import occ
from geometry.interface import BuildResult

# Below this relative difference between the principal second moments in the board plane, a
# shape has no long axis (a round hole, a square button) and its rotation is not defined.
ISOTROPY = 0.05
# Relative volume change that counts as resized.
VOLUME_TOLERANCE = 1e-3
# Parameters closer than this are equal (floating-point noise, not a design change).
VALUE_TOLERANCE = 1e-9


def _round(v: float, digits: int = 4) -> float:
    return round(v, digits) + 0.0  # no -0.0


def feature_summary(result: BuildResult) -> list[dict[str, Any]]:
    """Where each feature is, in the order applied: `{tag, part, centroid, size, volume,
    rotation}`. `rotation` is the long axis in the board plane, degrees in [0, 180), or None
    for a shape without one. A tag used by several features is summarised over all of them."""
    order: list[str] = []
    grouped: dict[str, list] = {}
    for f in result.features:
        if f.tag not in grouped:
            order.append(f.tag)
            grouped[f.tag] = []
        grouped[f.tag].append(f)
    return [_summarise(tag, grouped[tag]) for tag in order]


def _summarise(tag: str, features: list) -> dict[str, Any]:
    props = GProp_GProps()
    lo = [math.inf] * 3
    hi = [-math.inf] * 3
    for f in features:
        for tool in f.tools:
            one = GProp_GProps()
            BRepGProp.VolumeProperties_s(tool, one)
            props.Add(one)
            a, b = occ.bbox(tool)
            lo = [min(x, y) for x, y in zip(lo, a, strict=True)]
            hi = [max(x, y) for x, y in zip(hi, b, strict=True)]
    volume = props.Mass()
    if volume <= 0:
        return {
            "tag": tag,
            "part": features[0].part,
            "centroid": None,
            "size": None,
            "volume": 0.0,
            "rotation": None,
        }
    c = props.CentreOfMass()
    m = props.MatrixOfInertia()  # about the centroid; off-diagonal terms are -∫xy
    sxx_minus_syy = m.Value(2, 2) - m.Value(1, 1)
    sxy = -m.Value(1, 2)
    # The in-plane second moments' eigenvalues differ by `spread` and sum to Izz.
    spread = math.hypot(sxx_minus_syy, 2 * sxy)
    total = m.Value(3, 3)
    rotation = None
    if total > 0 and spread / total > ISOTROPY:
        rotation = _round(math.degrees(0.5 * math.atan2(2 * sxy, sxx_minus_syy)) % 180.0, 2)
        if rotation >= 180.0:
            rotation = 0.0
    return {
        "tag": tag,
        "part": features[0].part,
        "centroid": [_round(c.X()), _round(c.Y()), _round(c.Z())],
        "size": [_round(h - low) for low, h in zip(lo, hi, strict=True)],
        "volume": _round(volume),
        "rotation": rotation,
    }


# --- Parameters --------------------------------------------------------------------------------


def flatten_parameters(params: Any, prefix: str = "") -> dict[str, Any]:
    """Every leaf value by path, e.g. `footprint.x[0]` or `openings[Q4_window].x`.

    Items of a list of objects are named by their `id` (else `tag`) so that adding an item
    doesn't make every later item look changed.
    """
    out: dict[str, Any] = {}
    if isinstance(params, dict) and params:
        for key, value in params.items():
            out.update(flatten_parameters(value, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(params, list) and params and all(isinstance(v, dict) for v in params):
        for n, item in enumerate(params):
            name = item.get("id", item.get("tag", n))
            if name is None:
                name = n
            out.update(flatten_parameters(item, f"{prefix}[{name}]"))
    elif isinstance(params, list) and params:
        for n, item in enumerate(params):
            out.update(flatten_parameters(item, f"{prefix}[{n}]"))
    else:
        out[prefix] = params
    return out


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, int | float) and isinstance(b, int | float):
        return abs(a - b) <= VALUE_TOLERANCE
    return a == b


def diff_parameters(old: dict, new: dict) -> list[dict[str, Any]]:
    """`{name, change, old, new}` for every parameter added, removed or changed, by name."""
    a, b = flatten_parameters(old), flatten_parameters(new)
    changes = []
    for name in sorted(a.keys() | b.keys()):
        if name not in b:
            changes.append({"name": name, "change": "removed", "old": a[name], "new": None})
        elif name not in a:
            changes.append({"name": name, "change": "added", "old": None, "new": b[name]})
        elif not _same(a[name], b[name]):
            changes.append({"name": name, "change": "changed", "old": a[name], "new": b[name]})
    return changes


# --- Features ----------------------------------------------------------------------------------


def _angle_delta(a: float, b: float) -> float:
    d = (b - a) % 180.0
    return min(d, 180.0 - d)


def _feature_change(a: dict, b: dict) -> dict[str, Any] | None:
    kinds = []
    distance = offset = None
    if a["centroid"] is not None and b["centroid"] is not None:
        distance = math.dist(a["centroid"], b["centroid"])
        offset = [_round(n - o) for o, n in zip(a["centroid"], b["centroid"], strict=True)]
        if distance > POSITION_TOLERANCE:
            kinds.append("moved")
    turned = None
    if a["rotation"] is not None and b["rotation"] is not None:
        turned = _angle_delta(a["rotation"], b["rotation"])
        if turned > ROTATION_TOLERANCE:
            kinds.append("rotated")
    elif (a["rotation"] is None) != (b["rotation"] is None):
        kinds.append("resized")  # gained or lost a long axis: its shape changed
    if "resized" not in kinds and _resized(a, b):
        kinds.append("resized")
    if a["part"] != b["part"]:
        kinds.append("changed part")
    if not kinds:
        return None
    return {
        "tag": b["tag"],
        "part": b["part"],
        "change": " and ".join(kinds),
        "distance": None if distance is None else _round(distance),
        "offset": offset,
        "rotation": None if turned is None else _round(turned, 2),
        "old": a,
        "new": b,
    }


def _resized(a: dict, b: dict) -> bool:
    if (a["size"] is None) != (b["size"] is None):
        return True
    if a["size"] is not None:
        # Sizes are axis-aligned, so compare them square to the long axis when it turned.
        if (
            any(
                abs(x - y) > POSITION_TOLERANCE
                for x, y in zip(sorted(a["size"][:2]), sorted(b["size"][:2]), strict=True)
            )
            or abs(a["size"][2] - b["size"][2]) > POSITION_TOLERANCE
        ):
            return True
    scale = max(abs(a["volume"]), abs(b["volume"]), 1e-9)
    return abs(a["volume"] - b["volume"]) / scale > VOLUME_TOLERANCE


def diff_features(old: list[dict], new: list[dict]) -> list[dict[str, Any]]:
    """One change per feature added, removed, moved, rotated or resized, by tag.

    Each change: `{tag, part, change, distance, offset, rotation, old, new}`, where `change` is
    `added`, `removed`, or a combination such as `moved and rotated`; `distance` is how far the
    centroid moved (mm), `offset` that move as `[dx, dy, dz]`, and `rotation` how far the long
    axis turned (degrees), when both builds have the feature; `old` and `new` are the feature
    summaries.
    """
    a = {f["tag"]: f for f in old}
    b = {f["tag"]: f for f in new}
    changes = []
    for f in old:
        if f["tag"] not in b:
            changes.append(_endpoint(f, "removed"))
    for f in new:
        if f["tag"] not in a:
            changes.append(_endpoint(f, "added"))
        else:
            change = _feature_change(a[f["tag"]], f)
            if change is not None:
                changes.append(change)
    return changes


def _endpoint(f: dict, change: str) -> dict[str, Any]:
    return {
        "tag": f["tag"],
        "part": f["part"],
        "change": change,
        "distance": None,
        "offset": None,
        "rotation": None,
        "old": f if change == "removed" else None,
        "new": f if change == "added" else None,
    }
