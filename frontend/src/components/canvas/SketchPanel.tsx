"use client";

import { useEffect, useState } from "react";
import { Lock } from "lucide-react";
import type { Build } from "./api";
import { RebuildResult, useRebuild } from "./ParametersPanel";
import type { Picked } from "./selection";
import {
  createSketch,
  defaultDraft,
  fieldLabel,
  getFace,
  isLength,
  OPERATION_LABELS,
  parseSketch,
  SHAPE_LABELS,
  SKETCH_OPERATIONS,
  SKETCH_SHAPES,
  sketchFace,
  sketchFields,
  type FacePlane,
  type Sketch,
  type SketchDraft,
  type SketchField,
  type SketchOperation,
  type SketchShape,
} from "./sketch";
import { fromMm, type LengthUnit } from "./units";
import { shownError } from "@/lib/errors";

const radio = (on: boolean) =>
  `h-6 flex-auto whitespace-nowrap rounded-md px-2 ${on ? "bg-[var(--cs-surface)] text-[var(--cs-text)]" : "text-[var(--cs-muted)] hover:text-[var(--cs-text)]"}`;

/**
 * Sketch on a face: with one flat face picked on the model, place a circle, slot,
 * rectangle or vent on it with typed size and position (from the face's centre, along its
 * axes), then cut or extrude it. The result is a named, locked feature in the code that Claude
 * keeps; the design rebuilds and the panel shows the rebuild's checks.
 */
export function SketchPanel({
  sessionId,
  build,
  builds,
  picks,
  unit,
  busy = false,
  onSketched,
}: {
  sessionId: string;
  build: Build | undefined;
  builds: Build[];
  picks: Picked[];
  unit: LengthUnit;
  /** Claude is working: the design can't change by hand until the run ends. */
  busy?: boolean;
  /** A sketch was saved: the canvas follows the rebuild. */
  onSketched?: () => void;
}) {
  const [shape, setShape] = useState<SketchShape>("circle");
  const [operation, setOperation] = useState<SketchOperation>("cut");
  const [draft, setDraft] = useState<SketchDraft | null>(null);
  const [errors, setErrors] = useState<Partial<Record<SketchField, string>>>({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [made, setMade] = useState<Sketch | null>(null);
  const [edit, setEdit] = useState<{ name: string; before: string[] } | null>(null);
  const rebuild = useRebuild(builds, edit);

  // The picked face and its plane, read from the build.
  const target = sketchFace(picks);
  const faceId = "face" in target ? target.face.id : null;
  const buildId = build?.id;
  const key = buildId && faceId ? `${buildId}/${faceId}` : null;
  const [loaded, setLoaded] = useState<{ key: string; plane?: FacePlane; error?: string } | null>(null);
  useEffect(() => {
    if (!buildId || !faceId) return;
    let live = true;
    const k = `${buildId}/${faceId}`;
    getFace(sessionId, buildId, faceId)
      .then((plane) => live && setLoaded({ key: k, plane }))
      .catch((e: Error) => live && setLoaded({ key: k, error: shownError(e) }));
    return () => {
      live = false;
    };
  }, [sessionId, buildId, faceId]);
  const face = key && loaded?.key === key ? loaded : null;
  const plane = face?.plane ?? null;
  // The form starts from the first face it is shown for.
  const values = draft ?? defaultDraft(plane, unit);

  const why =
    "reason" in target
      ? target.reason
      : !face
        ? "Reading the face"
        : face.error
          ? "The face could not be read"
          : !plane?.planar
            ? "Pick a flat face: this one is curved"
            : busy
              ? "Claude is working: sketch when it is done"
              : saving
                ? "Saving the sketch"
                : undefined;

  const submit = () => {
    if (why || !plane) return;
    const parsed = parseSketch(values, shape, operation, { build_id: plane.build_id, face_id: plane.face_id }, unit);
    if ("errors" in parsed) return setErrors(parsed.errors);
    setErrors({});
    setError(null);
    setSaving(true);
    const before = builds.map((b) => b.id);
    createSketch(sessionId, parsed.body)
      .then((sketch) => {
        setMade(sketch);
        setEdit({ name: "sketch", before });
        onSketched?.();
      })
      .catch((e: Error) => setError(shownError(e)))
      .finally(() => setSaving(false));
  };

  const size = plane?.size ? plane.size.map((v) => Number(fromMm(v, unit).toFixed(unit === "in" ? 3 : 1))).join(" × ") : null;
  const action = `${OPERATION_LABELS[operation]} ${SHAPE_LABELS[shape].toLowerCase()}`;

  return (
    <section
      aria-label="Sketch"
      className="cs-scroll flex max-h-full w-72 max-w-full flex-col overflow-y-auto rounded-xl border border-[var(--cs-border)] bg-[#262626]/95 p-3 text-[12px] shadow-xl"
    >
      <p className="text-[13px]">Sketch on a face</p>
      <p className="mt-0.5 text-[var(--cs-muted)]">
        Place a shape on a flat face, then cut or extrude it. It becomes a locked feature Claude keeps.
      </p>

      <p
        data-testid="sketch-face"
        data-face={faceId ?? ""}
        data-planar={plane ? String(plane.planar) : ""}
        className="mt-2 rounded-md bg-[var(--cs-surface-2)] px-2 py-1.5"
      >
        {"reason" in target ? (
          <span className="text-[var(--cs-muted)]">{target.reason}.</span>
        ) : !face ? (
          <span className="text-[var(--cs-muted)]">Reading face {faceId}…</span>
        ) : face.error ? (
          <span className="text-[var(--cs-accent)]">Face {faceId} could not be read: {face.error}</span>
        ) : plane?.planar ? (
          <>
            On <span className="text-[var(--cs-text)]">{faceId}</span>
            <span className="text-[var(--cs-muted)]">
              {" "}
              · {plane.part} · {size} {unit}
            </span>
          </>
        ) : (
          <span className="text-[var(--cs-accent)]">Face {faceId} is curved. Pick a flat face.</span>
        )}
      </p>

      <div role="radiogroup" aria-label="Shape" className="mt-2 flex flex-wrap rounded-lg bg-[var(--cs-surface-2)] p-0.5">
        {SKETCH_SHAPES.map((s) => (
          <button key={s} type="button" role="radio" aria-checked={shape === s} onClick={() => setShape(s)} className={radio(shape === s)}>
            {SHAPE_LABELS[s]}
          </button>
        ))}
      </div>
      <div role="radiogroup" aria-label="Operation" className="mt-1.5 flex flex-wrap rounded-lg bg-[var(--cs-surface-2)] p-0.5">
        {SKETCH_OPERATIONS.map((o) => (
          <button
            key={o}
            type="button"
            role="radio"
            aria-checked={operation === o}
            onClick={() => setOperation(o)}
            className={radio(operation === o)}
          >
            {OPERATION_LABELS[o]}
          </button>
        ))}
      </div>

      <form
        className="mt-2 flex flex-col gap-1.5"
        data-shape={shape}
        data-operation={operation}
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        {sketchFields(shape).map((field) => {
          const id = `sketch-${field}`;
          const label = fieldLabel(field, shape, operation);
          const fieldError = errors[field];
          return (
            <div key={field}>
              <div className="flex items-center gap-1.5">
                <label htmlFor={id} title={label} className="min-w-0 flex-1 truncate text-[var(--cs-muted)]">
                  {label}
                </label>
                <input
                  id={id}
                  aria-label={label}
                  aria-invalid={fieldError ? true : undefined}
                  aria-describedby={fieldError ? `${id}-error` : undefined}
                  inputMode="decimal"
                  value={values[field]}
                  onChange={(e) => setDraft({ ...values, [field]: e.target.value })}
                  className={`h-6 w-20 shrink-0 rounded-md bg-[var(--cs-surface-2)] px-1.5 text-right text-[var(--cs-text)] outline-none focus:ring-1 focus:ring-[var(--cs-accent)] ${fieldError ? "ring-1 ring-[var(--cs-accent)]" : ""}`}
                />
                <span className="w-6 shrink-0 text-[var(--cs-muted)]">{isLength(field) ? unit : ""}</span>
              </div>
              {fieldError && (
                <p id={`${id}-error`} className="mt-0.5 text-right text-[var(--cs-accent)]">
                  {fieldError}
                </p>
              )}
            </div>
          );
        })}
        <p className="text-[11px] text-[var(--cs-muted)]">Position is from the face&apos;s centre; y points up on a wall.</p>
        <button
          type="submit"
          disabled={!!why}
          title={why}
          aria-describedby={why ? "sketch-why" : undefined}
          className="mt-1 h-8 rounded-lg bg-[var(--cs-text)] px-3 text-[13px] font-medium text-[#1f1f1f] disabled:cursor-not-allowed disabled:opacity-50"
        >
          {saving ? "Saving…" : action}
        </button>
        {why && (
          <p id="sketch-why" className="text-[11px] text-[var(--cs-muted)]">
            {why}
          </p>
        )}
      </form>

      {error && (
        <p role="alert" className="mt-2 text-[var(--cs-accent)]">
          {error}
        </p>
      )}
      {made && (
        <p role="status" data-testid="sketch-made" data-tag={made.tag} className="mt-2 flex items-center gap-1.5 text-[var(--cs-muted)]">
          <Lock className="size-3.5 shrink-0 text-[var(--cs-text)]" aria-hidden="true" />
          <span>
            <span className="text-[var(--cs-text)]">{made.tag}</span> is locked: Claude keeps it.
          </span>
        </p>
      )}
      {rebuild && <RebuildResult rebuild={rebuild} unit={unit} />}
    </section>
  );
}
