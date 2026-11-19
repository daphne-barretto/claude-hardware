"use client";

import { ChevronDown, ChevronLeft, ChevronRight, ChevronUp, House, RotateCcw, RotateCw, type LucideIcon } from "lucide-react";
import { useEffect, useRef, useState, type KeyboardEvent, type PointerEvent as ReactPointerEvent } from "react";
import { boardToWorld, targetForDirection, type RollDirection, type Target, type TurnDirection, type Vec3 } from "./orientation";

// The view cube and its turn, roll and home buttons.

/** The control area, the cube's edge and the edge and corner bands, in CSS pixels. */
export const CONTROL_SIZE = 150;
export const CUBE_SIZE = 66;
export const BAND = 14;
/** A press that moves less than this is a click (the cube snaps), more is a drag (it orbits). */
const CLICK_SLOP_PX = 4;

/** Axis colours, CAD convention: X red, Y green, Z blue. */
export const AXIS_RGB = { x: "226, 75, 74", y: "99, 153, 34", z: "55, 138, 221" } as const;
const FACE_BG = "#30302d";
const FACE_LINE = "rgba(236, 235, 230, 0.26)";
const EDGE_HOVER = "rgba(136, 135, 128, 0.34)";

type Face = {
  name: string;
  /** Outward normal, the face's right and its down, in board coordinates. */
  normal: Vec3;
  right: Vec3;
  down: Vec3;
  /** Placement on the cube; CSS has y down, so "top" turns up with rotateX(90deg). */
  transform: string;
  rgb: string;
};

const H = CUBE_SIZE / 2;
export const FACES: Face[] = [
  { name: "Front", normal: [0, -1, 0], right: [1, 0, 0], down: [0, 0, -1], transform: `translateZ(${H}px)`, rgb: AXIS_RGB.y },
  { name: "Back", normal: [0, 1, 0], right: [-1, 0, 0], down: [0, 0, -1], transform: `rotateY(180deg) translateZ(${H}px)`, rgb: AXIS_RGB.y },
  { name: "Right", normal: [1, 0, 0], right: [0, 1, 0], down: [0, 0, -1], transform: `rotateY(90deg) translateZ(${H}px)`, rgb: AXIS_RGB.x },
  { name: "Left", normal: [-1, 0, 0], right: [0, -1, 0], down: [0, 0, -1], transform: `rotateY(-90deg) translateZ(${H}px)`, rgb: AXIS_RGB.x },
  { name: "Top", normal: [0, 0, 1], right: [1, 0, 0], down: [0, -1, 0], transform: `rotateX(90deg) translateZ(${H}px)`, rgb: AXIS_RGB.z },
  { name: "Bottom", normal: [0, 0, -1], right: [1, 0, 0], down: [0, 1, 0], transform: `rotateX(-90deg) translateZ(${H}px)`, rgb: AXIS_RGB.z },
];

type Cell = { face: Face; target: Target; row: number; col: number };

/** Each face's 3 × 3 grid of cells, row by row, and the target each cell snaps to. */
export const CELLS: Cell[] = FACES.flatMap((face) =>
  [-1, 0, 1].flatMap((j) =>
    [-1, 0, 1].map((i) => {
      const dir = face.normal.map((n, k) => n + i * face.right[k] + j * face.down[k]) as Vec3;
      return { face, target: targetForDirection(dir)!, row: j + 1, col: i + 1 };
    }),
  ),
);

/** Below this a face is turned away or seen edge-on, too thin to click. */
const MIN_FACING = 0.2;

/**
 * How squarely a face looks at the camera: the view-space z of its normal under the camera's
 * view rotation (column-major), 1 when it faces the camera, ≤ 0 when it's turned away.
 */
export function facing(rotation: number[], normal: Vec3): number {
  const n = boardToWorld(normal);
  return rotation[2] * n.x + rotation[6] * n.y + rotation[10] * n.z;
}

/**
 * For each target, the face whose cell carries its accessible control: of the faces that hold
 * it, the one that looks most squarely at the camera. Targets on no visible face have none, so
 * nothing a pointer can't reach is in the accessibility tree or the focus order.
 */
export function controlFaces(rotation: number[]): Map<string, string> {
  const best = new Map<string, { face: string; f: number }>();
  for (const face of FACES) {
    const f = facing(rotation, face.normal);
    if (f < MIN_FACING) continue;
    for (const c of CELLS)
      if (c.face === face && (best.get(c.target.key)?.f ?? -Infinity) < f) best.set(c.target.key, { face: face.name, f });
  }
  return new Map([...best].map(([k, v]) => [k, v.face]));
}

/**
 * The camera's view rotation (column-major, three.js y up) as a CSS matrix3d (y down):
 * conjugating with diag(1, −1, 1) negates the entries that mix y with x or z.
 */
export function cssRotation(rotation: number[]): string {
  const m = rotation.slice(0, 16);
  for (const i of [1, 4, 6, 9]) m[i] = -m[i];
  return `matrix3d(${m.map((v) => (Math.abs(v) < 1e-10 ? 0 : Number(v.toFixed(6)))).join(",")})`;
}

const BUTTONS: { id: TurnDirection | RollDirection | "home"; label: string; icon: LucideIcon; style: React.CSSProperties }[] = [
  { id: "up", label: "Rotate to the face above", icon: ChevronUp, style: { left: 59, top: 0 } },
  { id: "down", label: "Rotate to the face below", icon: ChevronDown, style: { left: 59, bottom: 0 } },
  { id: "left", label: "Rotate to the face on the left", icon: ChevronLeft, style: { left: 0, top: 59 } },
  { id: "right", label: "Rotate to the face on the right", icon: ChevronRight, style: { right: 0, top: 59 } },
  { id: "ccw", label: "Roll 90 degrees counterclockwise", icon: RotateCcw, style: { left: 0, top: 0 } },
  { id: "cw", label: "Roll 90 degrees clockwise", icon: RotateCw, style: { right: 0, top: 0 } },
  { id: "home", label: "Home view", icon: House, style: { right: 0, bottom: 0 } },
];

/**
 * A cube that turns with the camera. Each face is a 3 × 3 grid: the middle snaps to the face,
 * the 14 px bands to the edges and the corner squares to the corners. An edge or corner is one
 * control across the faces that hold it: one highlight, one accessible name, one snap. Dragging
 * the cube orbits like dragging the model.
 */
export function ViewCube({
  rotation,
  view,
  onSnap,
  onTurn,
  onRoll,
  onHome,
  onOrbit,
  onHover,
}: {
  rotation: number[];
  /** The current view key (see viewKey in ./viewer). */
  view: string;
  onSnap: (key: string) => void;
  onTurn: (direction: TurnDirection) => void;
  onRoll: (direction: RollDirection) => void;
  onHome: () => void;
  onOrbit: (dx: number, dy: number) => void;
  /** The target under the pointer, for the view label, or null. */
  onHover: (target: Target | null) => void;
}) {
  const [hovered, setHovered] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const drag = useRef<{ x0: number; y0: number; x: number; y: number; moved: boolean } | null>(null);
  // A drag ends with a click on whatever cell is under the pointer; that click must not snap.
  const suppressClick = useRef(false);
  const owners = controlFaces(rotation);
  const onOrbitRef = useRef(onOrbit);
  const onHoverRef = useRef(onHover);
  useEffect(() => {
    onOrbitRef.current = onOrbit;
    onHoverRef.current = onHover;
  });

  useEffect(() => {
    if (!dragging) return;
    const move = (e: PointerEvent) => {
      const d = drag.current;
      if (!d) return;
      if (!d.moved && Math.hypot(e.clientX - d.x0, e.clientY - d.y0) < CLICK_SLOP_PX) return;
      if (!d.moved) {
        setHovered(null);
        onHoverRef.current(null);
      }
      d.moved = true;
      onOrbitRef.current(e.clientX - d.x, e.clientY - d.y);
      d.x = e.clientX;
      d.y = e.clientY;
    };
    const up = () => {
      if (drag.current?.moved) {
        suppressClick.current = true;
        setTimeout(() => (suppressClick.current = false), 0);
      }
      drag.current = null;
      setDragging(false);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", up);
    return () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", up);
    };
  }, [dragging]);

  const startDrag = (e: ReactPointerEvent) => {
    if (e.button !== 0 || (e.target as Element).closest("button")) return;
    drag.current = { x0: e.clientX, y0: e.clientY, x: e.clientX, y: e.clientY, moved: false };
    setDragging(true);
  };

  const hover = (t: Target | null) => {
    if (drag.current?.moved) return;
    setHovered(t?.key ?? null);
    onHover(t);
  };

  const press = (e: KeyboardEvent, key: string) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    e.preventDefault();
    e.stopPropagation();
    onSnap(key);
  };

  const run = (id: (typeof BUTTONS)[number]["id"]) => {
    if (id === "home") onHome();
    else if (id === "cw" || id === "ccw") onRoll(id);
    else onTurn(id);
  };

  return (
    <div
      data-testid="view-cube"
      onPointerDown={startDrag}
      className={`relative touch-none select-none ${dragging ? "cursor-grabbing" : "cursor-grab"}`}
      style={{ width: CONTROL_SIZE, height: CONTROL_SIZE }}
    >
      {/* Above the buttons: where the turned cube reaches into a button's square, the cube takes
          the pointer; everywhere else the layer lets it through. */}
      <div className="pointer-events-none absolute inset-0 z-10" style={{ perspective: 520 }}>
        <div
          data-testid="view-cube-body"
          className="pointer-events-none absolute"
          style={{
            left: CONTROL_SIZE / 2,
            top: CONTROL_SIZE / 2,
            width: 0,
            height: 0,
            transformStyle: "preserve-3d",
            transform: cssRotation(rotation),
          }}
        >
          {FACES.map((face) => (
            <div
              key={face.name}
              data-face={face.name}
              className="absolute grid"
              style={{
                width: CUBE_SIZE,
                height: CUBE_SIZE,
                left: -H,
                top: -H,
                transform: face.transform,
                backfaceVisibility: "hidden",
                gridTemplateColumns: `${BAND}px 1fr ${BAND}px`,
                gridTemplateRows: `${BAND}px 1fr ${BAND}px`,
                background: FACE_BG,
                border: `1px solid ${FACE_LINE}`,
                pointerEvents: facing(rotation, face.normal) >= MIN_FACING ? "auto" : "none",
              }}
            >
              {CELLS.filter((c) => c.face === face).map((c) => {
                const t = c.target;
                const center = t.kind === "face";
                const isHovered = hovered === t.key;
                const control = owners.get(t.key) === face.name;
                const tint = `rgba(${face.rgb}, ${isHovered && center ? 0.4 : 0.15})`;
                return (
                  <div
                    key={`${c.row}-${c.col}`}
                    data-target={t.key}
                    data-hovered={isHovered ? "true" : undefined}
                    role={control ? "button" : undefined}
                    aria-label={control ? t.label : undefined}
                    aria-pressed={control ? view === t.key || (view === "iso" && t.key === "front-right-top") : undefined}
                    aria-hidden={control ? undefined : true}
                    tabIndex={control ? 0 : undefined}
                    title={t.label}
                    onPointerEnter={() => hover(t)}
                    onPointerLeave={() => hover(null)}
                    onClick={() => !suppressClick.current && onSnap(t.key)}
                    onKeyDown={control ? (e) => press(e, t.key) : undefined}
                    className="flex cursor-pointer items-center justify-center text-[10px] leading-none outline-none focus-visible:shadow-[inset_0_0_0_1.5px_#85b7eb]"
                    style={{
                      background: isHovered && !center ? EDGE_HOVER : tint,
                      color: isHovered ? "var(--cs-text)" : "#b4b2a9",
                    }}
                  >
                    {center ? face.name : null}
                  </div>
                );
              })}
            </div>
          ))}
        </div>
      </div>
      {BUTTONS.map((b) => (
        <button
          key={b.id}
          type="button"
          aria-label={b.label}
          title={b.label}
          onClick={() => run(b.id)}
          className="absolute flex size-8 items-center justify-center rounded-lg bg-transparent text-[#8a8984] outline-none hover:text-[var(--cs-text)] focus-visible:shadow-[0_0_0_1.5px_#85b7eb]"
          style={b.style}
        >
          <b.icon className="size-4" strokeWidth={1.8} />
        </button>
      ))}
    </div>
  );
}

/** The world axes as seen by the camera, nearest on top, for the axis triad. */
export function triadAxes(rotation: number[], length = 20) {
  const axes: { name: string; rgb: string; dir: Vec3 }[] = [
    { name: "X", rgb: AXIS_RGB.x, dir: [1, 0, 0] },
    { name: "Y", rgb: AXIS_RGB.y, dir: [0, 1, 0] },
    { name: "Z", rgb: AXIS_RGB.z, dir: [0, 0, 1] },
  ];
  return axes
    .map((a) => {
      const w = boardToWorld(a.dir);
      const vx = rotation[0] * w.x + rotation[4] * w.y + rotation[8] * w.z;
      const vy = rotation[1] * w.x + rotation[5] * w.y + rotation[9] * w.z;
      const vz = rotation[2] * w.x + rotation[6] * w.y + rotation[10] * w.z;
      return { ...a, x: vx * length, y: -vy * length, z: vz };
    })
    .sort((a, b) => a.z - b.z);
}

/** The axis triad: X, Y and Z as the camera sees them, turning and animating with the view. */
export function AxisTriad({ rotation }: { rotation: number[] }) {
  const axes = triadAxes(rotation);
  return (
    <svg
      data-testid="axis-triad"
      data-order={axes.map((a) => a.name).join("")}
      width={64}
      height={64}
      viewBox="-32 -32 64 64"
      role="img"
      aria-label="Axis triad"
    >
      {axes.map((a) => (
        <g key={a.name} data-axis={a.name} data-x={a.x.toFixed(1)} data-y={a.y.toFixed(1)}>
          <line x1={0} y1={0} x2={a.x} y2={a.y} stroke={`rgb(${a.rgb})`} strokeWidth={2} strokeLinecap="round" />
          <text
            x={a.x * 1.32}
            y={a.y * 1.32 + 4}
            textAnchor="middle"
            fontSize={11}
            fill={`rgb(${a.rgb})`}
          >
            {a.name}
          </text>
        </g>
      ))}
    </svg>
  );
}
