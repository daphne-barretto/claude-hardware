from build123d import Pos, Rectangle, Rot, extrude


def build(params):
    width, depth, t = params["width"], params["depth"], params["thickness"]
    back_height, lean, draft = params["back_height"], params["lean"], params["draft"]
    base = extrude(Rectangle(depth, width), amount=t, taper=draft)
    # The back rest stands on the rear of the base and leans back by `lean` degrees; its foot
    # sinks into the base so the two are one solid.
    rest = extrude(Rectangle(t * 2, width - 2), amount=back_height, taper=draft)
    back = Pos(-depth / 2 + 2 * t, 0, t / 2) * Rot(0, -lean, 0) * rest
    # The lip in front of the rest stops the phone sliding off.
    lip_x = -depth / 2 + 2 * t + params["lip_offset"]
    lip = Pos(lip_x, 0, t / 2) * extrude(
        Rectangle(t, width - 2), amount=params["lip_height"] + t / 2, taper=draft
    )
    return {
        "parts": {"stand": base + back + lip},
        "features": [
            {"tag": "stand", "part": "stand", "tools": [base]},
            {"tag": "back", "part": "stand", "tools": [back]},
            {"tag": "lip", "part": "stand", "tools": [lip]},
        ],
        "materials": {"stand": params["material"]},
    }
