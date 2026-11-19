# Fixtures

Sample data in the formats of `contracts/`, so frontend work can run without the geometry and agent backends. Everything here except this README is generated from `demos/b-parasite/` and `standards.yaml` by `backend/app/fixtures/`. Do not edit the files by hand. Regenerate them with:

```sh
cd backend && .venv/bin/python -m app.fixtures
```

`make test` fails if they are out of date, if a file is not generated, or if a fixture does not validate against its contract.

The scenario: a sample enclosure designed for the older board revision named in `demos/b-parasite/CHANGES.md` is checked against the newer one. Every difference in `CHANGES.md` fails a check at its component.

| File | Contract | What |
|---|---|---|
| `enclosure.gltf` | Mesh | The sample enclosure: `base` and `lid` parts, stable face and edge IDs, features `base`, `lid`, `opening_<ref>`, `boss_<n>` and `insert_<n>` |
| `checks.json` | Check results | Its checks against the newer board: standards, board openings and the untested list |
| `maps/wall_thickness.json`, `maps/draft.json` | Analysis maps | Per-vertex values aligned with `enclosure.gltf`, with flagged spots |
| `build.json` | Session API `Build` | The build that `build_ready` announces. Its URLs are relative to `/api/sessions/{id}/` |
| `previous-build.json` | Session API `Build` | The same enclosure's build on the older revision, the board it was designed for: the session's starting point, announced just before `build.json` |
| `previous-checks.json` | Check results | Its checks against the older board |
| `diff.json` | Session API `BuildDiff` | The diff (`geometry.diff`) from the enclosure generator's design for the older revision to its design for the newer one: parameters changed, and features added, removed, moved or rotated, with distances |
| `build-source.json` | Session API `BuildSource` | The build's CAD source: the default geometry backend's enclosure generator (`backend/geometry/backends/<backend>/examples/enclosure.py`), and the lines that create each feature, recorded by running it with the parameters of the board the enclosure was designed for |
| `parameters.json` | Session API `Parameter[]` | The design parameters |
| `standards.json` | Session API `StandardsSet[]` | The team standards sets, as `GET /api/standards` lists them |
| `session.json` | - | Default title, board, a suggested first prompt for a new session, and `conflict_prompt`, the message that plays the conflict run in the mock |
| `change-set.json` | Change set | What a run fixing the recorded session would propose, in manual mode with nothing reviewed: one change per `demos/b-parasite/CHANGES.md` row (the opening for that component moved, turned or added for the newer board), made by `app.agent.proposal` with `checks.json` as the checks. The mock API serves it after the recorded question is answered |
| `risk-flags.json` | Risk flags | The risks a run on the recorded session would flag, nothing reviewed: what `app.agent.risks` finds on `checks.json` and the maps, a flag for each thick boss with its fix (slim the bosses as far as the insert allows, a minimum wall around it), checked on the fixture enclosure: rebuilt with it, the risk is smaller, and `resolves` says whether it is gone. The mock API serves it with the change set |
| `session-events.jsonl` | Session events | The mock API's scripted run: one `{id, delay_ms, event}` per line, ending with the builds on both revisions and a question with options |
| `conflict-events.jsonl` | Session events | The can't-satisfy run: the conflict fixture's locks from conflict detection on the newer board, a build, and a question with the conflict (clashing constraints, best values, locations snapped to faces of `enclosure.gltf`), pinned where it is worst, with options for what gives |

The enclosure is a simplified stand-in, not the output of the enclosure generator. It is built in pure Python so the output is deterministic: the existing case's bounding box, drafted walls split at the board's top face, openings cut through the lid, and drafted bosses over the mounting holes, each with a drafted pocket for the standard heat-set insert. Wall thickness comes from the design rather than from ray casting. It has no snap fits, no separate insert parts, no screw holes in the base and no slot where the board leaves the case. On the revision it was designed for it passes every check (`previous-checks.json`). So `build-source.json` shows the code a real build runs, not code that reproduces `enclosure.gltf`.

## Demos

The demo sessions and the example board they use are not here: they live in `demos/` (`demos/README.md`).
