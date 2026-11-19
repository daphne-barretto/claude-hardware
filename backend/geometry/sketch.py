"""Sketched features: a shape drawn by hand on a flat face, with typed sizes, cut or extruded.

A sketch is plain JSON (`params["sketches"]`, one entry per feature, found by its `tag`):

    {"tag": "sketch_1", "part": "lid", "face_id": "lid-f12", "shape": "circle",
     "operation": "cut", "width": 6, "height": null, "count": null, "x": 0, "y": 0,
     "depth": 2, "plane": {"origin": [x, y, z], "normal": [..], "x_dir": [..]}}

- `plane` is the face's plane in board coordinates: `normal` points out of the part, `x_dir`
  and `normal × x_dir` are the sketch's x and y axes, and `origin` is the face's centre.
- `x`, `y` place the shape's centre on the face, in mm from `origin`.
- `width` is the circle's diameter, the slot's overall length, the rectangle's or the vent's
  width (along x); `height` is the slot's, rectangle's or vent's height (along y).
- A vent is `count` slots of equal width side by side across `width`, with gaps as wide as
  the slots.
- `cut` removes `depth` mm into the part, `extrude` adds `depth` mm out of the face.

`sketch_block(code, sketches, backend)` puts the code that applies them at the end of the
generator code: it wraps `build` so every sketched feature is applied, in order, to what the
generator returns, and registers it as a feature with its tag. The block is replaced, not
added twice, so it can be rewritten after every new sketch. Every value comes from `params`.
"""

from __future__ import annotations

from typing import Any

MARKER = "# --- Sketched features"

_HEADER = """\
# --- Sketched features (drawn by hand; each is locked: keep it as it is) ---
# Their shapes, sizes and planes are in params["sketches"].
_build_without_sketches = build


def build(params):
    result = _build_without_sketches(params)
"""

_BUILD123D = """\


def sketch_feature(result, params, tag):
    from build123d import Circle, Plane, Pos, Rectangle, SlotOverall, extrude

    s = next(s for s in params["sketches"] if s["tag"] == tag)
    p = s["plane"]
    plane = Plane(origin=tuple(p["origin"]), x_dir=tuple(p["x_dir"]), z_dir=tuple(p["normal"]))
    w, h = s["width"], s.get("height") or s["width"]
    if s["shape"] == "circle":
        profiles = [Circle(w / 2)]
    elif s["shape"] == "slot":
        profiles = [SlotOverall(w, h)]
    elif s["shape"] == "rectangle":
        profiles = [Rectangle(w, h)]
    else:  # vent: count slots across the width, with gaps as wide as the slots
        n = s["count"]
        bar = w / (2 * n - 1)
        profiles = [Pos(-w / 2 + bar / 2 + 2 * bar * i, 0) * Rectangle(bar, h) for i in range(n)]
    tools = []
    for profile in profiles:
        profile = Pos(s["x"], s["y"]) * profile
        if s["operation"] == "cut":  # from 1 mm outside the face, depth mm into the part
            tools.append(extrude(plane.offset(1) * profile, amount=-(s["depth"] + 1)))
        else:
            tools.append(extrude(plane * profile, amount=s["depth"]))
    part = result["parts"][s["part"]]
    for tool in tools:
        part = part - tool if s["operation"] == "cut" else part + tool
    result["parts"][s["part"]] = part
    result["features"].append({"tag": tag, "part": s["part"], "tools": tools})
    return result
"""

_CADQUERY = """\


def sketch_feature(result, params, tag):
    import cadquery as cq

    s = next(s for s in params["sketches"] if s["tag"] == tag)
    p = s["plane"]
    plane = cq.Plane(origin=tuple(p["origin"]), xDir=tuple(p["x_dir"]), normal=tuple(p["normal"]))
    w, h = s["width"], s.get("height") or s["width"]
    cut = s["operation"] == "cut"
    wp = cq.Workplane(plane).workplane(offset=1 if cut else 0).center(s["x"], s["y"])
    if s["shape"] == "circle":
        wp = wp.circle(w / 2)
    elif s["shape"] == "slot":
        wp = wp.slot2D(w, h)
    elif s["shape"] == "rectangle":
        wp = wp.rect(w, h)
    else:  # vent: count slots across the width, with gaps as wide as the slots
        n = s["count"]
        bar = w / (2 * n - 1)
        wp = wp.pushPoints([(-w / 2 + bar / 2 + 2 * bar * i, 0) for i in range(n)]).rect(bar, h)
    tool = wp.extrude(-(s["depth"] + 1) if cut else s["depth"]).val()
    part = result["parts"][s["part"]]
    if isinstance(part, cq.Workplane):
        part = part.val()
    result["parts"][s["part"]] = part.cut(tool) if cut else part.fuse(tool)
    result["features"].append({"tag": tag, "part": s["part"], "tools": [tool]})
    return result
"""

HELPERS = {"build123d": _BUILD123D, "cadquery": _CADQUERY}


def strip_block(code: str) -> str:
    """The code without its sketched features block."""
    at = code.find(MARKER)
    return code if at < 0 else code[:at].rstrip("\n") + "\n"


def call_line(sketch: dict[str, Any]) -> str:
    return (
        f'    result = sketch_feature(result, params, "{sketch["tag"]}")'
        f"  # {sketch['shape']}, {sketch['operation']}"
    )


def sketch_block(
    code: str, sketches: list[dict[str, Any]], backend: str = "build123d"
) -> tuple[str, dict[str, tuple[int, int]]]:
    """The code with the block that applies `sketches` at its end (none: no block), and the
    line of each sketch's call in it, by tag (1-based)."""
    base = strip_block(code)
    if not sketches:
        return base, {}
    if backend not in HELPERS:
        raise ValueError(f"no sketched features for the {backend} backend")
    head = base.rstrip("\n") + "\n\n\n" + _HEADER
    first = head.count("\n") + 1
    calls = [call_line(s) for s in sketches]
    lines = {s["tag"]: (first + i, first + i) for i, s in enumerate(sketches)}
    out = head + "\n".join(calls) + "\n    return result\n" + HELPERS[backend]
    return out, lines
