import * as THREE from "three";

/**
 * Camera orientation for the 3D canvas, kept free of the viewer so
 * it can be unit tested.
 *
 * The camera is placed by a turntable: `azimuth` spins it about the board's vertical Z axis,
 * `elevation` tilts it up or down (clamped to straight up and straight down, so it never flips)
 * and `roll` spins it about the line of sight. Dragging changes azimuth and elevation only; roll
 * changes only through the roll buttons, so the model stays level however the pointer moves.
 *
 * Board axes (x right, y back, z up, as in build123d) are three.js (x, z, −y): the front view
 * looks along board +y, from the camera on three.js +z.
 */

export type Orientation = { azimuth: number; elevation: number; roll: number };

export type Vec3 = [number, number, number];

const HALF_PI = Math.PI / 2;
const EPS = 1e-6;

/** Board coordinates to three.js world coordinates. */
export const boardToWorld = ([x, y, z]: Vec3): THREE.Vector3 => new THREE.Vector3(x, z, -y);
/** three.js world coordinates to board coordinates. */
export const worldToBoard = (v: THREE.Vector3): Vec3 => [v.x, -v.z, v.y];

// ------------------------------------------------------------------------------------- Targets

/** The view names along each board axis, positive side first. */
const AXIS_NAMES: [string, string][] = [
  ["Right", "Left"],
  ["Back", "Front"],
  ["Top", "Bottom"],
];
const NAME_ORDER = ["Front", "Back", "Right", "Left", "Top", "Bottom"];

/** A snap target of the view cube: a face, an edge or a corner. */
export type Target = {
  /** e.g. "front", "front-right", "front-right-top". */
  key: string;
  /** e.g. "Front", "Front-Right edge", "Front-Right-Top corner". */
  label: string;
  kind: "face" | "edge" | "corner";
  /** Direction from the model to the camera, in board coordinates (components −1, 0 or 1). */
  dir: Vec3;
};

function targetFor(dir: Vec3): Target {
  const names = dir.flatMap((c, i) => (c === 0 ? [] : [AXIS_NAMES[i][c > 0 ? 0 : 1]]));
  names.sort((a, b) => NAME_ORDER.indexOf(a) - NAME_ORDER.indexOf(b));
  const kind = names.length === 1 ? "face" : names.length === 2 ? "edge" : "corner";
  const joined = names.join("-");
  return {
    key: joined.toLowerCase(),
    label: kind === "face" ? joined : `${joined} ${kind}`,
    kind,
    dir,
  };
}

/** All 26 snap targets: 6 faces, 12 edges and 8 corners. */
export const TARGETS: Target[] = (() => {
  const all: Target[] = [];
  for (const x of [-1, 0, 1])
    for (const y of [-1, 0, 1])
      for (const z of [-1, 0, 1]) if (x || y || z) all.push(targetFor([x, y, z]));
  const rank = { face: 0, edge: 1, corner: 2 };
  return all.sort((a, b) => rank[a.kind] - rank[b.kind]);
})();

export const targetByKey = (key: string): Target | undefined => TARGETS.find((t) => t.key === key);

/** The target a board direction (any length) is, or null. */
export function targetForDirection(dir: Vec3): Target | null {
  const len = Math.hypot(...dir);
  if (len < EPS) return null;
  const n = dir.map((c) => c / len);
  const nonzero = n.filter((c) => Math.abs(c) > 1e-4);
  if (n.some((c) => Math.abs(c) > EPS && Math.abs(c) <= 1e-4)) return null;
  const m = Math.abs(nonzero[0]);
  if (!nonzero.every((c) => Math.abs(Math.abs(c) - m) < 1e-4)) return null;
  return targetFor(n.map((c) => (Math.abs(c) <= 1e-4 ? 0 : Math.sign(c))) as Vec3);
}

/** The home view: the Front-Right-Top corner. */
export const HOME_KEY = "front-right-top";

/**
 * The orientation that looks at a target with Z up on screen. Top and bottom views have the
 * front edge of the model at the bottom of the screen.
 */
export function targetOrientation(target: Target): Orientation {
  const d = boardToWorld(target.dir).normalize();
  if (Math.abs(d.y) > 1 - EPS) return { azimuth: d.y > 0 ? 0 : Math.PI, elevation: Math.sign(d.y) * HALF_PI, roll: 0 };
  return { azimuth: Math.atan2(d.x, d.z), elevation: Math.asin(d.y), roll: 0 };
}

// ------------------------------------------------------------------------------- Orientation

const qAxis = (axis: THREE.Vector3, angle: number) => new THREE.Quaternion().setFromAxisAngle(axis, angle);
const X = new THREE.Vector3(1, 0, 0);
const Y = new THREE.Vector3(0, 1, 0);
const Z = new THREE.Vector3(0, 0, 1);

/** The camera's rotation (camera to world) for an orientation. */
export function toQuaternion(o: Orientation): THREE.Quaternion {
  return qAxis(Y, o.azimuth).multiply(qAxis(X, -o.elevation)).multiply(qAxis(Z, o.roll));
}

/** Direction from the target to the camera, in three.js world space. */
export function cameraDirection(o: Orientation): THREE.Vector3 {
  return Z.clone().applyQuaternion(toQuaternion(o));
}

/** An angle in (−π, π], with values within a hair of a multiple of 45° snapped onto it. */
function tidy(a: number): number {
  let r = Math.atan2(Math.sin(a), Math.cos(a));
  const q = Math.round(r / (Math.PI / 4)) * (Math.PI / 4);
  if (Math.abs(r - q) < 1e-9) r = q;
  if (r <= -Math.PI + 1e-12) r = Math.PI;
  return r === 0 ? 0 : r;
}

/**
 * The orientation of a camera rotation. Looking straight up or down, azimuth and roll turn
 * the same way; `roll` is then kept and the azimuth takes the rest.
 */
export function fromQuaternion(q: THREE.Quaternion, roll = 0): Orientation {
  const d = Z.clone().applyQuaternion(q);
  if (Math.abs(d.y) > 1 - EPS) {
    const m = q.clone().multiply(qAxis(Z, -roll));
    const right = X.clone().applyQuaternion(m);
    return { azimuth: tidy(Math.atan2(-right.z, right.x)), elevation: Math.sign(d.y) * HALF_PI, roll: tidy(roll) };
  }
  const azimuth = Math.atan2(d.x, d.z);
  const elevation = Math.asin(THREE.MathUtils.clamp(d.y, -1, 1));
  const base = qAxis(Y, azimuth).multiply(qAxis(X, -elevation));
  const r = base.invert().multiply(q);
  return { azimuth: tidy(azimuth), elevation: tidyElevation(elevation), roll: tidy(2 * Math.atan2(r.z, r.w)) };
}

function tidyElevation(e: number) {
  const q = Math.round(e / (Math.PI / 4)) * (Math.PI / 4);
  return Math.abs(e - q) < 1e-9 ? q : e;
}

export type TurnDirection = "up" | "down" | "left" | "right";
export type RollDirection = "cw" | "ccw";

/** The camera's own rotation that moves it toward the face on that side of the screen. */
const TURNS: Record<TurnDirection, THREE.Quaternion> = {
  up: qAxis(X, -HALF_PI),
  down: qAxis(X, HALF_PI),
  left: qAxis(Y, -HALF_PI),
  right: qAxis(Y, HALF_PI),
};

/**
 * Turns the view 90° to the neighbouring face on that side of the screen, then levels it again
 * at the roll it had: from Front, up goes to Top and from Top on to Back, with Z up.
 */
export function turn(o: Orientation, direction: TurnDirection): Orientation {
  const q = toQuaternion(o).multiply(TURNS[direction]);
  return { ...fromQuaternion(q, o.roll), roll: o.roll };
}

/** Spins the view 90° about the line of sight; clockwise turns the model clockwise on screen. */
export function roll(o: Orientation, direction: RollDirection): Orientation {
  return { ...o, roll: tidy(o.roll + (direction === "cw" ? HALF_PI : -HALF_PI)) };
}

/** Degrees of turntable orbit per pixel dragged. */
export const ORBIT_DEG_PER_PX = 0.5;

/**
 * Turntable orbit for a pointer move of (dx, dy) CSS pixels: horizontal spins about Z,
 * vertical tilts and stops at straight down and straight up. Roll never changes.
 */
export function orbit(o: Orientation, dx: number, dy: number): Orientation {
  const k = THREE.MathUtils.degToRad(ORBIT_DEG_PER_PX);
  return {
    azimuth: tidy(o.azimuth - dx * k),
    elevation: THREE.MathUtils.clamp(o.elevation + dy * k, -HALF_PI, HALF_PI),
    roll: o.roll,
  };
}

/** The target the camera looks from, or null in a free orbit. */
export function currentTarget(o: Orientation): Target | null {
  return targetForDirection(worldToBoard(cameraDirection(o)));
}

/**
 * Where the board's Z axis points on screen, in degrees clockwise from straight up, or null
 * when looking straight along it.
 */
export function screenUpAngle(q: THREE.Quaternion): number | null {
  const v = Y.clone().applyQuaternion(q.clone().invert());
  if (Math.hypot(v.x, v.y) < 1e-6) return null;
  const deg = THREE.MathUtils.radToDeg(Math.atan2(v.x, v.y));
  return Math.abs(deg) < 1e-6 ? 0 : deg;
}

/** Ease in and out, as CSS `ease` reads: slow start, slow end. */
export function ease(t: number): number {
  return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
}

/** Snaps, turns, rolls and home animate over this many milliseconds. */
export const SNAP_MS = 450;
