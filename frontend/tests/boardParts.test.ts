import { readdirSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import {
  BOARD_PART,
  boardPartNames,
  boardPick,
  isBoardPick,
  resolveBoardPart,
  viewPartsOf,
} from "@/components/canvas/boardParts";
import { componentBody, type Board, type BoardComponent } from "@/components/canvas/inspect";
import { readRepoFile } from "./fixtures";

// Every board revision in demos/b-parasite/: the scenario comes from there, never from here.
const files = readdirSync(resolve(__dirname, "../../demos/b-parasite")).filter((f) => f.endsWith(".json"));
const boards = files.map((f) => [f, JSON.parse(readRepoFile(`demos/b-parasite/${f}`)) as Board] as const);

/** A component's box worked out from the board file alone: the rotated rectangle's half-widths, and its side's z range. */
function expectedExtents(board: Board, c: BoardComponent) {
  const r = (c.rotation * Math.PI) / 180;
  const [w, l] = c.size;
  const hx = (Math.abs(w * Math.cos(r)) + Math.abs(l * Math.sin(r))) / 2;
  const hy = (Math.abs(w * Math.sin(r)) + Math.abs(l * Math.cos(r))) / 2;
  const h = Math.max(c.height, 1e-3);
  const t = board.pcb.thickness;
  const [z0, z1] = c.side === "top" ? [t, t + h] : [-h, 0];
  return { min: [c.position[0] - hx, c.position[1] - hy, z0], max: [c.position[0] + hx, c.position[1] + hy, z1] };
}

/** The drawn box's extents: the body the canvas extrudes for the component (viewer.buildBoard). */
function drawnExtents(board: Board, c: BoardComponent) {
  const b = componentBody(board, c);
  const xs = b.corners.map((p) => p[0]);
  const ys = b.corners.map((p) => p[1]);
  return { min: [Math.min(...xs), Math.min(...ys), b.z0], max: [Math.max(...xs), Math.max(...ys), b.z1] };
}

const close = (a: number[], b: number[]) => a.forEach((v, i) => expect(v).toBeCloseTo(b[i], 6));

describe("board component boxes", () => {
  it("finds board files with top and bottom components", () => {
    expect(boards.length).toBeGreaterThan(0);
    for (const [, board] of boards) {
      expect(board.components.some((c) => c.side === "top")).toBe(true);
      expect(board.components.some((c) => c.side === "bottom")).toBe(true);
    }
  });

  for (const [file, board] of boards)
    for (const side of ["top", "bottom"] as const)
      it(`draws the first ${side} component of ${file} at its file extents`, () => {
        const c = board.components.find((x) => x.side === side)!;
        const got = drawnExtents(board, c);
        const want = expectedExtents(board, c);
        close(got.min, want.min);
        close(got.max, want.max);
        // Above the board's top face, or below its bottom face.
        if (side === "top") expect(got.min[2]).toBeCloseTo(board.pcb.thickness, 6);
        else expect(got.max[2]).toBe(0);
      });

  it("draws every component of every board at its file extents", () => {
    for (const [, board] of boards)
      for (const c of board.components) {
        const got = drawnExtents(board, c);
        const want = expectedExtents(board, c);
        close(got.min, want.min);
        close(got.max, want.max);
      }
  });
});

describe("board parts", () => {
  const board = boards[0][1];
  const refs = board.components.map((c) => c.ref);

  it("names the board, then each component by its reference designator", () => {
    expect(boardPartNames(board)).toEqual([BOARD_PART, ...refs]);
  });

  it("leaves out a name the enclosure already uses", () => {
    expect(boardPartNames(board, [refs[0]])).toEqual([BOARD_PART, ...refs.slice(1)]);
  });

  it("picks the board as a part and a component by its reference, on the board", () => {
    expect(boardPick(BOARD_PART)).toEqual({ kind: "part", id: BOARD_PART, part: BOARD_PART });
    expect(boardPick(refs[0])).toEqual({ kind: "component", id: refs[0], part: BOARD_PART });
    expect(isBoardPick(boardPick(refs[0]))).toBe(true);
    // A component picked through its opening on an enclosure part is not a board part.
    expect(isBoardPick({ kind: "component", id: refs[0], part: "some-part" })).toBe(false);
  });

  it("hides a picked component as its own part, and anything else by its part", () => {
    const opening = { kind: "component" as const, id: refs[0], part: "some-part" };
    expect(viewPartsOf([boardPick(refs[0]), opening, boardPick(BOARD_PART)])).toEqual([refs[0], "some-part", BOARD_PART]);
  });

  it("resolves a typed reference designator, ignoring case", () => {
    const names = boardPartNames(board);
    expect(resolveBoardPart(names, ` ${refs[0].toLowerCase()} `)).toEqual([boardPick(refs[0])]);
    expect(resolveBoardPart(names, "no such part")).toEqual([]);
  });
});
