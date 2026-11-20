# Geometry

The enclosure generator behind a swappable CAD backend: code and parameters in; OpenCascade shapes, feature tags and STEP out.

| Module | Contents |
|---|---|
| `params.py` | `enclosure_parameters(board, standards)`: every value the generator needs, derived from a board revision, `standards.yaml` and the existing case in `demos/<board>/`. Plain JSON. |
| `interface.py` | `GeometryBackend` (runs generator code), `BuildResult` (parts with their kind, material and placement, features, per-face feature tags, STEP export) and `BuildError` (with the failing line). |
| `assembly.py` | `assembly(result, board)`: the build's parts plus the board as the part `board`, each with a name, kind, material and placement; `neighbours()` lists the pairs of parts that can touch. |
| `sites.py` | Records the statement that created each feature while generator code runs: `Feature.lines`, served as `BuildSource.features` via `BuildResult.feature_sources()`. Sandboxed builds record it too (`run_build(..., feature_sites=True)`), so code Claude wrote lists its features. |
| `backends/<name>/` | One CAD library each: the adapter, `prompt.md` (how Claude writes code for it) and `examples/` (the enclosure generator). |
| `checks.py` | One check per `check:` in `standards.yaml` plus the board's own checks (openings, antenna keep-out, board fit, board retention) and the checks between parts (interference). |
| `report.py` | The check runner: `check_report(board, standards, result, build_id, mesh)` runs every check and returns the check-results report (`contracts/README.md`), each result located by a point and mesh face IDs, with strength, drop and heat listed as untested. |
| `occ.py` | OpenCascade helpers shared by tagging and checks. |
| `tessellate.py` | `tessellate(result, previous)`: the build as a glTF that follows the mesh contract (`contracts/README.md`), with face and edge IDs that survive rebuilds. |
| `conflicts.py` | Conflict detection: `detect_conflicts(board, standards, params, locked, result, mesh)` says which locks, standards and board parts clash, where on the model, by how much, and the best achievable value of each. `conflict_locks()` is the test fixture. |
| `analysis.py` | Analysis maps: per-face wall thickness (ray casting) and draft, thin, thick and low-draft spots flagged against `standards.yaml`, written as analysis-map documents aligned with a contract mesh. |
| `export.py` | `export_build(board, standards, result, code, params, out_dir, report, mesh)`: STEP, STL, 3MF, GLB, OBJ, DXF flat profiles, a PDF production drawing (a sheet per part), the source code and parameters, and the build note (checks passed, failed and not tested, written from the check report). |
| `step_import.py` | STEP import, run as a worker process by `app.imports`: reads the file in the build sandbox (`run_build(inputs=...)`), makes each solid a part named from the file's labels, tags whole cylinders as `hole_<n>`/`boss_<n>` and the walls around inner loops of flat faces as `opening_<n>` (or `boss_<n>` when they stand out of the face), every other face with its part's name, then tessellates, checks and analyzes it like a build. A file this app exported comes back as the build it was, from the design record in it. |
| `step_manifest.py` | The design record a STEP export carries as a comment (parts' exact solids, materials, kinds, placements, features and tools, face tags and IDs, the board and the fitted object), so exporting and importing a design gives back the same build and the same check results. |
| `diff.py` | What changed between two builds: `feature_summary(result)` (each feature's centroid, size, volume and long-axis rotation, plain JSON), `diff_parameters` and `diff_features` (added, removed, moved, rotated or resized, with distances). `app/builds.py` stores every build (code, parameters, check results, files, parent) under `DATA_DIR/builds` and diffs any two. |

## Usage

```python
from app.boards import BOARDS_DIR, load_board
from app.standards import load_standards
from geometry import enclosure_parameters, generate_enclosure
from geometry.checks import run_checks
from geometry.tessellate import tessellate

board = load_board(BOARDS_DIR / "<revision>.json")
standards = load_standards()
result = generate_enclosure(board, standards, backend="cadquery")  # default: build123d
result.face_refs["lid"][0].feature  # the feature that created the lid's first face
result.export_step("enclosure.step")
run_checks(board, standards, result)

mesh = tessellate(result)  # geometry.tessellate
mesh.write("enclosure.gltf")  # optional vertex colors: mesh.write(path, colors)
stored = mesh.signatures()  # plain JSON, kept with the build
rebuilt = tessellate(generate_enclosure(board, standards, changed), stored)  # same IDs

# Analysis maps on that mesh (the analysis-map contract, `contracts/README.md`).
from app.contracts.mesh import validate_mesh
from geometry.analysis import analysis_maps, analyze

maps = analysis_maps(
    analyze(result, standards), standards, validate_mesh(mesh.to_gltf()), "build-1"
)
maps["wall_thickness"]["flagged"]  # thin and thick spots, each on a face

# Another revision's design on this board: what the board change breaks.
old_design = enclosure_parameters(load_board(BOARDS_DIR / "<older>.json"), standards)
run_checks(board, standards, generate_enclosure(board, standards, old_design))

# The check report (the check-results contract), located on the same mesh.
from geometry.report import check_report

report = check_report(board, standards, result, "build-1", mesh)
report["summary"]  # {"pass": ..., "fail": ..., "untested": 3}

# Every export format plus the build note (`geometry.export.FORMATS`), written to a directory.
from geometry import get_backend
from geometry.export import export_build

params = enclosure_parameters(board, standards)  # what made `result`
code = get_backend("cadquery").example("enclosure")
files = export_build(board, standards, result, code, params, "out/", report, mesh)
[(f.format, f.name) for f in files]  # [("step", "enclosure.step"), ...]

# Store builds and diff any two (`app.builds`; kept in DATA_DIR/builds).
from app.builds import default_store

store = default_store()
first = store.save_result(result, code=code, parameters=params, checks=report)
second = store.save_result(rebuilt_result, code=code, parameters=changed, parent=first.id)
store.diff(first.id, second.id)["features"]  # [{"tag", "change": "moved", "distance", ...}]
store.history(second.id)  # the build and its ancestors, newest first
```

The backend is chosen per run (`backend=`) or per project (`GEOMETRY_BACKEND`).

## How faces get their feature

Generator code returns each feature with the solids it added or cut (its tools), in the order applied. A face of the result belongs to the last feature whose tool has a surface through the face's interior point. This uses exact distances on the final B-rep, so it works the same for every backend. Tags are stable names from the board data: `base`, `lid`, `opening_<component ref>`, `opening_pcb` (where the board leaves the case), `boss_<n>`, `insert_<n>`, `screw_<n>`, `peg_<mounting hole id>`, `clamp_<n>` and `support_<n>`.

## The design

Starting from the existing case's extents (height and length unchanged), the generator builds:

- a base below the board and a lid above it, meeting at the board's bottom face;
- four bosses hanging from the lid's ceiling in the corners, each with a pocket for an M3 heat-set insert, and a screw clearance hole in the base under each; the case widens sideways to make room for them;
- a cut through the lid for every sensor window, vent and button on the board, exactly the required size at the outside face;
- a slot in the end wall where the board leaves the case;
- features that hold the board (`params["retention"]`), each standing off it by half the fit tolerance:
  - a peg from the lid into every mounting hole whose top side is clear: a pin in the hole under a wider post that presses on the board's top face. Where a bottom-side part covers the hole, the pin stops the component clearance above the board's bottom face;
  - a clamp from the lid onto the top face and a support from the base under the bottom face, in the half of the board away from the slot, each at the clear spot with the most room (so small moves of nearby parts leave it clear).

The `board_retention` check proves the board cannot move: each edge of the board inside the enclosure is pushed just past the fit tolerance outward (the whole board slides) and up and down (the board tilts about the opposite edge, and only the half nearer the pushed edge counts). An exact solid boolean must find enclosure material in the way, and the result names the features that stop it.

Every side wall is drafted, and walls are 1.25 times the minimum, so the drafted walls stay above it.

## The assembly

A build is an assembly of parts, not one solid. Each part has a name, a kind, a material and a placement (where its own origin sits, in board coordinates; the solids are always returned in place):

| Part | Kind | Material |
|---|---|---|
| `base`, `lid` | `enclosure` | the `material` standard |
| `heat_set_insert_<n>` | `insert` | the `heat_set_inserts` standard's `material` |
| `board` | `board` | the board file's `pcb.material` |

The generator returns the enclosure parts and the inserts (`kinds`, `placements`); `geometry.assembly` adds the board from the board data: the bare board and every component body. The molding checks and maps (material, wall thickness, draft, overall height) look at the `enclosure` parts only. The checks between parts name the two parts each result is between (`CheckResult.parts`, `parts` in the check report):

- **Lid to base**: `fit_tolerance` at the parting plane (outline and interference), between `base` and `lid`.
- **Insert to boss**: `insert_pockets` also checks the insert part in each pocket is seated: no interference with the boss, its sides within the fit tolerance of the pocket wall, and all of it inside the pocket; between `lid` and `heat_set_insert_<n>`.
- **Board to enclosure**: `component_clearance` and `board_fit`, between a part and `board`.
- **Interference**: `interference`, one result per pair of parts whose bounding boxes meet (`interference:<a>--<b>`), by exact solid booleans (the board's bodies one by one). Pairs whose boxes are apart cannot share material.

The 3D canvas gets every built part as its own mesh node, with its kind, material and placement in the node's extras. The board is drawn from the board data rather than the mesh.

## The check report

`check_report()` runs one check per standard (the standard's `check:`; a standard naming a check that doesn't exist raises `KeyError`) and the board checks, and writes each result in the check-results contract:

- `id` is the check name, or `<check>:<subject>` when the check runs more than once (per part, component, opening, boss or board edge).
- Standard results carry the standard's ID, title and limit (`required` repeats its `kind`, `value` and `unit`). Board results have `standard: null` and name the `component` and `opening`. Openings are titled by kind: a button's is "Button reachable".
- The location is the B-rep face nearest the check's point among the faces of the feature it names (else of its part), by exact distance, and the nearest point on that face. A blocked opening is located on the material in the way, so a failure points at what to cut.
- Strength, drop and heat are always listed as `untested`.

## Stable face and edge IDs

`tessellate()` names every face `<part>.<feature>.<n>`. The feature tag comes first, so a face can only inherit an ID from a face of the same feature. Within a feature, each face is matched to the previous build's faces by surface type, outward direction, area and position relative to the feature's tools; because positions are relative, an opening that moves keeps its faces' IDs. A face with no match gets a number its feature has never used, so a retired ID never comes back for a different face. Without a previous build, faces are numbered in a canonical order. Edge IDs are the IDs of the faces they bound, joined by `--`.

## Analysis maps

`analyze()` measures every face on the exact B-rep, at the same sample points the checks use:

- **Wall thickness.** A ray from each sample goes square into the material; its length is the thickness there, exactly as in the `wall_thickness` check. A face is `thin` when its shortest ray is below `min_wall`, so thin spots and the check always agree. A ray that starts on the narrow end of a wall (a rim, the side of a cut-out) runs along the wall and comes back long, so the map also fits a ball at the ray's midpoint and takes `min(ray, max(ball, min_wall))`: the ball can shorten a long ray but never make a sample thin. A face that is not thin shows its thickest sample and is `thick` above `THICK_FACTOR` times `min_wall` (thick sections sink).
- **Draft.** The smallest angle of the face's samples to the Z pull, like the `draft_angle` check; `low_draft` below `min_draft`, and null on faces square to the pull.

`analysis_maps()` writes one analysis-map document per type: every vertex of a mesh face carries that face's value, ready for `COLOR_0`. By default a part's mesh faces are its B-rep faces in `occ.faces()` order, which is the order `tessellate()` writes them in; a mesh that orders them differently passes `brep_faces` (mesh face ID to B-rep face index).

## Exports

`export_build()` writes each format from the build's exact solids or its tessellation (the same triangles the 3D canvas shows), in the board's coordinates and millimetres:

- **STEP**: every part of the assembly, the board included, as a separate exact body labelled with its part name, plus the design record (`step_manifest.py`) as a STEP comment, which other CAD tools skip and the app's import reads back.

`export_build(..., parts=[...])` exports only those parts of the assembly (any of the build's parts and `board`) instead of the whole assembly; the board is only in the STEP.
- **STL, 3MF, OBJ**: the tessellation with the vertices faces share merged, so each part is a closed mesh. 3MF and OBJ keep one named object per part; STL holds all parts.
- **GLB**: the contract mesh (`contracts/README.md`), face and edge IDs included, in one binary file.
- **DXF**: flat profiles. Each part is cut by a horizontal plane half the minimum wall inside its lowest and highest faces (`profile_planes()`), one layer per cut (`<part>_bottom`, `<part>_top`), so the plates show their openings and holes.
- **PDF**: the production drawing, one A4 sheet per part (`write_drawing()`): front, top, right side and isometric views with the overall dimensions; the part's key dimensions (`key_dimensions(result, part)`): overall size, wall (molded parts, `wall_thickness()`), and the centre and size of every opening and boss on it; a title block (part, material, revision, date, units, board, build); and a revision table (`revision_table()`) listing the build history up to this build, lettered A, B, C. Pass the history as `export_build(..., history=[Revision(...)])`: the session API passes the session's builds (`app.exports.session_revisions`), and `app.exports.store_revisions` reads a stored build's ancestors from the build store. Exporting one part (`parts=[name]`) gives that part's drawing alone. Full GD&T is out of scope.
- **Source**: the generator code and `parameters.json`; `build(params)` on them rebuilds the same model.
- **Build note**: Markdown, written from the check report: every result under "Checks passed", "Checks failed" or "Not tested" (strength, drop, heat) with its ID, standard, measured value, limit and summary, then the exported files.

The tests read every file back with a different reader: STEP through OpenCascade, meshes through trimesh, DXF through ezdxf, the PDF through pypdf.

## Conflicts

`detect_conflicts()` writes the vertical stack as linear constraints on the parameters the generator reads: the roof (`ceiling` to `z_top`), the floor (`z_bottom` to `floor_top`) and the side walls (`wall`) at least `min_wall`; the ceiling and the floor `min_component_clearance` from every part inside the footprint; `z_top - z_bottom` at most `max_height`; and each locked parameter (a dict of paths, or the parameter store) at its value. Locks outside the stack are not constrained here.

When the system has no solution (a linear program, `scipy.optimize.linprog`), a deletion filter shrinks it to a minimal clashing set, and each conflict reports:

- `constraints`: the locks (`locked`), standards (`standard`) and board parts (`board`) in the set, each with its value and `locations` (a point, the part, the component, and with a build and its mesh the stable face ID);
- `over_by`: the least the standards must give, in mm, with every lock held;
- `best` on each constraint: its value when only it gives way and every other constraint holds (the nearest value for a lock, the most demanding achievable value for a standard, null for a board part);
- `location`: at the tallest part the clash squeezes, else at the first lock.

`conflict_locks(board, standards, params)` locks `floor_top` half the component clearance (rounded to 0.1 mm) below the lowest floor the stack allows, which clashes with the clearance standard and the height limit.

```python
from geometry.conflicts import conflict_locks, detect_conflicts

conflicts = detect_conflicts(board, standards, params, conflict_locks(board, standards, params))
conflicts[0].to_json()  # {"id": "stackup", "over_by": ..., "constraints": [...], ...}
detect_conflicts(board, standards, params, store)  # store: app.parameters.ParameterStore
```
