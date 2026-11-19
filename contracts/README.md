# Contracts

The data formats between the backend and the frontend. Frontend work builds against the fixtures in `fixtures/` and the mock API (`docs/architecture.md`); backend work produces the same formats and proves it with the contract tests (`docs/architecture.md`).

The source of truth is the pydantic models and validators in `backend/app/contracts/`. The files here are generated from them. Do not edit them by hand. Regenerate them with:

```sh
cd backend && .venv/bin/python -m app.contracts
```

`make test` fails if they are out of date.

| File | What |
|---|---|
| `mesh-extras.schema.json` | JSON Schema of the glTF `asset.extras` and part `node.extras` |
| `check-results.schema.json` | JSON Schema of a check report |
| `analysis-map.schema.json` | JSON Schema of an analysis map |
| `session-events.schema.json` | JSON Schema of one server-sent event's `data` |
| `change-set.schema.json` | JSON Schema of the proposed changes of an agent run |
| `risk-flags.schema.json` | JSON Schema of the risks an agent run flags, each with its fix |
| `telemetry-event.schema.json` | JSON Schema of one line of a session's telemetry log (`docs/architecture.md`) |
| `session-api.openapi.json` | OpenAPI 3.1 document of the session API, including the event schemas |
| `models.json` | The model catalogue: the picker's models and the IDs `MessageCreate.model` accepts |

Shared conventions:

- Lengths are millimetres and angles are degrees, in board coordinates (`backend/app/boards/SCHEMA.md`): x and y in the board plane, Z up, and the board's bottom face at z = 0.
- IDs (faces, edges, features, checks, builds) are strings matching `^[A-Za-z0-9_][A-Za-z0-9_.:\-]*$`. Treat them as opaque. Features named after board data keep the board's case, as in `opening_Q4`.
- A **location** is `{point: [x, y, z], part, faces: [face IDs], edges: [edge IDs]}`. The point is on the model's surface; `part` is null when the location spans parts.
- Every document carries a `contract` field (`check-results/1`, `analysis-map/1`; the mesh carries it in `asset.extras`). A breaking change bumps the number.

## Mesh

What the tessellator writes and the 3D canvas loads: **glTF 2.0** (`.gltf` with embedded or external buffers, or `.glb`).

- `asset.extras` = `{contract: "mesh/1", units: "mm", up_axis: "Z", source: {...}}`. `source` is free-form provenance (board, build ID, generator).
- **Units and axes.** Part geometry is in millimetres in board coordinates, with Z up. glTF viewers expect Y up, so the scene's root node (`enclosure`) carries a rotation of −90° about X: `[-0.7071068, 0, 0, 0.7071068]`. There is no scale: one unit is 1 mm. Part nodes have no transform of their own, so their vertex positions are board coordinates as they are.
- **One node per part.** Each child of the root with a mesh is a part (`base`, `lid`, ...). `node.name` equals `node.extras.part`.
- **One triangle primitive per part** (mode 4), with these attributes:
  - `POSITION` and `NORMAL` (float VEC3);
  - `_FACE_ID` (unsigned byte or short SCALAR): the index into `node.extras.faces` of the face the vertex belongs to. All three vertices of a triangle have the same value.
  - `COLOR_0` (optional, VEC3 or VEC4): vertex colors, for example an analysis map baked in.

  Indices are optional. Vertices are not shared between faces, so a face's ID never bleeds into its neighbour.
- **Edges, optionally:** one line primitive (mode 1) with `POSITION` and `_EDGE_ID` (unsigned byte or short), indexing `node.extras.edges`.
- **`node.extras`** = `{part, kind, material, placement, faces: [{id, feature}], edges: [{id, feature, faces: [face IDs]}]}`.
  - `kind`: `enclosure` for a molded part (`base`, `lid`), `insert` for bought-in hardware pressed into it (`heat_set_insert_<n>`). Defaults to `enclosure`. `material` (optional) is the part's material; `placement` is where the part's own origin sits in board coordinates (informational: the vertices are already in board coordinates). The board is not a mesh part: the canvas draws it from the board data.
  - `id`: a stable face or edge ID, unique in the whole mesh. A rebuild keeps a face's ID, so comments and selections survive rebuilds.
  - `feature`: the feature that created the face or edge, for example `base`, `lid`, `opening_<component ref>` or `boss_<n>` (the names come from the board data).
  - An edge lists the one or two faces it bounds.
  - Every listed face has triangles, and every listed edge has segments.

three.js `GLTFLoader` exposes the custom attributes as `geometry.attributes._face_id` and `_edge_id` (lower case), and the extras as `object.userData`.

## Check results

What the check runner produces and the checks UI shows: one JSON report per build.

```json
{
  "contract": "check-results/1",
  "build_id": "build-1",
  "board": {"name": "...", "revision": "..."},
  "summary": {"pass": 6, "fail": 6, "untested": 3, "not_applicable": 0},
  "results": [
    {
      "id": "wall_thickness",
      "check": "wall_thickness",
      "title": "Minimum wall thickness",
      "standard": "min_wall",
      "component": null,
      "opening": null,
      "status": "pass",
      "measured": {"value": 2.5, "unit": "mm"},
      "required": {"kind": "min", "value": 2.0, "unit": "mm"},
      "summary": "The thinnest wall is 2.5 mm on base.",
      "covers": "Every face of every part, from the wall_thickness analysis map.",
      "location": {"point": [0, 0, 0], "part": "base", "faces": ["base.outer_wall_1"], "edges": []}
    }
  ]
}
```

- `board` is the board revision the design was checked against, or null for a design without a board (a part designed from scratch).
- `status` is `pass`, `fail`, `untested` or `not_applicable`. A `pass` or `fail` result needs `measured`, `required` and a `location`. Untested items (strength, drop and heat today) have none of them and are listed separately in the UI.
- `not_applicable` is a standard that doesn't apply to this design (`applies_to` in `standards.yaml`: a standard for designs built around a board, on a design without one, or `object_fit`, for designs made to fit an object, on a design with no `object` parameters). It has a `reason`, set exactly for this status, and no measurement or location; it is not a failure. A design without a board gets no board-derived results.
- `standard` is the ID of the standard in `standards.yaml` the result enforces. Every standard has a result. For such a result, `check` is the standard's `check:` and `required` repeats the standard's `kind`, `value` and `unit`.
- Board-derived checks (opening alignment, keep-outs and so on) have `standard: null` and name the `component` (reference designator) and `opening` (board opening ID) they concern.
- `id` is the check name, or `<check>:<subject>` when a check runs once per subject (`opening_alignment:<opening id>`). IDs are unique in a report.
- `parts` names the two assembly parts a result is between, for example `["base", "lid"]` (lid-to-base fit and interference), `["lid", "heat_set_insert_1"]` (insert-to-boss fit) or `["lid", "board"]` (board-to-enclosure clearance), and is null otherwise. `board` is the board, drawn from the board data rather than the mesh. `interference` has one result per pair of parts that can touch: `interference:<a>--<b>`.
- `measured.value` is text for spec checks or when nothing could be measured (for example `"no opening"`). Units are `mm`, `deg`, `mm2` or `mm3`.
- `summary` counts the results by status (`not_applicable` defaults to 0).

## Analysis maps

What the analysis produces and the color maps show: one JSON document per map type per build.

```json
{
  "contract": "analysis-map/1",
  "build_id": "build-1",
  "type": "wall_thickness",
  "standard": "min_wall",
  "unit": "mm",
  "legend": {"min": 0.0, "max": 6.0, "limit": 2.0, "unit": "mm"},
  "parts": [{"part": "base", "values": [2.5, 2.5, null]}],
  "flagged": [
    {"id": "thick:boss_1.side", "kind": "thick", "value": 7.8, "message": "...",
     "location": {"point": [0, 0, 0], "part": "lid", "faces": ["boss_1.side"], "edges": []}}
  ]
}
```

- `type` is `wall_thickness` (judged against `min_wall`, in mm) or `draft` (against `min_draft`, in degrees, for the mold pull along Z).
- `parts[].values` holds **one value per vertex of that part's triangle primitive**, in vertex order, so it lines up with the mesh and can become `COLOR_0` directly. `null` means not applicable, for example draft on faces square to the pull.
- `legend` is the color scale's range; values outside it clamp to the end colors. `limit` is the standard's value, drawn as a mark on the scale.
- `flagged` lists the spots worth fixing: `thin` (below the minimum), `thick` (thick sections that sink) and `low_draft`. Each has the worst value and a location with at least one face, so it can be sent to Claude as a fix request.

## Change sets

What a finished agent run proposes (`backend/app/agent/proposal.py`) and the tracked-changes overlay shows: one change per thing the run changed in the design.

```json
{
  "contract": "change-set/1",
  "mode": "manual",
  "base_build": "build-3",
  "build": "build-12",
  "checks": {"pass": 60, "fail": 4, "untested": 0},
  "changes": [
    {"id": "openings.Q4-sensor_window", "title": "Move opening_Q4 (...) by (-0.82, -2.97) mm and turn it 90°",
     "reason": "Board change: Q4 moved and rotated. ... Fixes openings:Q4.", "status": "accepted", "rounds": [1],
     "delta": {"kind": "modified", "path": "openings.Q4-sensor_window", "feature": "opening_Q4", "component": "Q4",
               "fields": [{"field": "x", "before": 1.9, "after": 1.08}], "move": [-0.82, -2.97], "rotate": 90.0},
     "checks": [{"id": "openings:Q4", "check": "openings", "standard": null, "effect": "fixes",
                 "before": "fail", "alone": "pass", "after": "pass"}]}
  ]
}
```

- A change's `id` is its parameter path: a list element with an `id` or `tag` (`openings.<id>`), a top-level parameter, or `code` for the generator code. `delta.kind` is `added`, `removed` or `modified`; `move` (mm) and `rotate` (degrees) are set when an element's `x`, `y` or `rotation` changed. `feature` is the element's feature tag on the mesh.
- `checks` are the proof. `before` is the check on the design the run started from (`base_build`), `alone` on that design plus this change only, `after` on `build`, the build of the accepted changes. `effect` is `fixes` (fails before, passes alone), `breaks` (the reverse) or `related` (same component or feature).
- `status` is `proposed`, `accepted` or `rejected`. In `manual` mode the changes start `proposed` and only accepted ones are applied; in `auto` mode they start `accepted`. Accepting or rejecting rebuilds and rechecks with only the accepted changes, and `build` and `checks` follow.
- The session API serves the change set of the session's latest run that changed the design at `GET /api/sessions/{id}/changes`, and reviews it with `POST` (below).

## Risk flags

Risks the checks don't block, which Claude flags after a build (`backend/app/agent/risks.py`), each with a fix that is kept apart from the change set so it can be accepted or ignored on its own.

```json
{
  "contract": "risk-flags/1",
  "build": "build-7",
  "flags": [
    {"id": "thick:boss_1", "kind": "thick", "standard": "min_wall", "value": 7.8, "limit": 2.0, "unit": "mm",
     "message": "Thick section: 7.8 mm, over 2x the 2 mm minimum wall (min_wall), sinks when molded, boss_1 on the lid at (7.9, 85.1, 2.8).",
     "location": {"part": "lid", "feature": "boss_1", "component": null, "faces": ["boss_1.side"], "point": [7.87, 85.08, 2.77]},
     "status": "proposed",
     "fix": {"title": "Core out the boss", "edits": {"boss.radius": 4.0}, "resolves": true,
             "changes": [{"kind": "modified", "path": "boss", "fields": [{"field": "radius", "before": 4.5, "after": 4.0}]}],
             "checks": []}}
  ]
}
```

- `kind` is `thick` (a section that sinks when molded), `thin` or `low_draft` (an analysis spot no failing check covers), or `near_limit` (a passing check of a `min` or `max` standard within 10% of its limit). `id` is `<kind>:<feature>` for analysis spots and `near_limit:<check id>` for checks.
- `message` is the line said in chat: what the risk is and where. `location` has the part, the feature or component, the faces when the analysis names them, and a point when known.
- `fix.edits` are parameter paths (as in `set_parameter`) and `code` for new generator code; `fix.changes` are the same as change-set deltas. The fix was built and checked on its own: it breaks no passing check (`checks` lists the checks it changes or that concern the same feature), and `resolves` says whether the risk is gone with it or only smaller.
- `status` is `proposed` until the person accepts or rejects the fix; accepting lays the edits over the design (and over the run's accepted changes), then rebuilds and rechecks.
- The session API serves the flags of the session's latest run that flagged any at `GET /api/sessions/{id}/risks`, and reviews them with `POST` (below).

## Session API

What the session API serves and the chat calls: `session-api.openapi.json`. Everything is under `/api`:

| Method and path | What |
|---|---|
| `GET /api/examples` | Designs a new session can start from (`demos/b-parasite/examples.yaml`): `{id, title, description}`. The board revision is one of them |
| `GET, POST /api/sessions` | List and create sessions. A new session starts empty: no board, no design and no build until Claude writes the first design. `{"example": id}` (or `board`, an example's new revision) opens an example instead: its existing design is built at once, and its `next_revision` is ready to load. 404 for an unknown example. `{"new_design": true}` marks a new design, where Claude proposes a build plan before its first edit |
| `GET /api/sessions/{id}` | A session: status (`idle`, `running`, `waiting_for_answer`) and latest build |
| `GET, POST /api/sessions/{id}/messages` | Messages. POST sends text with model, effort, Manual/Auto mode, selection and attachments, or an `answer` to a question; it returns 202 and the reply streams on `/events` |
| `GET, POST /api/sessions/{id}/queue` | The message queue (`MessageQueue`: `items`, each a `QueuedMessage` `{id, created_at, message}` with the `MessageCreate` to send, and `paused`). POST a `MessageCreate` while Claude works (201, the `QueuedMessage`): it is sent as a normal message when the run before it ends, one at a time, in order (at once on an idle session). `POST .../messages` still answers 409 while a run is going. Claude's question pauses the queue (`paused`): the next message waits until the question is answered and the run ends; it is never taken as the answer. 409 for an empty message or an `answer`. Saved with the session and scoped by its owner |
| `PATCH, DELETE /api/sessions/{id}/queue/{item}` | Edit a queued message before it is sent (`QueuedMessageUpdate`: `text`, `position` 0 = next), or take it out. 404 once it was sent; 409 for empty text |
| `POST /api/sessions/{id}/queue/{item}/send` | "Send anyway" (202, the stored user `Message`): sends that queued message now as a normal message. While Claude's question is open, the paused run ends with a note that the question was left unanswered; 409 while a run is going that isn't waiting for an answer |
| `POST /api/sessions/{id}/stop` | Stop Claude's run, going, waiting in line or waiting for an answer (202, the `Session`): it ends with an `error` (`reason` `stopped`, `retryable`) and `message_end`; the design and builds are kept and the queue goes on. Nothing to stop: 202 with the session as it is |
| `POST /api/sessions/{id}/retry` | Retry the reply that stopped (`RunRetry`: `model`, `effort`, `mode`; 202, the user `Message` "Retry"): a new run on the same request picks up from the design and builds so far. 409 while a run is going, a question is open, or nothing has run |
| `POST /api/sessions/{id}/attachments`, `GET .../attachments/{attachment}` | Upload a file to send with a message (multipart, field `file`; its filename, a relative path for a folder, names it). Returns an `Attachment` (`name`, `media_type`, `url`) to put in the message's `attachments`; the file is served at `url`. 413 over 50 MB |
| `POST /api/sessions/{id}/imports` | Import a STEP file (multipart, field `file`) as a build: read in the build sandbox, never in the server, with face IDs and feature tags (`hole_<n>`, `boss_<n>`, `opening_<n>` where recognized, else the part's name), checked against the session's board and standards. Returns the `Build` (201); it also arrives as a `build_ready` without a `message_id`, and Claude is told about it with the next message. 413 over 25 MB; 422 for a file that isn't STEP, can't be read or takes too long, with a `detail` that says which; 409 while a run is going |
| `GET /api/sessions/{id}/events` | Server-sent events (below) |
| `GET /api/sessions/{id}/builds[/{build}]` | Builds, oldest first, with the board revision (null for a design without a board), check summary and URLs, and `restored_from` for a restore |
| `GET /api/sessions/{id}/builds/{build}/mesh` | The glTF (mesh contract) |
| `GET /api/sessions/{id}/builds/{build}/checks` | The check report |
| `GET /api/sessions/{id}/builds/{build}/source` | The CAD code that produced the build (`BuildSource`: `backend`, `language`, `filename`, `code`, and `features`: each feature's `tag`, `part` and `lines` `[first, last]`, 1-based and inclusive, of the statement in `code` that created it, in the order applied, and `locked` for a feature sketched by hand), at the build's `source_url` |
| `GET /api/sessions/{id}/builds/{build}/faces/{face}` | A face of the build (`FacePlane`): `planar`, and for a flat face its plane in board coordinates: `origin` (the face's centre), outward unit `normal`, the sketch's x axis `x_dir` (level on a wall, so y = `normal × x_dir` points up; board x on a top or bottom face) and `size` along x and y (mm) |
| `GET /api/sessions/{id}/builds/{build}/board` | The board revision the build was checked against (the build's `board`), in the board JSON format of `backend/app/boards/SCHEMA.md` (`Board`), at the build's `board_url`; 404 when the build has no board |
| `GET /api/sessions/{id}/builds/{build}/maps/{type}` | An analysis map |
| `GET /api/sessions/{id}/builds/{build}/diff?against={other}` | What changed from build `other` (`old`) to `build` (`new`), for any two builds of the session (`BuildDiff`): `parameters`, each `{name, change, old, new}` added, removed or changed by path, and `features`, one per feature tag `{tag, part, change, distance, offset, rotation, old, new}`: `change` is `added`, `removed` or what happened (`moved`, `rotated`, `resized`, joined with ` and `), `distance` and `offset` the centroid's move in mm, `rotation` the long axis's turn in degrees, `old`/`new` where the feature is (`centroid`, `size`, `volume`, `rotation`). The same as the build store's diff (`app.builds`) |
| `POST /api/sessions/{id}/builds/{build}/restore` | Undo in the history: makes the build's design (its parameters and code) the current one again and rebuilds it on the build's revision. Locked parameters stay locked, at the restored values. Returns the new build (201), whose `restored_from` is `build`; it also arrives as a `build_ready` without a `message_id`. 409 while a run is going |
| `POST /api/sessions/{id}/builds/{build}/find` | Find in model: `{query}` in words ("all M3 holes", "faces under 1° of draft") resolved to faces (`FindResult`: `status` `found`, `none` or `unclear`, `understood`, `question` when unclear, and `matches` `{face, part, feature, value, unit}`) |
| `GET /api/sessions/{id}/parameters`, `PATCH .../parameters/{name}` | Design parameters. Setting a value is a hand edit: it locks the parameter and rebuilds, and the new build arrives as a `build_ready` without a `message_id`. Setting `locked: false` hands the parameter back to Claude. A length's `dimension` says where it is measured on the latest build's model (two points, each on a named face, or a radius from a centre to a curved face), inferred from its mesh (`backend/app/dimensions.py`): an overall extent, a feature's radius or a hole's diameter, else null. A design's grouped value that measures there (`boss.radius`) is listed too, and editable by its path; `drag` lists the faces that change it when dragged and the unit axis they move along, where moving them `d` mm changes the value by `d` mm. Faces no parameter names don't drag |
| `GET /api/sessions/{id}/constraints`, `PATCH, DELETE .../constraints/{id}` | The design's spec, as the chips above the chat input show it: one `Constraint` per standard (`id`, `title`, `label`, `kind`, `value`, `unit`, `check`, `standard_value`), with its `source`: `standard` (standards.yaml), `user` (the user's words: `quote`, `message_id`) or `edit`. Empty until a message describes the design. PATCH `{value}` edits a chip; DELETE drops the user's value (409 for a team standard). Either one rebuilds and rechecks on `/events`; both return 409 unless the session is idle |
| `GET, POST /api/sessions/{id}/sketches` | Sketch on a face. POST a `SketchCreate`: `build_id`, `face_id`, `shape` (`circle`, `slot`, `rectangle`, `vent`), `operation` (`cut`, `extrude`), `width` (a circle's diameter, a slot's overall length), `height` (not for a circle), `count` (a vent's slots), `x`, `y` (the centre, mm from the face's centre along its axes) and `depth`, all mm. It returns the `Sketch`: named (`tag`, `sketch_<n>`), `locked`, with its `part` and `plane`. It is a hand edit: the design keeps it as a locked parameter (`sketches.<tag>`) and in the code, the design rebuilds and the new build arrives as a `build_ready` without a `message_id`; that build's source lists the feature, `locked`. 409 when the face isn't flat, the shape runs off the face or its centre isn't on it, or Claude is running |
| `GET, POST /api/sessions/{id}/comments` | Comments pinned to a face ID and point. `GET ?build_id=` locates every pin on that build (the latest by default): `location` is the face found again by its stable ID (or the nearest face of the comment's `feature`) and the point at the same offset from the face's centre (`anchor`) as when pinned; null when the build has neither. A comment carries its `replies`, its `request` once sent to Claude (`{message_id, status}`) and `resolved` |
| `PATCH /api/sessions/{id}/comments/{comment}` | `{"resolved": true}` resolves the comment (its pin is hidden; it stays listed), `false` reopens it |
| `POST /api/sessions/{id}/comments/{comment}/replies` | `{"text"}`: a reply in the thread, as a note (201; it reopens a resolved comment). Send the comment (`MessageCreate.comments`) to have Claude read the thread |
| `PUT /api/sessions/{id}/source` | A hand edit to the code (`SourceUpdate`: `build_id`, `code`). The response is the rebuild, which also arrives as a `build_ready` without a `message_id`, and its source is the edited code. Code that doesn't parse, doesn't define `build(params)` or doesn't build is refused with 400 and a `CodeError` (`detail`, and `line` and `column` when known); nothing changes. Claude gets the edit, as a diff, on its next turn |
| `POST /api/sessions/{id}/exports`, `GET .../exports/{export}/files/{name}` | Export the build in the chosen `formats`, then download the files. Optional `parts` exports only those parts: any of the build's mesh parts and `board` (only the STEP carries the board); default, the whole assembly. 422 for an unknown part, or for the board alone in a format other than STEP. `units` (`mm`, the default, or `in`) is the build note's length unit, the display unit; every other format stays in its native unit, millimetres |
| `GET /api/sessions/{id}/changes` | The change set (`change-set/1`) of the latest run that changed the design; 404 until there is one |
| `POST /api/sessions/{id}/changes` | Review it: body `{"accept": [change ids], "reject": [change ids]}` (`ChangeReview`; changes not named keep their status). Rebuilds and rechecks with only the accepted changes and returns the updated change set. The rebuild streams as `build_ready` with no `message_id`, then the set as `changes_ready`. 404 for an unknown change; 409 for a change both accepted and rejected, while a run is going, or when a lock refuses the review (the `detail` names the locked parameter) |
| `GET /api/sessions/{id}/risks` | The risk flags (`risk-flags/1`) of the latest run that flagged any; 404 until there are some |
| `POST /api/sessions/{id}/risks` | Review the fixes, each on its own: body `{"accept": [flag ids], "reject": [flag ids]}` (`RiskReview`; flags not named keep their status, rejecting an accepted fix takes it back). Calls `Flags.review`: lays the accepted fixes over the design, rebuilds and rechecks, and returns the updated flags. The rebuild streams as `build_ready` with no `message_id`, then the flags as `risks_ready`. 404 for an unknown flag; 409 for a flag both accepted and rejected, while a run is going, or when a lock refuses the fix |
| `POST /api/sessions/{id}/telemetry` | Record a user action only the frontend sees in the session's telemetry log: `{kind: "view", view, value}` (a map, section, compare, history, camera...), `{kind: "choice", setting, value}` (model, effort or mode) or `{kind: "change_decision", change_id, decision}` (accept or reject). 204, no body (`docs/architecture.md`) |

A message's `comments` sends those comments to Claude as requests: one run handles them all and answers each in its own thread (`reply_to_comment`); its `text` may then be empty. A change made for a comment carries the comment's ID (`ProposedChange.comment`).

A message's `selection` is what the user picked on the canvas, all of it sent to Claude: `parts` (whole parts), `components` (board components by reference designator, picked by their openings; the opening faces are not added to `faces`), `faces` and `edges` (IDs from the mesh), `part` (the part they are all on, or null across parts) and `point` (where the last click hit, in mm).

Errors are `{"detail": "..."}` with 400 (refused code, a `CodeError` with its line), 404 (unknown resource), 409 (for example a message while the session is still running), 413 (an attachment too large) or 422 (invalid body).

### Models

`models.json` lists the models a message can ask for. It is generated from `backend/app/contracts/models.py` (`MODEL_CATALOG`), which is the only place to add or remove a model. The generator also writes a copy to `frontend/src/generated/models.json`, which the model picker imports, because the frontend build cannot read files outside `frontend/`.

```json
{"contract": "models/1", "default": "<model id>",
 "models": [{"id": "<model id>", "name": "...", "blurb": "..."}],
 "more_models": [{"id": "<model id>", "name": "...", "blurb": "..."}]}
```

- `models` is the picker's main list and `default` (one of them) is what a new chat starts with.
- `more_models` sits under "More models". When it is empty, the picker hides that item.
- `MessageCreate.model` must be one of these IDs, or null for the server's choice; anything else is a 422.

### Server-sent events

`GET /api/sessions/{id}/events` is a `text/event-stream`. Each event has:

```text
id: 12
event: progress
data: {"type": "progress", "message_id": "msg-2", "text": "... · round 1 of 2", "round": 1, "rounds": 2}
```

`id` increases by one within a session. To resume, reconnect with `Last-Event-ID` (EventSource does this itself) or `?after=<id>`. `?follow=false` returns the events so far and closes the stream. Comment lines (`: keep-alive`) may appear.

| `event` | `data` | Meaning |
|---|---|---|
| `message_start` | `message_id`, `role` | An assistant message begins |
| `text` | `message_id`, `delta` | Text tokens to append |
| `progress` | `message_id`, `text`, `round`, `rounds` | A progress line such as "round 1 of 4" |
| `question` | `message_id`, `question_id`, `text`, `options[] {id, label, description}`, optional `location` and `conflict` | Claude asks; answer by posting a message with `answer: {question_id, option_id}` |
| `build_ready` | `message_id`, `build` | A new build (same shape as `GET .../builds/{id}`). `message_id` is null for a build outside a run: the first build, a rebuild after a hand edit, a restore, a review of the change set or the risk flags |
| `changes_ready` | `message_id`, `change_set` | A run's change set is ready, or a review updated it (same shape as `GET .../changes`). `message_id` is the run's message, or null after a review |
| `constraints` | `message_id` (null outside a message), `constraints[]` | The spec changed: every chip, as `GET .../constraints` returns them |
| `plan` | `message_id`, `steps[]` | Claude's build plan for a new design, before any edit; its questions follow as `question` events |
| `risks_ready` | `message_id`, `risk_flags` | A run's risk flags are ready, or a review updated them (same shape as `GET .../risks`). `message_id` is the run's message, or null after a review |
| `found` | `message_id`, `build_id`, `result` | Claude found parts of the model from chat (`find_in_model`): `result` is a `FindResult` on build `build_id`, as `POST .../builds/{id}/find` returns it. The canvas highlights the matched faces and the chat lists them, ready to send back as the selection; an unclear query shows `understood` and the `question` |
| `message_end` | `message_id` | The message is complete |
| `error` | `message_id`, `message`, `retryable`, `reason` | The run stopped. `message` says plainly what happened, whether anything was lost and what to do (`backend/app/agent/limits.py`); `retryable` offers Retry (`POST .../retry`), which picks up from the design and builds so far; `reason` is a `LimitReason` code (`rate_limited`, `billing`, `not_configured`, `model_unavailable`, `interrupted`, `stopped`, ...) |
| `notice` | `message_id`, `kind`, `text`, `reason`, `retry_in_s`, `attempt`, `attempts`, `place` | A notice while the run goes on: `retrying` a temporary API error in `retry_in_s` seconds (`attempt` of `attempts`), `waiting` behind other runs (`place`, 1: next) or another build, or a `warning` (a build hit a limit, the session couldn't be saved). A later event of the reply replaces a `retrying` or `waiting` notice |
| `queue` | `queue`, `sent`, `message_id` | The message queue changed (queued, edited, moved, removed, sent, or paused by a question): the whole `MessageQueue`. When it sent a message, `sent` is the queued item's ID and `message_id` the stored user message it became; the reply's `message_start` follows |
| `user_message` | `message_id`, `message` | The user sent a message (typed, an answer, a queued message sent, a revision load, a Retry): the stored `Message`, so every tab shows it. The tab that sent it shows it already and takes its ID; a reload has it from `GET .../messages` |
| `comment` | `message_id`, `comment` | A comment was pinned, replied to, sent, answered or resolved: the whole comment, located on the latest build. `message_id` is the run for Claude's replies, else null |
| `notice` | `message_id`, `kind`, `text`, `categories[]`, `regulations[]`, `region` | A notice the chat shows apart from Claude's text. `kind: "refused"`: Claude won't design this (a weapon or harmful part, or a part made to defeat a safety protection or evade a legal requirement); `text` is the reason and nothing was edited. `kind: "safety"`: Claude goes on with a regulated or safety-critical product; `text` says requirements vary by region and none were checked, `categories` what kind of product it is, `regulations` the kinds of regulations that may apply (never a claim the design meets them), `region` the region the user named or null. The build note gets a "Safety notice" section |

A `question` can carry:

- `location`: the spot on the model it is about (a location, above). The canvas pins the question there.
- `conflict`: set when Claude asks because locked values, standards and the board can't all be met (conflict detection): `{id, message, over_by, constraints[], location}`. `over_by` is how far apart they are, in mm. Each constraint is `{kind, id, label, relation, value, unit, best, locations[]}`: `kind` is `locked` (a parameter path), `standard` (a standard ID in `standards.yaml`) or `board` (a component reference); `relation` is how `value` binds (`=` for a lock or a part's height, `>=` for a `min` standard, `<=` for a `max` one); `best` is the best achievable value when only that constraint gives way, or null when no value of it helps (a board part). The options say what gives.
