"use client";

import {
  ArrowUpFromLine,
  Circle,
  CircleDot,
  Copy,
  FlipHorizontal2,
  Move3d,
  Redo2,
  Scissors,
  Slice,
  Spline,
  SquareDashed,
  Undo2,
  type LucideIcon,
} from "lucide-react";
import { TOOLS, type EditKind } from "./modeling";

const ICONS: Record<EditKind, LucideIcon> = {
  move: Move3d,
  push_pull: ArrowUpFromLine,
  hole: Circle,
  boss: CircleDot,
  rib: Slice,
  fillet: Spline,
  chamfer: Scissors,
  shell: SquareDashed,
  mirror: FlipHorizontal2,
  pattern: Copy,
};

const btn =
  "grid size-8 place-items-center rounded-md text-[var(--cs-muted)] hover:bg-[var(--cs-hover)] hover:text-[var(--cs-text)] aria-pressed:bg-[#1e3557] aria-pressed:text-[#a8c7fa] disabled:cursor-not-allowed disabled:opacity-40";

/**
 * The modeling toolbar, in the mockups' toolbar style: one button per tool (pressing one
 * opens its panel; pressing it again closes it), then undo and redo, which move the design back
 * and forth along the design timeline: every change, whoever made it.
 */
export function ModelingToolbar({
  tool,
  onTool,
  onUndo,
  onRedo,
  undoWhy,
  redoWhy,
  disabledWhy,
  undoTitle,
  redoTitle,
}: {
  tool: EditKind | null;
  /** Why the design can't be edited by hand (a read-only shared link): every tool is off. */
  disabledWhy?: string;
  onTool: (tool: EditKind | null) => void;
  onUndo: () => void;
  onRedo: () => void;
  /** Why undo can't be used now; none: it can. */
  undoWhy?: string;
  redoWhy?: string;
  /** What undo and redo would do: "Undo: You: push/pull 1 mm (⌘Z)". */
  undoTitle?: string;
  redoTitle?: string;
}) {
  return (
    <div
      role="toolbar"
      aria-label="Modeling tools"
      aria-orientation="vertical"
      className="pointer-events-auto flex flex-col items-center gap-0.5 rounded-xl border border-[var(--cs-border)] bg-[var(--cs-surface-2)] p-1 shadow-lg"
    >
      {TOOLS.map((t) => {
        const Icon = ICONS[t.kind];
        return (
          <button
            key={t.kind}
            type="button"
            aria-label={t.label}
            title={disabledWhy ?? t.hint}
            disabled={!!disabledWhy}
            aria-pressed={tool === t.kind}
            data-tool={t.kind}
            onClick={() => onTool(tool === t.kind ? null : t.kind)}
            className={btn}
          >
            <Icon className="size-4" />
          </button>
        );
      })}
      <span aria-hidden className="my-1 h-px w-5 bg-[var(--cs-border)]" />
      <button type="button" aria-label="Undo" title={undoWhy ?? undoTitle ?? "Undo (⌘Z)"} disabled={!!undoWhy} onClick={onUndo} className={btn}>
        <Undo2 className="size-4" />
      </button>
      <button type="button" aria-label="Redo" title={redoWhy ?? redoTitle ?? "Redo (⇧⌘Z)"} disabled={!!redoWhy} onClick={onRedo} className={btn}>
        <Redo2 className="size-4" />
      </button>
    </div>
  );
}
