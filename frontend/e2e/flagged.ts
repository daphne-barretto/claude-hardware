// Browser tests for features behind feature flags (src/lib/flags.ts). The browser tests run the
// app as shipped, every flag off, so these are skipped (playwright.config.ts). To test a flagged
// feature, turn its flag on (NEXT_PUBLIC_FEATURE_<NAME>=1) and run its spec on its own.

/** Specs that are only about flagged features. */
export const FLAGGED_SPECS = [
  "exploded.spec.ts", // EXPLODE_TOOL
  "find.spec.ts", // FIND
  "history.spec.ts", // HISTORY
  "modeling.spec.ts", // MODELING_TOOLS
  "revision.spec.ts", // LOAD_REVISION
  "select-help.spec.ts", // SELECT_FILTERS
  "selection.spec.ts", // SELECT_FILTERS
  "sketch.spec.ts", // SKETCH
  "undo-history.spec.ts", // MODELING_TOOLS
  "canvas-layout.spec.ts", // HISTORY and MODELING_TOOLS chrome
  "maps.spec.ts", // MAPS
];

/** Tests in other specs that use a flagged feature, by title. */
export const FLAGGED_TESTS = [
  "send, stream and answer against the mock API", // MANUAL_MODE
  "pin a comment, send it to Claude, accept and undo its change, and resolve it", // MANUAL_MODE
  "send, stream and answer a live run, then ask about a selected face", // MANUAL_MODE
  "the parameters panel is above the toolbar and view cube, every control reachable", // MODELING_TOOLS
  "the sketch panel is inside the canvas, every control reachable", // SKETCH
  "the revision menu lists the builds and shows the selected one", // HISTORY
  "no pin draws over the parts panel, the view cube or the toolbar, and every pin can be clicked", // HISTORY
  "the check results match the reference screenshot", // HISTORY
  "a demo link opens the viewer", // LOAD_REVISION
  "the demo ", // MODELING_TOOLS
  "clicking a feature selects its faces; selecting a face highlights its feature", // SELECT_FILTERS
  "a rebuild that moves an opening keeps the selected face", // SELECT_FILTERS
  "select by ID finds the board and its components after the enclosure", // SELECT_FILTERS
  "flagged risks show in chat with Accept and Ignore, and on the canvas", // LOAD_REVISION
  "New → message → build on the canvas → reload keeps it → Recents", // HISTORY
  "H hides the selected parts, I isolates them and Alt+H shows all", // SELECT_FILTERS
  "S switches to the select tool", // SELECT_FILTERS
  "opening compare and history are reported as views", // HISTORY
  "the map legend is inside the canvas and names the standard, not its ID", // MAPS
  "picking an analysis map is reported as a view", // MAPS
  "the analysis maps render from the real values with a matching legend", // MAPS
  "x-ray shows the board and its components through the enclosure", // XRAY
];

const escape = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
/** One pattern matching every flagged test title, for `grepInvert`. */
export const FLAGGED_TITLES = new RegExp(FLAGGED_TESTS.map(escape).join("|"));
