import * as THREE from "three";
import { describe, expect, it } from "vitest";
import {
  boardToWorld,
  cameraDirection,
  currentTarget,
  fromQuaternion,
  HOME_KEY,
  orbit,
  roll,
  screenUpAngle,
  targetByKey,
  targetOrientation,
  TARGETS,
  toQuaternion,
  turn,
  type Orientation,
  type Vec3,
} from "@/components/canvas/orientation";

// The view cube's orientation rules, as pure functions.

/** Where a board direction points on screen: [right, up] in the camera's image plane. */
function onScreen(o: Orientation, dir: Vec3): [number, number] {
  const v = boardToWorld(dir).applyQuaternion(toQuaternion(o).invert());
  return [v.x, v.y];
}

const deg = (r: number) => THREE.MathUtils.radToDeg(r);
const face = (key: string) => targetOrientation(targetByKey(key)!);

describe("snap targets", () => {
  it("has 26 targets: 6 faces, 12 edges and 8 corners, each named once", () => {
    expect(TARGETS).toHaveLength(26);
    expect(TARGETS.filter((t) => t.kind === "face")).toHaveLength(6);
    expect(TARGETS.filter((t) => t.kind === "edge")).toHaveLength(12);
    expect(TARGETS.filter((t) => t.kind === "corner")).toHaveLength(8);
    expect(new Set(TARGETS.map((t) => t.label)).size).toBe(26);
    expect(targetByKey("front-right")!.label).toBe("Front-Right edge");
    expect(targetByKey(HOME_KEY)!.label).toBe("Front-Right-Top corner");
    expect(targetByKey("front")!.dir).toEqual([0, -1, 0]);
    expect(targetByKey("top")!.dir).toEqual([0, 0, 1]);
  });

  it("snaps every target to its view with Z up on screen and no roll", () => {
    for (const t of TARGETS) {
      const o = targetOrientation(t);
      expect(currentTarget(o)?.key, t.label).toBe(t.key);
      expect(o.roll).toBe(0);
      const up = screenUpAngle(toQuaternion(o));
      if (t.key === "top" || t.key === "bottom") expect(up, t.label).toBeNull();
      else expect(up!, t.label).toBeCloseTo(0, 6);
    }
  });

  it("puts the front edge at the bottom of the screen in the top and bottom views", () => {
    for (const key of ["top", "bottom"]) {
      const [x, y] = onScreen(face(key), [0, -1, 0]);
      expect(x, key).toBeCloseTo(0, 6);
      expect(y, key).toBeCloseTo(-1, 6);
    }
  });

  it("looks at the front face from the front: board x right, z up", () => {
    const [xr, xu] = onScreen(face("front"), [1, 0, 0]);
    expect([xr, xu].map((v) => +v.toFixed(6))).toEqual([1, 0]);
    expect(cameraDirection(face("front")).toArray().map((v) => +v.toFixed(6))).toEqual([0, 0, 1]);
  });
});

describe("turns and rolls", () => {
  const neighbours: [string, "up" | "down" | "left" | "right", string][] = [
    ["front", "up", "top"],
    ["front", "down", "bottom"],
    ["front", "right", "right"],
    ["front", "left", "left"],
    ["top", "up", "back"],
    ["top", "down", "front"],
    ["top", "right", "right"],
    ["right", "right", "back"],
    ["back", "right", "left"],
    ["bottom", "up", "back"],
    ["left", "up", "top"],
  ];

  it.each(neighbours)("from %s, %s turns to %s with Z up", (from, dir, to) => {
    const o = turn(face(from), dir);
    expect(currentTarget(o)?.key).toBe(to);
    expect(o.roll).toBe(0);
    const up = screenUpAngle(toQuaternion(o));
    if (to !== "top" && to !== "bottom") expect(up!).toBeCloseTo(0, 6);
  });

  it("turns from every face in every direction to another face", () => {
    for (const t of TARGETS.filter((t) => t.kind === "face"))
      for (const dir of ["up", "down", "left", "right"] as const) {
        const o = turn(targetOrientation(t), dir);
        expect(currentTarget(o)?.kind, `${t.key} ${dir}`).toBe("face");
        expect(currentTarget(o)?.key).not.toBe(t.key);
      }
  });

  it("four turns the same way come back to the start from a face", () => {
    for (const dir of ["up", "left"] as const) {
      let o = face("front");
      for (let i = 0; i < 4; i++) o = turn(o, dir);
      expect(currentTarget(o)?.key).toBe("front");
    }
  });

  it("rolls clockwise and counterclockwise by 90° about the line of sight", () => {
    const cw = roll(face("front"), "cw");
    expect(currentTarget(cw)?.key).toBe("front");
    expect(deg(cw.roll)).toBeCloseTo(90);
    // Clockwise turns the model clockwise: Z now points right on screen.
    expect(screenUpAngle(toQuaternion(cw))!).toBeCloseTo(90);
    expect(roll(cw, "ccw").roll).toBe(0);
    let o = face("front");
    for (let i = 0; i < 4; i++) o = roll(o, "ccw");
    expect(o.roll).toBe(0);
  });

  it("keeps the roll when turning: up on a rolled view goes to the face above on screen", () => {
    const rolled = roll(face("front"), "cw");
    const o = turn(rolled, "up");
    expect(deg(o.roll)).toBeCloseTo(90);
    // With Z pointing right, the face above on screen is Left.
    expect(currentTarget(o)?.key).toBe("left");
  });

  it("round-trips orientations through the camera rotation", () => {
    for (const t of TARGETS) {
      const o = { ...targetOrientation(t), roll: Math.PI / 2 };
      const back = fromQuaternion(toQuaternion(o), o.roll);
      expect(toQuaternion(back).angleTo(toQuaternion(o))).toBeLessThan(1e-6);
    }
  });
});

describe("turntable orbit", () => {
  it("never changes roll and keeps Z up on screen over several full circles", () => {
    let o = face(HOME_KEY);
    for (let lap = 0; lap < 3; lap++)
      for (let a = 0; a < 360; a += 5) {
        const r = THREE.MathUtils.degToRad(a);
        o = orbit(o, Math.cos(r) * 20, Math.sin(r) * 20);
      }
    expect(o.roll).toBe(0);
    expect(screenUpAngle(toQuaternion(o))!).toBeCloseTo(0, 6);
  });

  it("stops at straight down and straight up", () => {
    let o = face("front");
    for (let i = 0; i < 50; i++) o = orbit(o, 3, 40);
    expect(deg(o.elevation)).toBeCloseTo(90);
    expect(o.roll).toBe(0);
    for (let i = 0; i < 100; i++) o = orbit(o, -3, -40);
    expect(deg(o.elevation)).toBeCloseTo(-90);
    o = orbit(o, 0, 10);
    expect(deg(o.elevation)).toBeCloseTo(-85);
  });

  it("spins about Z for a horizontal drag", () => {
    const o = orbit(face("front"), 90 / 0.5, 0);
    expect(deg(o.elevation)).toBeCloseTo(0);
    // Dragging right turns the model's front toward the right: the camera moves to the left.
    expect(currentTarget(o)?.key).toBe("left");
  });
});
