# Enclosure generator for build123d: base, lid, corner bosses with heat-set insert pockets and
# the inserts in them (separate parts),
# screw holes, the openings the board needs, the slot where the board leaves the case and
# the pegs, clamp and support that hold the board.
#
# Every value comes from `params` (see geometry/params.py); nothing about a particular board is
# written here. All side walls are drafted by params["draft"] degrees: positive taper narrows
# a prism as it rises, negative taper widens it.
import math

from build123d import Circle, Face, Plane, RectangleRounded, Vector, Wire, extrude


def prism(points, z0, z1, taper=0.0):
    """Extrude a polygon at z0 up to z1, narrowing by `taper` degrees as it rises."""
    face = Face(Wire.make_polygon([Vector(x, y, z0) for x, y in points], close=True))
    return extrude(face, amount=z1 - z0, taper=taper)


def rounded(x0, x1, y0, y1, r, z0, z1, taper=0.0):
    """Rounded rectangle from z0 up to z1."""
    sketch = Plane.XY.offset(z0) * RectangleRounded(x1 - x0, y1 - y0, r)
    return extrude(sketch.translate(((x0 + x1) / 2, (y0 + y1) / 2, 0)), amount=z1 - z0, taper=taper)


def disc(x, y, radius, z0, z1, taper=0.0):
    """Circle of `radius` at z0 up to z1 (a cone when tapered)."""
    face = (Plane.XY.offset(z0) * Circle(radius)).translate((x, y, 0))
    return extrude(face, amount=z1 - z0, taper=taper)


def rect(o, grow_by):
    w, length = o["size"][0] + 2 * grow_by, o["size"][1] + 2 * grow_by
    r = math.radians(o["rotation"])
    c, s = math.cos(r), math.sin(r)
    corners = [
        (-w / 2, -length / 2),
        (w / 2, -length / 2),
        (w / 2, length / 2),
        (-w / 2, length / 2),
    ]
    return [(o["x"] + x * c - y * s, o["y"] + x * s + y * c) for x, y in corners]


def build(params):
    p = params
    t = math.tan(math.radians(p["draft"]))
    wall, draft = p["wall"], p["draft"]
    fx, fy, r = p["footprint"]["x"], p["footprint"]["y"], p["footprint"]["corner_radius"]
    zb, zt, zp = p["z_bottom"], p["z_top"], p["parting_z"]
    floor_top, ceiling = p["floor_top"], p["ceiling"]
    features = []

    def feature(tag, part, *tools):
        features.append({"tag": tag, "part": part, "tools": list(tools)})

    def footprint(inset, z_ref, z0, z1, taper):
        """The footprint offset inward by `inset`, sized exactly at z_ref, from z0 to z1."""
        d = inset - (z_ref - z0) * math.tan(math.radians(taper))
        return rounded(fx[0] + d, fx[1] - d, fy[0] + d, fy[1] - d, r - d, z0, z1, taper)

    # Base: below the parting plane, walls widening towards the parting plane.
    base_outer = footprint(0, zp, zb, zp, -draft)
    base_cavity = footprint(wall, zp, floor_top, zp + 1, -draft)
    base = base_outer - base_cavity
    feature("base", "base", base_outer, base_cavity)

    # Lid: above the parting plane, walls narrowing towards the top.
    lid_outer = footprint(0, zp, zp, zt, draft)
    lid_cavity = footprint(wall, zp, zp - 1, ceiling, draft)
    lid = lid_outer - lid_cavity
    feature("lid", "lid", lid_outer, lid_cavity)

    # Bosses hang from the lid's ceiling, narrowing towards their free end, each with a pocket
    # for a heat-set insert. The base gets a clearance hole under each one for the screw.
    b = p["boss"]
    inserts, placements = {}, {}
    for boss in p["bosses"]:
        x, y = boss["x"], boss["y"]
        top = ceiling + wall / 2  # overlap the lid's top so the boss fuses into it
        bottom_r = b["radius"] - (ceiling - b["bottom"]) * t
        tool = disc(x, y, bottom_r, b["bottom"], top, -draft)
        lid = lid + tool
        feature(boss["tag"], "lid", tool)
    for boss in p["bosses"]:
        x, y, n = boss["x"], boss["y"], boss["id"]
        z0 = b["bottom"] - 1
        pocket = disc(x, y, b["insert_diameter"] / 2 + t, z0, b["insert_top"], draft)
        lid = lid - pocket
        feature(f"insert_{n}", "lid", pocket)
        # The heat-set insert itself, a separate part: from the pocket's mouth up its length,
        # with the pocket's draft, standing off the pocket wall by the insert gap.
        insert = disc(
            x,
            y,
            b["insert_diameter"] / 2 - b["insert_gap"],
            b["bottom"],
            b["bottom"] + b["insert_length"],
            draft,
        )
        inserts[f"heat_set_insert_{n}"] = insert
        placements[f"heat_set_insert_{n}"] = [x, y, b["bottom"]]
        feature(f"heat_set_insert_{n}", f"heat_set_insert_{n}", insert)
        z0 = zb - 1
        hole = disc(x, y, b["screw_clearance"] / 2 + t, z0, floor_top + 1, draft)
        base = base - hole
        feature(f"screw_{n}", "base", hole)

    # Openings the board needs, cut through the lid's top (or the base's floor), exactly the
    # required size at the outside face and widening inwards with the draft.
    for o in p["openings"]:
        if not o["cut"]:
            continue
        if o["side"] == "top":
            z0, z1 = ceiling - 1, zt + 1
            cutter = prism(rect(o, (zt - z0) * t), z0, z1, draft)
            lid = lid - cutter
            feature(o["tag"], "lid", cutter)
        else:
            z0, z1 = zb - 1, floor_top + 1
            cutter = prism(rect(o, -(zb - z0) * t), z0, z1, -draft)
            base = base - cutter
            feature(o["tag"], "base", cutter)

    # Where the board leaves the enclosure: a slot through the lid's wall just above the
    # parting plane, as thick as the board plus the fit tolerance.
    for s in p["slots"]:
        z0, z1 = zp - 1, zp + p["pcb_thickness"] + p["fit"]
        (x0, x1), (y0, y1) = s["x"], s["y"]
        d = (z1 - z0) * t
        cutter = prism([(x0 - d, y0), (x1 + d, y0), (x1 + d, y1), (x0 - d, y1)], z0, z1, draft)
        lid = lid - cutter
        feature(s["tag"], "lid", cutter)

    # Board retention: pegs from the lid through the board's mounting holes (a pin in the hole
    # under a post that presses on the board's top face), a clamp from the lid onto the top face
    # and a support from the base under the bottom face. Each stands off the board by the gap.
    keep = p["retention"]
    gap, pcb = keep["gap"], p["pcb_thickness"]
    lid_top = ceiling + wall / 2  # overlap the lid's top so the feature fuses into it
    for peg in keep["pegs"]:
        x, y = peg["x"], peg["y"]
        post_bottom = pcb + gap if peg["post_radius"] else lid_top
        pin_top = post_bottom + 0.5 if peg["post_radius"] else lid_top
        pin_r = peg["pin_radius"] - (pcb - peg["tip"]) * t  # pin_radius at the board's top face
        tools = [disc(x, y, pin_r, peg["tip"], pin_top, -draft)]
        if peg["post_radius"]:
            tools.append(disc(x, y, peg["post_radius"], post_bottom, lid_top, -draft))
        for tool in tools:
            lid = lid + tool
        feature(peg["tag"], "lid", *tools)
    for pad in keep["clamps"]:
        tool = disc(pad["x"], pad["y"], pad["radius"], pcb + gap, lid_top, -draft)
        lid = lid + tool
        feature(pad["tag"], "lid", tool)
    for pad in keep["supports"]:
        z0, z1 = floor_top - wall / 2, -gap
        tool = disc(pad["x"], pad["y"], pad["radius"] + (z1 - z0) * t, z0, z1, draft)
        base = base + tool
        feature(pad["tag"], "base", tool)

    return {
        "parts": {"base": base, "lid": lid, **inserts},
        "features": features,
        "materials": {
            "base": p["material"],
            "lid": p["material"],
            **{name: b["insert_material"] for name in inserts},
        },
        "kinds": {name: "insert" for name in inserts},
        "placements": placements,
    }
