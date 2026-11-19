"use client";

import { useState } from "react";
import { Lock } from "lucide-react";
import {
  dimensionText,
  draggedValue,
  formatParameter,
  parseParameter,
  placeLabels,
  type Parameter,
  type Point3,
  type Rect,
} from "./parameters";
import { PLACE } from "./chromeLayout";
import { overallExtents, type Bounds } from "./extents";
import type { LengthUnit } from "./units";
import { parameterLabel as label, type Parameters } from "./useParameterEdits";

/** A face drag in progress: the parameter and how far its faces have moved, mm. */
export type Dragging = { name: string; distance: number };

const TICK_PX = 5;
type Px = [number, number];
/** Half a tick, square to the line from `a` to `b`. */
const tick = (a: Px, b: Px): Px => {
  const [dx, dy] = [b[0] - a[0], b[1] - a[1]];
  const len = Math.hypot(dx, dy) || 1;
  return [(-dy / len) * TICK_PX, (dx / len) * TICK_PX];
};
/** About the size of a value label, to keep labels apart. */
const LABEL_PX: [number, number] = [76, 24];

/** A dimension end, moved along the drag axis when its face is being dragged. */
function endPoint(p: Parameter, end: "start" | "end", dragging: Dragging | null): Point3 {
  const e = p.dimension![end];
  const moved = dragging?.name === p.name && p.drag && e.face && p.drag.faces.includes(e.face);
  if (!moved) return e.point;
  const [ax, ay, az] = p.drag!.axis;
  const d = dragging!.distance;
  return [e.point[0] + ax * d, e.point[1] + ay * d, e.point[2] + az * d];
}

/**
 * The parameters' dimensions drawn on the model: a line between the two points each is measured
 * between (a radius from its centre), and its value. Clicking a value opens a field to type a new
 * one, which is a hand edit like typing in the parameters panel: it locks the parameter and
 * rebuilds (`onEdited` then shows the rebuild's checks). While a face is dragged, its dimension
 * follows the drag and shows the value it will set.
 */
export function Dimensions({
  parameters,
  unit,
  project,
  layout,
  hidden,
  dragging,
  onEdited,
  bounds,
}: {
  parameters: Parameters;
  unit: LengthUnit;
  /** Board point → canvas CSS pixels (null when behind the camera). */
  project: (p: Point3) => [number, number] | null;
  /** The canvas size and the panels over it, the top and bottom bars included (CSS pixels): labels stay inside and keep out of the panels. */
  layout: () => { size: [number, number]; panels: Rect[] } | null;
  /** Hidden parts: their dimensions aren't drawn. */
  hidden: (part: string) => boolean;
  dragging: Dragging | null;
  onEdited: () => void;
  /** The enclosure's bounding box (board mm): its overall width, depth and height are drawn too, read-only, unless a parameter's dimension measures one. */
  bounds?: Bounds | null;
}) {
  const { params, saving, errors, setError } = parameters;
  const [editing, setEditing] = useState<{ name: string; text: string } | null>(null);

  const shown = (params ?? []).flatMap((p) => {
    const dim = p.dimension;
    if (!dim) return [];
    if ([dim.start.part, dim.end.part].some((part) => part && hidden(part))) return [];
    const a = project(endPoint(p, "start", dragging));
    const b = project(endPoint(p, "end", dragging));
    return a && b ? [{ p, a, b }] : [];
  });

  // The overall dimensions, projected: the line and the extension lines out to it from the box.
  const extents = (bounds ? overallExtents(bounds, shown.map(({ p }) => p), unit, project) : []).flatMap((e) => {
    const [a, b] = [project(e.from), project(e.to)];
    const ext = e.extension.map(([s, t]) => [project(s), project(t)]);
    if (!a || !b || ext.some(([s, t]) => !s || !t)) return [];
    return [{ e, a, b, ext: ext as [number, number][][] }];
  });

  const canvas = layout();
  // Parameters' labels are placed first, so they sit where they did without the overall ones.
  const allSpots = placeLabels(
    [...shown.map(({ a, b }): [Px, Px] => [a, b]), ...extents.map(({ a, b }): [Px, Px] => [a, b])],
    LABEL_PX,
    canvas?.panels,
    canvas?.size,
  );
  const spots = allSpots.slice(0, shown.length);
  const extentSpots = allSpots.slice(shown.length);

  const commit = (param: Parameter) => {
    if (!editing || editing.name !== param.name || saving) return;
    const parsed = parseParameter(editing.text, param, unit);
    if ("error" in parsed) return setError(param.name, parsed.error);
    setError(param.name, null);
    if (editing.text.trim() === formatParameter(param, unit)) return setEditing(null);
    parameters.edit(param, parsed.value).then((ok) => {
      if (!ok) return;
      setEditing(null);
      onEdited();
    });
  };

  return (
    // Only in the band between the top and bottom bars (chromeLayout.ts): never over the
    // canvas's toolbars. Inside it, positions are in canvas pixels as `project` gives them.
    <div role="group" aria-label="Dimensions" data-chrome-band="" className={PLACE.dimensions}>
      <div className={PLACE.dimensionsCanvas}>
      <svg aria-hidden="true" className="absolute inset-0 size-full">
        {/* A label moved off its line (out from under a panel) points back to it. */}
        {spots.map(({ at, anchor }, i) =>
          at[0] === anchor[0] && at[1] === anchor[1] ? null : (
            <line
              key={`leader-${shown[i].p.name}`}
              x1={at[0]}
              y1={at[1]}
              x2={anchor[0]}
              y2={anchor[1]}
              stroke="#E9E9E7"
              strokeWidth={0.75}
              strokeDasharray="3 3"
            />
          ),
        )}
        {shown.map(({ p, a, b }) => {
          const [dx, dy] = [b[0] - a[0], b[1] - a[1]];
          const len = Math.hypot(dx, dy) || 1;
          // Ticks square to the line at each end (a dot at a radius's centre).
          const [nx, ny] = [(-dy / len) * TICK_PX, (dx / len) * TICK_PX];
          const radius = p.dimension!.kind === "radius";
          return (
            <g
              key={p.name}
              data-dimension={p.name}
              data-start={a.map((v) => v.toFixed(1)).join(",")}
              data-end={b.map((v) => v.toFixed(1)).join(",")}
              stroke={dragging?.name === p.name ? "#5B9BFF" : "#E9E9E7"}
              strokeWidth={1.25}
            >
              <line x1={a[0]} y1={a[1]} x2={b[0]} y2={b[1]} />
              {radius ? (
                <circle cx={a[0]} cy={a[1]} r={2} fill="#E9E9E7" />
              ) : (
                <line x1={a[0] - nx} y1={a[1] - ny} x2={a[0] + nx} y2={a[1] + ny} />
              )}
              <line x1={b[0] - nx} y1={b[1] - ny} x2={b[0] + nx} y2={b[1] + ny} />
            </g>
          );
        })}
        {extents.map(({ e, a, b, ext }, i) => {
          const [nx, ny] = tick(a, b);
          const { at, anchor } = extentSpots[i];
          return (
            <g
              key={`extent-${e.axis}`}
              data-extent={e.name.toLowerCase()}
              data-start={a.map((v) => v.toFixed(1)).join(",")}
              data-end={b.map((v) => v.toFixed(1)).join(",")}
              stroke="#E9E9E7"
              strokeWidth={1.25}
            >
              {ext.map(([s, t], k) => (
                <line key={k} x1={s[0]} y1={s[1]} x2={t[0]} y2={t[1]} strokeWidth={0.75} strokeOpacity={0.7} />
              ))}
              <line x1={a[0]} y1={a[1]} x2={b[0]} y2={b[1]} />
              <line x1={a[0] - nx} y1={a[1] - ny} x2={a[0] + nx} y2={a[1] + ny} />
              <line x1={b[0] - nx} y1={b[1] - ny} x2={b[0] + nx} y2={b[1] + ny} />
              {(at[0] !== anchor[0] || at[1] !== anchor[1]) && (
                <line x1={at[0]} y1={at[1]} x2={anchor[0]} y2={anchor[1]} strokeWidth={0.75} strokeDasharray="3 3" />
              )}
            </g>
          );
        })}
      </svg>
      {shown.map(({ p }, i) => {
        const [x, y] = spots[i].at;
        const id = `dim-${p.name}`;
        const error = errors[p.name];
        const drag = dragging?.name === p.name ? draggedValue(p, dragging.distance, unit) : null;
        const value = drag && "value" in drag ? drag.value : p.value;
        return (
          <div
            key={p.name}
            data-parameter={p.name}
            data-locked={p.locked ? "true" : "false"}
            data-dragging={dragging?.name === p.name ? "true" : undefined}
            className="pointer-events-auto absolute flex -translate-x-1/2 -translate-y-1/2 flex-col items-center"
            style={{ left: x, top: y }}
          >
            {editing?.name === p.name ? (
              <span className="flex items-center gap-1 rounded-md border border-[#5B9BFF] bg-[#24395C] px-1.5 py-0.5 text-[12px] text-[var(--cs-text)] shadow-lg">
                <label htmlFor={id} className="whitespace-nowrap">
                  {label(p)}
                </label>
                <input
                  id={id}
                  aria-label={label(p)}
                  aria-invalid={error ? true : undefined}
                  aria-describedby={error ? `${id}-error` : undefined}
                  autoFocus
                  inputMode="decimal"
                  value={editing.text}
                  readOnly={saving === p.name}
                  onChange={(e) => setEditing({ name: p.name, text: e.target.value })}
                  onBlur={() => commit(p)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") commit(p);
                    if (e.key === "Escape") {
                      e.stopPropagation();
                      setEditing(null);
                      setError(p.name, null);
                    }
                  }}
                  className="h-5 w-14 rounded bg-[var(--cs-surface-2)] px-1 text-right outline-none focus:ring-1 focus:ring-[var(--cs-accent)]"
                />
                <span className="text-[var(--cs-muted)]">{unit}</span>
              </span>
            ) : (
              <button
                type="button"
                aria-label={`Edit ${label(p)}`}
                title={`${label(p)}. Click to type a value${p.drag ? ", or drag its face" : ""}.`}
                onClick={() => {
                  setError(p.name, null);
                  setEditing({ name: p.name, text: formatParameter(p, unit) });
                }}
                className={`flex items-center gap-1 whitespace-nowrap rounded-md px-1.5 py-0.5 text-[12px] tabular-nums shadow ${
                  dragging?.name === p.name
                    ? "bg-[#24395C] text-[#5B9BFF]"
                    : "bg-[#262626]/90 text-[var(--cs-text)] hover:bg-[#333]"
                }`}
              >
                {dimensionText(p, unit, value)}
                {p.locked && <Lock className="size-3" aria-hidden="true" />}
              </button>
            )}
            {error && (
              <p
                id={`${id}-error`}
                role="alert"
                className="mt-0.5 whitespace-nowrap rounded bg-[#262626] px-1 text-[11px] text-[var(--cs-accent)]"
              >
                {error}
              </p>
            )}
          </div>
        );
      })}
      {extents.map(({ e }, i) => {
        const [x, y] = extentSpots[i].at;
        return (
          // Read-only: the overall size isn't a value to edit (no parameter measures it).
          <div
            key={`extent-${e.axis}`}
            role="note"
            aria-label={`Overall ${e.name.toLowerCase()} ${e.label}`}
            data-extent={e.name.toLowerCase()}
            data-length={e.length.toFixed(3)}
            className="pointer-events-none absolute -translate-x-1/2 -translate-y-1/2 whitespace-nowrap rounded-md bg-[#262626]/90 px-1.5 py-0.5 text-[12px] tabular-nums text-[var(--cs-text)] shadow"
            style={{ left: x, top: y }}
          >
            {e.label}
          </div>
        );
      })}
      </div>
    </div>
  );
}
