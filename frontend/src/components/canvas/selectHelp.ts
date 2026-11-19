import type { PickKind } from "./selection";

/**
 * What the parts panel's Select filters and box select do, in plain words: each filter's
 * tooltip, and the one-line hint under them until the first selection, or while box select is on.
 */
export const PICK_TIPS: Record<PickKind, string> = {
  face: "Click a face on the model",
  edge: "Click an edge",
  part: "Click a whole part",
  component: "Click a board component",
};

export const BOX_TIP = "Drag a box on the model to select several";
export const BOX_ON_HINT = "Drag on the model · Esc to stop";

const HINT_WORDS: Record<PickKind, string> = { face: "a face", edge: "an edge", part: "a part", component: "a component" };

/**
 * The hint under the filters: how to select in the current mode, until something has been
 * selected in this session; while box select is on, always how to use it and how to stop.
 */
export function selectHint(kind: PickKind, boxMode: boolean, selectedOnce: boolean): string | null {
  if (boxMode) return BOX_ON_HINT;
  if (selectedOnce) return null;
  return `Click the model to select ${HINT_WORDS[kind]}`;
}
