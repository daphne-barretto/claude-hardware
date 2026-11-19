# Board JSON schema

Each board revision is one JSON file in this directory (`<name>-<revision>.json`). It describes everything the enclosure has to fit around: the board outline, its mounting holes, every component's body, and the openings the enclosure must provide.

The schema is enforced by the pydantic models in `backend/app/boards/schema.py` (`load_board()` validates a file). The current version is `schema_version: 1`.

## Coordinates

- Units are millimetres; angles are degrees.
- The board lies in the x-y plane with its bottom face at z = 0 and its top face at z = `pcb.thickness`. "Top" is the KiCad front (F.Cu) side.
- The origin is the minimum x and minimum y of the outline's bounding box, so every outline point has x >= 0 and y >= 0.
- x points right and y points up as the top side is drawn in KiCad, so the edge at the top of the KiCad drawing has the largest y. KiCad's own y points down: `board x = KiCad x - source.kicad_origin[0]` and `board y = source.kicad_origin[1] - KiCad y`.
- Rotations are counter-clockwise when looking at the top side.

## Fields

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | `1` | Schema version. |
| `name`, `revision` | string | Board name and revision, for example `"2.0"`. |
| `source.repo`, `source.commit`, `source.path` | string | Where the data came from: repository, full 40-character commit hash and file path. |
| `source.license` | string | License of the source data. |
| `source.kicad_origin` | [x, y] | KiCad coordinates of this file's origin (see above). |
| `units` | `"mm"` | Always millimetres. |
| `pcb.thickness` | number | Board thickness. |
| `pcb.material` | string, optional | Substrate material (`pcb_material` in `source/revisions.json`), the board part's material in the assembly. |
| `pcb.outline` | [[x, y], ...] | Closed outline polygon, counter-clockwise, starting at the lowest point. Arcs are approximated with 5 degree segments. |
| `mounting_holes[]` | object | Holes through the board: `id` (`H1`, `H2`, ... ordered by position), `position` (centre) and `diameter`. |
| `components[]` | object | One entry per placed part, ordered by reference designator. |
| `components[].ref`, `.value`, `.footprint` | string | Reference designator, value and KiCad footprint (`library:name`). |
| `components[].role` | string or null | What the part is for the enclosure, for example `"light sensor"`. Null for parts the enclosure does not care about individually. |
| `components[].side` | `"top"` or `"bottom"` | Side of the board the part is mounted on. |
| `components[].position` | [x, y] | Centre of the part's body. |
| `components[].rotation` | number in [0, 360) | Orientation of the body. Rotation 0 means the same physical orientation for a given role, even when two revisions use different footprint libraries (see `rotation_offset` below). |
| `components[].size` | [w, l] | Body width and length along the part's own x and y axes, before rotation. This is the footprint's courtyard, or its fab outline if it has none, or its silkscreen outline, or its pads. |
| `components[].height` | number | Height of the part above the board surface on its side. |
| `openings[]` | object | Openings the enclosure must provide. |
| `openings[].id` | string | `<component ref>-<kind>`, stable across revisions as long as the reference designator is. |
| `openings[].kind` | string | `sensor_window` (light must reach the part), `vent` (air must reach the part), `button` (a finger must reach the actuator) or `antenna_keepout` (no conductive or thick material over the antenna). |
| `openings[].component` | string | Reference designator of the part the opening serves. |
| `openings[].side` | `"top"` or `"bottom"` | Side of the enclosure the opening is on. |
| `openings[].position`, `.rotation`, `.size` | as for components | The rectangle the opening must leave clear, at least. The enclosure standards decide the margins. |

## Example

A minimal valid board: a 20 x 30 mm rectangle with one hole and a light sensor that needs a window.

```json
{
  "schema_version": 1,
  "name": "example",
  "revision": "1.0",
  "source": {
    "repo": "https://example.com/example-board",
    "commit": "0123456789abcdef0123456789abcdef01234567",
    "path": "kicad/example.kicad_pcb",
    "license": "CC BY-SA 4.0",
    "kicad_origin": [100.0, 130.0]
  },
  "units": "mm",
  "pcb": {
    "thickness": 1.6,
    "outline": [[0.0, 0.0], [20.0, 0.0], [20.0, 30.0], [0.0, 30.0]]
  },
  "mounting_holes": [
    { "id": "H1", "position": [10.0, 5.0], "diameter": 2.8 }
  ],
  "components": [
    {
      "ref": "Q1",
      "value": "Q_Photo_NPN",
      "footprint": "snapeda:TR8",
      "role": "light sensor",
      "side": "top",
      "position": [10.0, 25.0],
      "rotation": 90.0,
      "size": [2.8, 1.3],
      "height": 0.6
    }
  ],
  "openings": [
    {
      "id": "Q1-sensor_window",
      "kind": "sensor_window",
      "component": "Q1",
      "side": "top",
      "position": [10.0, 25.0],
      "rotation": 90.0,
      "size": [2.8, 1.3]
    }
  ]
}
```

## How the files are made

The board files are generated from KiCad. Do not edit them by hand: change the inputs and regenerate.

| Input | Contents |
|---|---|
| `source/revisions.json` | The revisions to convert: name, revision, source repository, commit, path and license, the local copy of the KiCad file and the output file name. |
| `source/*.kicad_pcb` | Unmodified copies of the KiCad board files at those commits. |
| `source/footprints.json` | What KiCad does not record, per footprint: `height` (with `height_source`), `role`, `openings`, `rotation_offset` and `ignore`. |

The converter (`backend/app/boards/kicad.py`) reads KiCad 5 and KiCad 6+ board files. It takes the outline and mounting holes from the `Edge.Cuts` layer (lines and arcs form the outline; circles are holes), and each footprint's placement, side and body outline. Then it applies `footprints.json`:

- `height`: the part's height above the board. Every footprint on the board must be listed, so a new part cannot slip through without a height.
- `ignore: true`: drop the footprint (logos and other silkscreen art).
- `rotation_offset`: a multiple of 90 degrees added to the KiCad rotation, so that rotation 0 means the same physical orientation in every footprint library used for a part. The body size is swapped to match.
- `openings`: openings to create for every part with this footprint. By default an opening is the body rectangle. `offset` (relative to the body centre, along the body's own axes) and `size` override that.

Regenerate the board files after changing any input:

```sh
cd backend && .venv/bin/python -m app.boards.kicad
```

To add a revision, copy its `.kicad_pcb` into `source/`, add an entry to `source/revisions.json`, add any new footprints to `source/footprints.json`, regenerate, and update `CHANGES.md`. `make test` fails if a board file is out of date, or if `CHANGES.md` does not match the recomputed differences.
