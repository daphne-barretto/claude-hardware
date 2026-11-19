# Board changes that affect the enclosure

From `b-parasite-1.2.json` to `b-parasite-2.0.json`.

This is the list of differences the enclosure has to absorb when the board moves from revision 1.2 to 2.0. A difference counts when it changes the board outline or thickness, a mounting hole, an opening the enclosure must provide (sensor window, vent, button, antenna keep-out), or the tallest part on either side.

Positions are board coordinates in mm (see `backend/app/boards/SCHEMA.md`). The existing enclosure is `b-parasite-case.step`, its top with vent holes and a light-sensor cutout made for revision 1.2 (see `README.md`).

| Component | Role | Opening | Change | Old position (mm) | Old rotation | New position (mm) | New rotation | What it breaks |
|---|---|---|---|---|---|---|---|---|
| `Q4` | light sensor | `sensor_window` | moved and rotated | (1.90, 123.71) | 0° | (1.08, 120.74) | 90° | The case's light-sensor cutout spans y 121.9 to 127.0. The sensor now lies along y (119.3 to 122.1) and sits mostly under the solid top, so it no longer sees light. The window must move down and turn 90°. |
| `RST1` | push button | `button` | added | - | - | (15.30, 103.21) | 270° | The case has no button openings, so the reset button cannot be pressed with the case closed. It also sits directly under the 1 mm vent holes that were placed over the old humidity sensor position. |
| `SW1` | push button | `button` | added | - | - | (6.92, 103.21) | 270° | The case has no button openings, so the user button cannot be pressed with the case closed. |
| `U2` | humidity sensor | `vent` | moved | (15.80, 102.81) | 180° | (17.08, 97.37) | 180° | The case's five vent holes are centred on the old position (x 13.9 to 17.8, y 101.1 to 104.6). The sensor is now about 5.6 mm away under solid wall, so it reads the air sealed inside the case. The vents must follow the sensor. |

## Checked, and not enclosure-affecting

- **Radio module `U1`.** The footprint changed library, from `nrfmicro:E73-2G4M08S1C-52840` at KiCad rotation 90° to `snapeda:E73-2G4M08S1C` at 0°. The two libraries draw the module in different orientations: both revisions put the antenna end at the top edge of the board, and every pad keeps its corner, so the module did not rotate. Its body centre moves 0.08 mm and its antenna keep-out stays put, both within tolerance.
- **Battery holder `BT1`.** It moves 0.26 mm along y on the bottom side. It needs no opening, and the tallest bottom-side part is unchanged.
- **Outline, thickness and mounting holes.** Identical in both revisions.
- **Other parts.** Passives, test pads and the status LED moved, and some were added or removed. None needs an opening, and the tallest top-side part (the radio module) is unchanged.

## Maintainer notes

Nothing from this heading on is given to Claude (`app.boards.changes.agent_text`): keep the facts above it and notes on keeping this file below it.

- The rules and tolerances are in `backend/app/boards/changes.py`. `make test` recomputes the differences from the two JSON files and fails unless the table above matches them row for row.
- The original brief expected the radio module `U1` to move and rotate; the files show it does neither (see above).
