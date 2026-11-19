"""Modeled features: edits made by hand with the canvas's modeling tools.

An edit is plain JSON (`params["edits"]`, one entry per edit, found by its `tag`), applied in
order after the generator code and the sketched features (`geometry.sketch`):

    {"tag": "hole_hand_1", "kind": "hole", "part": "lid", "suppressed": false,
     "plane": {"origin": [x, y, z], "normal": [..], "x_dir": [..]}, "x": 0, "y": 0,
     "diameter": 4, "depth": 4, "insert": true}

Kinds and the values each reads (lengths in mm, angles in degrees):

- `move`: `dx`, `dy`, `dz` and `rotation` (about the vertical axis through the part's centre)
  move the whole `part`, with the features already on it.
- `push_pull`: the flat face on `plane` moves `distance` along its normal: out adds material,
  in (negative) removes it.
- `hole`: a `diameter` hole `depth` deep at (`x`, `y`) on the face on `plane`.
- `boss`: a `diameter` post `height` tall at (`x`, `y`) on the face. With `insert`, a pocket
  for the standard heat-set insert (`pocket_diameter`, `pocket_depth`) is cut in its top and
  tagged `insert_<n>` for the boss `boss_<n>`, as the insert check expects.
- `rib`: a `length` by `thickness` wall `height` tall at (`x`, `y`) on the face, turned
  `rotation` from the face's x axis.
- `fillet`, `chamfer`: round or bevel the edge of `part` nearest `point` by `radius` (fillet)
  or `distance` (chamfer).
- `shell`: hollow `part` to walls `thickness` thick, open at the face on `plane` if given.
- `mirror`: a copy of the feature `source` mirrored across the plane through the part's
  centre normal to `axis` (`x`, `y` or `z`).
- `pattern`: `count` copies of the feature `source` in all (the original is the first): along
  `axis` every `spacing` mm (`linear`), or around the vertical axis through the part's centre
  over `angle` degrees (`circular`; 360 spaces them evenly round the circle).

`plane` is a face's plane in board coordinates, as for a sketch (`app.sketches.face_plane`): the
face an edit refers to is found again on the built part by that plane, so an edit keeps working
when the code before it changes, and fails with a clear message when the face is gone.

`suppressed` edits stay in the parameters and are left out of the build; the code shows them as
a comment. `edits_block(code, edits, backend)` puts the block that applies the edits at the end
of the generator code (after any sketched features), replacing the block it had. Every value
comes from `params`.
"""

from __future__ import annotations

from typing import Any

MARKER = "# --- Modeled features"

KINDS = (
    "move",
    "push_pull",
    "hole",
    "boss",
    "rib",
    "fillet",
    "chamfer",
    "shell",
    "mirror",
    "pattern",
)

_HEADER = """\
# --- Modeled features (edited by hand on the canvas; each is locked: keep it as it is) ---
# Their kinds and values are in params["edits"].
_build_without_edits = build


def build(params):
    result = _build_without_edits(params)
"""

_BUILD123D = '''\


def model_feature(result, params, tag):
    import math

    from build123d import (
        Axis,
        Circle,
        GeomType,
        Location,
        Part,
        Plane,
        Pos,
        Rectangle,
        Rot,
        Vector,
        extrude,
        mirror,
        offset,
    )

    e = next(e for e in params["edits"] if e["tag"] == tag)
    kind, name = e["kind"], e["part"]
    if name not in result["parts"]:
        raise ValueError(f"{tag}: the design has no part {name!r}")
    part = result["parts"][name]
    eps = 1e-6

    def plane():
        p = e["plane"]
        return Plane(origin=tuple(p["origin"]), x_dir=tuple(p["x_dir"]), z_dir=tuple(p["normal"]))

    def face():
        p = e["plane"]
        n, o = Vector(*p["normal"]), Vector(*p["origin"])
        found = []
        for f in part.faces():
            if f.geom_type != GeomType.PLANE:
                continue
            c = f.center()
            if f.normal_at(c).dot(n) > 0.999 and abs((c - o).dot(n)) < 1e-3:
                found.append(((c - o).length, f))
        if not found:
            raise ValueError(f"{tag}: the face it was made on is no longer on {name}")
        return min(found, key=lambda t: t[0])[1]

    def edge():
        point = Vector(*e["point"])
        return min(part.edges(), key=lambda d: d.distance_to(point))

    def centre():
        return part.bounding_box().center()

    def volume(shape):
        return shape.volume if shape is not None else 0.0

    def difference(before, after):
        """What an edit added and removed, as tools that tag its faces."""
        tools = []
        for tool in (before - after, after - before):
            if volume(tool) > eps:
                tools.append(tool)
        return tools

    def source_tools():
        found = [f for f in result["features"] if f["tag"] == e["source"]]
        if not found:
            raise ValueError(f"{tag}: the design has no feature {e['source']!r} to copy")
        tools = [t for f in found for t in f["tools"] if volume(t) > eps]
        # A tool the part holds was added; one it doesn't was cut.
        return [(t, volume(part & t) > volume(t) / 2) for t in tools]

    def apply(copies):
        nonlocal part
        tools = []
        for tool, added in copies:
            part = part + tool if added else part - tool
            tools.append(tool)
        return tools

    def axis_vector():
        return {"x": Vector(1, 0, 0), "y": Vector(0, 1, 0), "z": Vector(0, 0, 1)}[e["axis"]]

    tools = []
    extra = []
    if kind == "move":
        c = centre()
        to = Location((e.get("dx", 0), e.get("dy", 0), e.get("dz", 0)))
        turn = Axis((c.X, c.Y, c.Z), (0, 0, 1))

        def place(shape):
            return shape.rotate(turn, e.get("rotation", 0)).moved(to)

        part = place(part)
        for f in result["features"]:
            if f["part"] == name:
                f["tools"] = [place(t) for t in f["tools"]]
    elif kind == "push_pull":
        d = e["distance"]
        tool = extrude(face(), amount=d)
        part = part + tool if d > 0 else part - tool
        tools = [tool]
    elif kind == "hole":
        disc = plane().offset(1) * Pos(e["x"], e["y"]) * Circle(e["diameter"] / 2)
        tool = extrude(disc, amount=-(e["depth"] + 1))
        part = part - tool
        tools = [tool]
    elif kind == "boss":
        at = Pos(e["x"], e["y"])
        post = extrude(plane() * at * Circle(e["diameter"] / 2), amount=e["height"])
        part = part + post
        tools = [post]
        if e.get("insert"):
            top = plane().offset(e["height"] + 1) * at * Circle(e["pocket_diameter"] / 2)
            pocket = extrude(top, amount=-(e["pocket_depth"] + 1))
            part = part - pocket
            extra = [("insert_" + tag.removeprefix("boss_"), [pocket])]
    elif kind == "rib":
        bar = Rot(0, 0, e.get("rotation", 0)) * Rectangle(e["length"], e["thickness"])
        tool = extrude(plane() * Pos(e["x"], e["y"]) * bar, amount=e["height"])
        part = part + tool
        tools = [tool]
    elif kind in ("fillet", "chamfer"):
        before = part
        target = edge()
        solid = next(s for s in part.solids() if any(target.is_same(d) for d in s.edges()))
        if kind == "fillet":
            done = solid.fillet(e["radius"], [target])
        else:
            done = solid.chamfer(e["distance"], None, [target])
        others = [s for s in part.solids() if not s.is_same(solid)]
        part = Part(children=[done, *others]) if others else done
        tools = difference(before, part)
    elif kind == "shell":
        before = part
        openings = face() if e.get("plane") else None
        part = offset(part, amount=-e["thickness"], openings=openings)
        tools = difference(before, part)
    elif kind == "mirror":
        c = centre()
        about = Plane(origin=(c.X, c.Y, c.Z), z_dir=tuple(axis_vector()))
        tools = apply([(mirror(t, about=about), added) for t, added in source_tools()])
    elif kind == "pattern":
        copies = []
        found = source_tools()
        for i in range(1, e["count"]):
            if e["pattern"] == "linear":
                step = axis_vector() * (e["spacing"] * i)
                copies += [(t.moved(Location(step)), added) for t, added in found]
            else:
                c = centre()
                full = abs(abs(e["angle"]) - 360) < eps
                angle = e["angle"] * i / (e["count"] if full else e["count"] - 1)
                turn = Axis((c.X, c.Y, c.Z), (0, 0, 1))
                copies += [(t.rotate(turn, angle), added) for t, added in found]
        tools = apply(copies)
    else:
        raise ValueError(f"{tag}: unknown kind of edit {kind!r}")
    result["parts"][name] = part
    result["features"].append({"tag": tag, "part": name, "tools": tools})
    for extra_tag, extra_tools in extra:
        result["features"].append({"tag": extra_tag, "part": name, "tools": extra_tools})
    return result
'''

HELPERS = {"build123d": _BUILD123D}


def strip_block(code: str) -> str:
    """The code without its modeled features block."""
    at = code.find(MARKER)
    return code if at < 0 else code[:at].rstrip("\n") + "\n"


def describe(edit: dict[str, Any]) -> str:
    """A few words on the edit, for the comment on its line."""
    kind = edit["kind"]
    if kind in ("mirror", "pattern"):
        what = f"{edit.get('pattern', '')} pattern" if kind == "pattern" else "mirror"
        return f"{what.strip()} of {edit.get('source')}"
    return f"{kind.replace('_', ' ')} on {edit.get('part')}"


def call_line(edit: dict[str, Any]) -> str:
    if edit.get("suppressed"):
        return f"    # {edit['tag']} is suppressed: {describe(edit)}"
    return f'    result = model_feature(result, params, "{edit["tag"]}")  # {describe(edit)}'


def edits_block(
    code: str, edits: list[dict[str, Any]], backend: str = "build123d"
) -> tuple[str, dict[str, tuple[int, int]]]:
    """The code with the block that applies `edits` at its end (none: no block), and the line
    of each edit's call (or suppressed comment) in it, by tag (1-based)."""
    base = strip_block(code)
    if not edits:
        return base, {}
    if backend not in HELPERS:
        raise ValueError(f"no modeled features for the {backend} backend")
    head = base.rstrip("\n") + "\n\n\n" + _HEADER
    first = head.count("\n") + 1
    calls = [call_line(e) for e in edits]
    lines = {e["tag"]: (first + i, first + i) for i, e in enumerate(edits)}
    out = head + "\n".join(calls) + "\n    return result\n" + HELPERS[backend]
    return out, lines


def dependents(edits: list[dict[str, Any]], tag: str) -> list[str]:
    """The edits that copy the feature `tag` (mirrors and patterns of it), and theirs in turn."""
    out: list[str] = []
    frontier = [tag]
    while frontier:
        current = frontier.pop()
        for e in edits:
            if e.get("source") == current and e["tag"] not in out:
                out.append(e["tag"])
                frontier.append(e["tag"])
    return out
