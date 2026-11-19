import type { Board } from "./inspect";
import type { Picked } from "./selection";

/**
 * The build's board revision as parts of the parts panel, kept free of three.js. The board
 * itself is the part `board`; each component is a part named by its reference designator. They
 * hide, isolate and turn see-through like the enclosure's parts (the same `PartViews`), and a
 * click or a row selects them whole.
 */

/** The part name of the board itself. */
export const BOARD_PART = "board";

/**
 * The board's part names: the board, then its components in file order. A name the enclosure
 * already uses is left out, so the two never share a part view.
 */
export function boardPartNames(board: Pick<Board, "components">, enclosureParts: string[] = []): string[] {
  const taken = new Set(enclosureParts);
  return [BOARD_PART, ...board.components.map((c) => c.ref)].filter((p) => !taken.has(p));
}

/**
 * The pick for a board part: the board as a part, or a component by its reference designator
 * (`part` is the board, the part it sits on).
 */
export function boardPick(name: string): Picked {
  return name === BOARD_PART
    ? { kind: "part", id: BOARD_PART, part: BOARD_PART }
    : { kind: "component", id: name, part: BOARD_PART };
}

/** Whether a pick is a board part picked whole (not a component picked through its opening). */
export function isBoardPick(p: Picked): boolean {
  return p.part === BOARD_PART && (p.kind === "component" || (p.kind === "part" && p.id === BOARD_PART));
}

/** The board part a pick is, if it is one. */
export function boardPartOf(p: Picked): string | null {
  return isBoardPick(p) ? p.id : null;
}

/** The part views the picks belong to: a board component is its own part, anything else its part. */
export function viewPartsOf(picks: Picked[]): string[] {
  return [...new Set(picks.map((p) => boardPartOf(p) ?? p.part))];
}

/** A board part typed by name (any case), for select by ID. */
export function resolveBoardPart(names: string[], query: string): Picked[] {
  const q = query.trim().toLowerCase();
  const name = q ? names.find((n) => n.toLowerCase() === q) : undefined;
  return name ? [boardPick(name)] : [];
}
