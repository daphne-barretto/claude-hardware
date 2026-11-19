"use client";

import { ChevronRight, Eye, EyeOff, Focus } from "lucide-react";
import { useState } from "react";
import { BOARD_PART, boardPartOf, boardPick } from "./boardParts";
import { isIsolated, isolate, partView, type PartView, type PartViews, type Picked } from "./selection";

const rowBtn =
  "grid size-6 shrink-0 place-items-center rounded text-[var(--cs-muted)] hover:bg-[var(--cs-hover)] hover:text-[var(--cs-text)] aria-pressed:text-[var(--cs-text)]";

/** One part in the parts list: select it, and make it see-through, isolate it or hide it. */
export function PartRow({
  part,
  label,
  title,
  view,
  selected,
  alone,
  onSelect,
  onChange,
  onIsolate,
}: {
  part: string;
  /** What the select button shows: the part name by default. */
  label?: React.ReactNode;
  title?: string;
  view: PartView;
  selected: boolean;
  alone: boolean;
  onSelect: () => void;
  onChange: (change: Partial<PartView>) => void;
  onIsolate: () => void;
}) {
  return (
    <div className="flex items-center gap-0.5 rounded-md px-1 hover:bg-[var(--cs-hover)]/50">
      <button
        type="button"
        aria-label={`Select ${part}`}
        aria-pressed={selected}
        title={title}
        onClick={onSelect}
        className={`flex h-6 min-w-0 flex-1 items-baseline gap-1.5 text-left ${view.hidden ? "text-[var(--cs-faint)]" : ""} ${selected ? "text-[var(--cs-accent)]" : ""}`}
      >
        {label ?? <span className="truncate">{part}</span>}
      </button>
      <button
        type="button"
        aria-label={`Transparent: ${part}`}
        title={view.transparent ? "Make opaque" : "Make transparent"}
        aria-pressed={view.transparent}
        onClick={() => onChange({ transparent: !view.transparent })}
        className={rowBtn}
      >
        <span className={`size-3 rounded-sm border border-current ${view.transparent ? "opacity-40" : "bg-current"}`} />
      </button>
      <button
        type="button"
        aria-label={`Isolate: ${part}`}
        title={alone ? "Show all parts" : "Isolate"}
        aria-pressed={alone}
        onClick={onIsolate}
        className={rowBtn}
      >
        <Focus className="size-3.5" />
      </button>
      <button
        type="button"
        aria-label={`Hide: ${part}`}
        title={view.hidden ? "Show" : "Hide"}
        aria-pressed={view.hidden}
        onClick={() => onChange({ hidden: !view.hidden })}
        className={rowBtn}
      >
        {view.hidden ? <EyeOff className="size-3.5" /> : <Eye className="size-3.5" />}
      </button>
    </div>
  );
}

/** Where the board is drawn, for its rows' tooltips. */
const BOARD_SHOWN = "Drawn in every view but the print layout";

/**
 * The build's board revision in the parts list, after the enclosure's parts: the board, then its
 * components by reference designator in a list that opens under it (closed at first). Isolate
 * covers every listed part, the enclosure's included (`allParts`).
 */
export function BoardPartRows({
  names,
  allParts,
  views,
  onViews,
  picks,
  onSelect,
}: {
  /** The board's parts: the board, then its components. */
  names: string[];
  allParts: string[];
  views: PartViews;
  onViews: (v: PartViews) => void;
  picks: Picked[];
  onSelect: (picks: Picked[]) => void;
}) {
  const [open, setOpen] = useState(false);
  const board = names.includes(BOARD_PART) ? BOARD_PART : null;
  const components = names.filter((n) => n !== BOARD_PART);
  const picked = new Set(picks.flatMap((p) => boardPartOf(p) ?? []));
  const row = (name: string) => (
    <li key={name} data-part={name}>
      <PartRow
        part={name}
        title={BOARD_SHOWN}
        view={partView(views, name)}
        selected={picked.has(name)}
        alone={isIsolated(views, allParts, name)}
        onSelect={() => onSelect([boardPick(name)])}
        onChange={(change) => onViews({ ...views, [name]: { ...partView(views, name), ...change } })}
        onIsolate={() => onViews(isolate(views, allParts, name))}
      />
    </li>
  );
  return (
    <>
      {board && row(board)}
      {components.length > 0 && (
        <li className="flex flex-col">
          <button
            type="button"
            aria-expanded={open}
            aria-controls="parts-components"
            aria-label={`Components ${components.length}`}
            onClick={() => setOpen((o) => !o)}
            className="flex h-6 items-center gap-1 rounded-md pl-3 pr-1 text-left text-[var(--cs-muted)] hover:bg-[var(--cs-hover)]/50 hover:text-[var(--cs-text)]"
          >
            <ChevronRight className={`size-3 transition-transform ${open ? "rotate-90" : ""}`} />
            Components
            <span className="ml-auto text-[var(--cs-faint)]">{components.length}</span>
          </button>
          {open && (
            <ul id="parts-components" aria-label="Components" className="flex flex-col pl-3">
              {components.map(row)}
            </ul>
          )}
        </li>
      )}
    </>
  );
}
