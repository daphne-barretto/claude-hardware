# b-parasite

The demo scenario as data: two revisions of the open-source [b-parasite](https://github.com/rbaron/b-parasite) soil moisture sensor, the enclosure made for the earlier one, and the differences between the revisions that affect the enclosure. This directory is the single source of truth for the scenario. Application code and tests read it and never restate it.

| File | Contents |
|---|---|
| `b-parasite-1.2.json` | Revision 1.2, from b-parasite commit `6528565`. The board the existing enclosure was made for. |
| `b-parasite-2.0.json` | Revision 2.0, from b-parasite commit `15920b1`. The new revision. |
| `CHANGES.md` | Every difference between the two revisions that affects the enclosure, and what each one breaks. |
| `b-parasite-case.step` | The team's existing enclosure, the demo's starting design (see below). |
| `source/` | Converter inputs: the KiCad files, `revisions.json` and `footprints.json`. |
| `examples.yaml` | The board revision example a new session can open (`GET /api/examples`). |
| `demo-script.md` | The end-to-end walk through the scenario that the session API tests follow. |
| `LICENSE` | CC BY-SA 4.0, with attribution. |

## The existing enclosure

`b-parasite-case.step` is an unmodified copy of b-parasite's `case/Case.step`, last changed in commit `8b80fe6` ("Reorganized case files", April 2022). It is the snap-on case made for the 1.x boards, which the 2.0 board (November 2022) post-dates. The copy at `6528565`, the 1.2 commit, is older: it has no vent holes and no light-sensor cutout.

The STEP file holds two top-level shapes:

- `Case`: three solids, unnamed in the file. Tell them apart by geometry:
  - the bottom, the solid that reaches lowest (z down to -7.5), with two pegs that locate the board through its mounting holes;
  - the top with openings (the smaller of the two tops by volume): five 1 mm vent holes over the 1.2 humidity sensor and a cutout over the 1.2 light sensor;
  - a plain top with no openings.
- `b-parasite_PCB_v2`: a reference board from an earlier, shorter layout (110.4 mm long instead of 126.4 mm) with some of its parts. It is not one of the revisions here, so use the board JSON files instead.

The case uses its own axes. Board coordinates (see `backend/app/boards/SCHEMA.md`) map to case coordinates as:

```text
case x = 126.414 - board y    (the board's top edge is at case x = 0; the case covers case x -4.4 to 47.2)
case y = board x - 11         (the board is centred on case y = 0)
case z = board z              (the board's bottom face is at z = 0, its top face at z = 1.6)
```

`backend/tests/test_boards.py` checks this mapping with exact solid booleans. The bottom's pegs go through the 1.2 mounting holes, and the top's openings sit over the 1.2 sensor window and vent positions but not the 2.0 ones.

## License

The files here are derived from b-parasite by rbaron, whose hardware is licensed under CC BY-SA 4.0, and are shared under the same license. See `LICENSE`.
