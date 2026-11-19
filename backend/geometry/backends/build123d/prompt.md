# Writing enclosure code for the build123d backend

You write Python that uses [build123d](https://build123d.readthedocs.io) (algebra mode) to build the enclosure. The code runs with the parameters as a plain dict; it never reads files or the network.

## Contract

Define `build(params: dict) -> dict` and return:

```python
{
    "parts": {
        "base": base,
        "lid": lid,
        "heat_set_insert_1": insert_1,
    },  # part name -> build123d solid (Part/Solid)
    "features": [  # in the order you applied them
        {"tag": "boss_1", "part": "lid", "tools": [boss_solid]},
        {"tag": "opening_Q4", "part": "lid", "tools": [cutter]},
    ],
    "materials": {
        "base": params["material"],
        "lid": params["material"],
        "heat_set_insert_1": params["boss"]["insert_material"],
    },
    "kinds": {"heat_set_insert_1": "insert"},  # parts not listed are "enclosure"
    "placements": {"heat_set_insert_1": [x, y, z]},  # where the part's origin sits
}
```

- **The parts are an assembly.** Each part has a name, a material and a placement. `base` and `lid` are the molded enclosure (`kind` `enclosure`, the molding standards apply); each heat-set insert is a part of its own (`heat_set_insert_<n>`, kind `insert`, `params["boss"]["insert_material"]`), seated in its pocket from the mouth up `insert_length`, standing off the pocket wall by `insert_gap`. Return every solid in place, in board coordinates. The board is added to the assembly for you as the part `board`; never name a part `board`. Checks run between parts: no two parts may share material (`interference`), the lid must fit the base, each insert must be seated in its boss, and the board must clear the enclosure.

- **Every face must come from a tagged feature.** A feature's `tools` are the solids it added or cut. A face is credited to the last feature whose tool surface it lies on, so register the shell itself (outer solid and cavity cutter) as the part's first feature, tagged with the part name.
- **Tags are stable names from the board data**: `opening_<component ref>` for each opening in `params["openings"]` (use its `tag`), `boss_<n>` and `insert_<n>` from `params["bosses"]`, `screw_<n>` for the screw holes, `opening_pcb` for the slot the board leaves through, and `peg_<hole id>`, `clamp_<n>` and `support_<n>` from `params["retention"]` for the features that hold the board. Never invent names that depend on build order.
- **Units** are millimetres and degrees, in board coordinates (see `backend/app/boards/SCHEMA.md`): the board lies in x-y with its bottom face at z = 0.
- **Take every value from `params`**. Do not write board names, parts or dimensions into the code.

## Rules the checks enforce

The checks come from `standards.yaml` and the board; they measure the shapes you return.

- Draft: every face that is not horizontal is tilted at least the minimum draft from vertical. Use `extrude(face, amount=..., taper=params["draft"])`; a positive taper narrows the prism as it rises, a negative one widens it. Circles become cones, rounded rectangles get conical corners.
- Wall: nothing thinner than the minimum wall, measured square to each face. Keep `params["wall"]` between every cut and the outside.
- Openings: each opening is exactly the required rectangle at the outside face (±fit tolerance) and centred on its component.
- Clearance: stay at least the minimum clearance from every component's body.
- Fasteners: bosses carry pockets for M3 heat-set inserts (`params["boss"]`).
- Retention: the board cannot move. Every edge of the board inside the enclosure must be stopped within the fit tolerance when pushed out, lifted or pressed down (`params["retention"]` places pegs, a clamp and a support that do this).
- Height: stay within `params["z_bottom"]` to `params["z_top"]`.

## Useful build123d calls

```python
from build123d import Circle, Face, Plane, RectangleRounded, Vector, Wire, extrude

face = Face(Wire.make_polygon([Vector(x, y, z0) for x, y in points], close=True))
solid = extrude(face, amount=z1 - z0, taper=draft)  # polygon prism
disc = extrude((Plane.XY.offset(z0) * Circle(r)).translate((x, y, 0)), amount=h, taper=draft)
part = outer - cavity + boss - pocket  # booleans
```

Avoid coplanar faces between a tool and the part (extend cutters 1 mm past the surfaces they pierce) so booleans stay clean and faces keep one owner.

## Examples

- `examples/enclosure.py`: the full enclosure generator (base, lid, bosses, insert pockets, screw holes, openings, board slot, pegs, clamp and support). Start from it.
