from build123d import Circle, Plane, Pos, Rectangle, Rot, extrude


def build(params):
    length, width, height = params["length"], params["width"], params["height"]
    t, d, draft = params["thickness"], params["hole_diameter"], params["draft"]
    base = extrude(Pos(length / 2, 0) * Rectangle(length, width), amount=t, taper=draft)
    upright = extrude(Pos(t / 2, 0) * Rectangle(t, width), amount=height, taper=draft)
    # A hole through each leg, 1 mm past both faces it pierces.
    hole_1 = extrude(
        Plane.XY.offset(-1) * (Pos((length + t) / 2, 0) * Circle(d / 2)),
        amount=t + 2,
        taper=-draft,
    )
    hole_2 = Pos(-1, 0, (height + t) / 2) * Rot(0, 90, 0) * extrude(Circle(d / 2), amount=t + 2)
    return {
        "parts": {"bracket": base + upright - hole_1 - hole_2},
        "features": [
            {"tag": "bracket", "part": "bracket", "tools": [base, upright]},
            {"tag": "hole_1", "part": "bracket", "tools": [hole_1]},
            {"tag": "hole_2", "part": "bracket", "tools": [hole_2]},
        ],
        "materials": {"bracket": params["material"]},
    }
