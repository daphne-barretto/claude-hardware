// Feature flags, read at build time from NEXT_PUBLIC_* variables. Off unless set to "1".

/** "Add folder" in the composer's + menu: attach every file in a folder (e.g. a KiCad project). */
export const ADD_FOLDER = process.env.NEXT_PUBLIC_FEATURE_ADD_FOLDER === "1";

/** Manual mode (Claude proposes changes for review) and the Manual/Auto toggle. Off: no toggle,
 * and every message goes in Auto mode. */
export const MANUAL_MODE = process.env.NEXT_PUBLIC_FEATURE_MANUAL_MODE === "1";

/** Sketch on a face: the canvas toolbar's Sketch button and panel. */
export const SKETCH = process.env.NEXT_PUBLIC_FEATURE_SKETCH === "1";

/** The History panel: the session's versions (builds), to show or restore. Undo and redo stay. */
export const HISTORY = process.env.NEXT_PUBLIC_FEATURE_HISTORY === "1";

/** The modeling toolbar down the canvas's right side (move, push/pull, hole, …). Off, hand edits
 * are made through Dimensions and Parameters, and undo and redo sit in the canvas toolbar. */
export const MODELING_TOOLS = process.env.NEXT_PUBLIC_FEATURE_MODELING_TOOLS === "1";

/** The parts panel's Select filters (face, edge, part, and component), box select, and Select by ID. Off, a click on
 * the model selects a face. */
export const SELECT_FILTERS = process.env.NEXT_PUBLIC_FEATURE_SELECT_FILTERS === "1";

/** Find in model, the parts panel's search ("low draft", "faces on lid"). */
export const FIND = process.env.NEXT_PUBLIC_FEATURE_FIND === "1";

/** "Load next revision" in the chat header: load a board's next revision and Claude updates the
 * design to fit. The demo where the next revision arrives still shows the result. */
export const LOAD_REVISION = process.env.NEXT_PUBLIC_FEATURE_LOAD_REVISION === "1";

/** The canvas toolbar's Explode button and amount slider. The bottom bar's Assembled / Exploded /
 * Print layout switch stays either way. */
export const EXPLODE_TOOL = process.env.NEXT_PUBLIC_FEATURE_EXPLODE_TOOL === "1";

/** "Start from a board file" (KiCad and the like) on a new session. Off, a design starts from a
 * prompt, a STEP file, or a photo. */
export const BOARD_FILES = process.env.NEXT_PUBLIC_FEATURE_BOARD_FILES === "1";

/** The Measure tool: click two points on the model for the distance between them. Off, the
 * model's size shows under the view label, and Dimensions and Parameters show its key lengths. */
export const MEASURE_TOOL = process.env.NEXT_PUBLIC_FEATURE_MEASURE_TOOL === "1";

/** The canvas toolbar's Analysis maps button (wall thickness and draft colored onto the model).
 * Off, the Constraints panel still reports those checks. */
export const MAPS = process.env.NEXT_PUBLIC_FEATURE_MAPS === "1";

/** The canvas toolbar's X-ray button: the enclosure turns see-through to show the board inside.
 * Off, the parts panel's eye icons still hide parts. */
export const XRAY = process.env.NEXT_PUBLIC_FEATURE_XRAY === "1";
