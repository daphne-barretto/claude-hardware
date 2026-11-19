from build123d import Plane, RectangleRounded, extrude


def build(params):
    length, width, height = params["length"], params["width"], params["height"]
    wall, radius, draft = params["wall"], params["corner_radius"], params["draft"]
    # Outer shell and cavity narrow at the same draft, so the wall keeps its thickness.
    outer = extrude(RectangleRounded(length, width, radius), amount=height, taper=draft)
    inner = RectangleRounded(length - 2 * wall, width - 2 * wall, max(radius - wall, 0.5))
    cavity = extrude(Plane.XY.offset(wall) * inner, amount=height, taper=draft)
    return {
        "parts": {"box": outer - cavity},
        "features": [{"tag": "box", "part": "box", "tools": [outer, cavity]}],
        "materials": {"box": params["material"]},
    }
