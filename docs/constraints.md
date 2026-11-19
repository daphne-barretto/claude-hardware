# Constraints

Constraints are the rules a design is checked against: walls at least 2.0 mm, no taller than the
existing case, molded ABS. A **constraint set** is a named group of them. Claude designs to the
session's set, the check runner measures every build against it, and the canvas shows each
verdict. Constraints belong to the user: Claude reads them and never changes them, and a design
that can't meet one is raised with the user, never traded away silently.

In the code and the API the older name stays: a constraint is a *standard* and a constraint set a
*standards set* (`standards.yaml`, `backend/app/standards*.py`, `/api/standards`, and
`StandardsEditor.tsx`). This page uses the user-facing terms.

## Every constraint has a check

A constraint without a check is a bug, because the agent satisfies the checks and nothing else.
So constraints can only be made of the kinds the app can enforce: the **rule catalogue**
(`GET /api/standards/kinds`, `backend/app/standards_catalog.py`). Each kind is one check in
`backend/geometry/checks.py` and fixes:

| Field | Meaning |
| --- | --- |
| `check` | The check that enforces the constraint (`wall_thickness`, `draft_angle`, …) |
| `standard_id` | The constraint's ID in every set (`min_wall`, …); the check and the design code read the value by it, so a set has at most one constraint of each kind |
| `kind`, `unit` | How the value is compared (`min`, `max`, `tolerance`, or `spec`) and its unit (`mm`, `deg`, none for a spec) |
| `min`, `max` | The values the check accepts: physical sanity bounds, not team choices |
| `phrase` | The constraint in plain language, `{value}` for the value ("Walls at least {value}") |
| `dimensions`, `material` | Named sizes and a material a spec constraint must give (the heat-set insert's pocket) |
| `suggested` | The built-in set's constraint of the kind, which a new one starts from |

`backend/tests/test_standards_editor.py` keeps the catalogue and the checks in step: one kind per
check, and every built-in constraint is a catalogue kind with its ID and unit.

A new kind of constraint needs code: a check that measures the model, then a catalogue entry. The
editor's "Request a new kind of check" says so and offers nothing it couldn't enforce.

## Constraint sets

- **The built-in set** is `standards.yaml` ("Default constraints"; its schema is in that file and
  `backend/app/standards.py`). It is read-only in the app: `PATCH` and `DELETE` answer 403.
  Change it in the repository.
- **A browser's own sets** belong to the browser that made them (its owner cookie) and are saved
  in the session store (`DATA_DIR/sessions.db`, table `standards_sets`; the mock API keeps them in
  memory). Another browser gets 404 for them, as for sessions.

A set the browser made is used like the built-in one. Constraints it doesn't have keep the
built-in set's value as a fallback for design code that needs a number
(`StandardsFile.defaults`); those are never checked and never shown.

The built-in set holds only rules that apply to any design of their kind. A limit measured from
one example's own design belongs to that example instead: an entry of an `examples.yaml` in
`demos/` may name a constraints file next to it (`standards:`, same format as `standards.yaml`),
whose rules are added to whatever set a session on that example uses, including a set the user
chooses later, and to the demos recorded on it (`Example.with_standards`). The board revision
example's overall height limit, taken from its existing case, is one: a session with any other
board never gets it. A rule the chosen set already has stays as the set has it.

### Routes

| Route | What it does |
| --- | --- |
| `GET /api/standards` | The built-in set, then the browser's sets (`StandardsSet`: `built_in`, `duplicated_from`, each constraint with `kind`, `value`, `unit`, `applies_to`, `enabled`, `aliases`, and its plain-language `label`) |
| `GET /api/standards/kinds` | The rule catalogue (`CheckKindInfo`) |
| `POST /api/standards` | A new set: `{name}` (empty) or `{name, duplicate_of}` (a copy of any set the browser can see) |
| `GET /api/standards/{id}` | One set |
| `PATCH /api/standards/{id}` | Rename (`name`) and/or replace the constraints (`standards`, a list of `StandardInput`) |
| `DELETE /api/standards/{id}` | Delete a set; sessions using it keep their copy |
| `PUT /api/sessions/{id}/standards` | Use a set in a session from now on (see below) |

A session's first message can also choose its set (`MessageCreate.standards`); after that a
message can't change it (409, pointing at `PUT .../standards`).

### Validation

Nothing is saved unless the whole set is valid. A refused set answers 422 with every problem
(`StandardsErrorBody`: `detail` and `problems`, each with the constraint's index, the field, and a
message to show next to it):

- the kind must be in the catalogue (anything else "needs code");
- a number must be a number, in the kind's unit, within its range;
- a spec needs its text, and every size its kind names, each within range; other kinds take no sizes;
- `applies_to` is `any`, `board`, or `object`;
- one constraint of each kind; a word (alias) names one constraint only and is listed once;
- the set needs a name, unique among the browser's sets and the built-in one.

A constraint's ID, comparison, and unit follow from its kind, and a missing title or description
takes the kind's (the description then says the constraint in plain words).

### Turning a constraint off

A constraint turned off (`enabled: false`) stays in the set but is not checked: the check report
lists it as `untested` with the reason ("Turned off in …, so it was not checked"), or
`not_applicable` when it wouldn't apply to the design anyway. Claude's `read_spec` lists only the
constraints that are on, and names the ones turned off; no chip is made for them.

### Where a constraint applies

`applies_to` is `any`, `board`, or `object`. A constraint that doesn't apply to the design (a board
rule on a design with no board) is reported **not applicable** with the reason, never as a
failure.

## Changing a session's set

The chat header shows the set a session is designed and checked against (the built-in set until
one is chosen). Choosing another set, or choosing the same one again after editing it, calls
`PUT /api/sessions/{id}/standards`:

1. The session takes the set (its own copy, saved with the session, so editing or deleting the
   set elsewhere changes nothing until the user chooses it again). Values the user gave in their
   own words (constraint chips) stay for the constraints the new set has on.
2. The current design is rebuilt and rechecked in a geometry worker (the server never loads the
   CAD kernel), and a `build_ready` updates the canvas.
3. A chat notice says what changed: the constraint changes, then the checks now failing, now
   passing, newly checked, and no longer checked, and the totals.
4. Claude gets the new set from the next message: the prompt says the user changed the
   constraints (and how), and `read_spec` lists them.

The editor does step 1 itself when the user saves the set the session uses.

Claude has no tool that changes constraints, and these routes are for the user only.

## The editor

"Manage constraint sets…" at the bottom of the constraint set picker (on New, and in a session's
header) opens the editor: the sets on the left (built-in first, with a lock), the chosen set's
constraints on the right, each as a plain-language row ("Walls at least 2.0 mm"). On the browser's
own set each row has an on/off switch, the value, what designs it applies to, details (title,
description, words that name it, and sizes) and Remove; "Add rule" lists the catalogue kinds the
set lacks. Save sends the whole set; problems show next to their constraint. "Use in this chat"
chooses the set. A description that states the old value is updated with it, so Claude never
reads a constraint whose words contradict its value.

Tests: `backend/tests/test_standards_editor.py`, `frontend/tests/standardsEditor.test.tsx`,
`frontend/e2e/standards-editor.spec.ts`.
