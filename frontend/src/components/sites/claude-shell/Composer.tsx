"use client";

import { type RefObject, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { ArrowUp, Check, FileText, Mic, MousePointer2, X } from "lucide-react";
import type { Selection } from "@/lib/session-api";
import type { Choices } from "./chat";
import { LONG_PASTE_CHARS, LONG_PASTE_LINES } from "./data";
import { pickFiles, unsupportedNote } from "./files";
import { AddMenu } from "./AddMenu";
import { ModelPicker } from "./ModelPicker";
import { type DictationParts, formatElapsed, partsText } from "./dictation";
import { useDictation } from "./useDictation";
import { QUEUE_NOTE } from "./messageQueue";

type Props = {
  placeholder: string;
  onSubmit: (text: string, files: string[], choices: Choices) => void;
  /** Model, effort and Manual/Auto, sent with the next message. */
  choices: Choices;
  onChoicesChange: (choices: Choices) => void;
  /** The canvas selection the next message carries as context, shown as a chip. */
  selection?: Selection | null;
  /** The chip text; by default it is made from the selection's IDs. */
  selectionLabel?: string;
  onClearSelection?: () => void;
  /** Claude is still replying: the draft can be written but not sent. */
  busy?: boolean;
  /** Why the draft can't be sent yet (Claude's question is open), or nothing. */
  blocked?: string;
  /** Claude is working: Send adds the message to the session's queue (./messageQueue). */
  queues?: boolean;
  /** Files staged for the next message. The parent owns them so other buttons can add to them. */
  files: string[];
  onFilesChange: (files: string[]) => void;
  /** "home" = large centered composer, "reply" = compact one in a chat column. */
  variant?: "home" | "reply";
  /** Files added elsewhere (the start buttons, the canvas) that were left out, said under the box. */
  fileNote?: string | null;
};

const iconBtnBase = "grid size-8 shrink-0 place-items-center rounded-lg hover:bg-[var(--cs-hover)]";
const iconBtn = `${iconBtnBase} text-[var(--cs-muted)] hover:text-[var(--cs-text)]`;
/** A toggle that is on (dictation listening): the accent colour stays, including on hover. */
const iconBtnActive = `${iconBtnBase} text-[var(--cs-accent)]`;

export function FileChip({ name, onRemove }: { name: string; onRemove?: () => void }) {
  return (
    <span className="inline-flex max-w-full items-center gap-1.5 rounded-md bg-[var(--cs-hover)] px-2 py-1 text-[13px]">
      <FileText className="size-3.5 shrink-0 text-[var(--cs-muted)]" />
      <span className="truncate">{name}</span>
      {onRemove && (
        <button type="button" aria-label={`Remove ${name}`} onClick={onRemove} className="text-[var(--cs-muted)] hover:text-[var(--cs-text)]">
          <X className="size-3" />
        </button>
      )}
    </span>
  );
}

/**
 * The recording bar shown in place of the idle controls while dictating: cancel, a live
 * microphone level meter, the elapsed time, and keep. Without a level (no microphone
 * stream) the bars pulse gently so it still reads as recording.
 */
export function RecordingBar({
  levels,
  metered,
  elapsed,
  onCancel,
  onKeep,
}: {
  levels: number[];
  metered: boolean;
  elapsed: number;
  onCancel: () => void;
  onKeep: () => void;
}) {
  return (
    <div data-testid="recording-bar" className="flex min-w-0 flex-1 items-center gap-2">
      <button type="button" aria-label="Cancel dictation" title="Cancel (Esc)" onClick={onCancel} className={iconBtn}>
        <X className="size-4" />
      </button>
      <div
        role="meter"
        aria-label="Microphone level"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round((levels[levels.length - 1] ?? 0) * 100)}
        className={`flex h-6 min-w-0 flex-1 items-center justify-end gap-[3px] overflow-hidden ${metered ? "" : "animate-pulse"}`}
      >
        {levels.map((l, i) => (
          <span
            key={i}
            className="w-[3px] shrink-0 rounded-full bg-[var(--cs-accent)]"
            style={{ height: `${Math.max(3, Math.round((metered ? l : 0.15) * 22))}px` }}
          />
        ))}
      </div>
      <span role="timer" aria-label="Recording time" className="w-10 shrink-0 text-right text-[13px] tabular-nums text-[var(--cs-muted)]">
        {formatElapsed(elapsed)}
      </span>
      <button type="button" aria-label="Keep dictation" title="Keep (Enter)" onClick={onKeep} className={iconBtnActive}>
        <Check className="size-4" />
      </button>
    </div>
  );
}

/** Every selected item, by the ID the agent gets: parts, board components, faces, then edges. */
export function selectionItems(selection: Selection): string[] {
  return [...(selection.parts ?? []), ...(selection.components ?? []), ...selection.faces, ...selection.edges];
}

/** "base.outer_wall_1", "3 faces on lid", "2 parts, 1 component", ... */
export function selectionLabel(selection: Selection) {
  const items = selectionItems(selection);
  if (items.length === 1) return items[0];
  const count = (n: number, noun: string) => (n ? `${n} ${noun}${n > 1 ? "s" : ""}` : "");
  const parts = [
    count(selection.parts?.length ?? 0, "part"),
    count(selection.components?.length ?? 0, "component"),
    count(selection.faces.length, "face"),
    count(selection.edges.length, "edge"),
  ].filter(Boolean);
  if (!parts.length) return selection.part ?? "Selection";
  return selection.part ? `${parts.join(", ")} on ${selection.part}` : parts.join(", ");
}

/**
 * The selection as a chip: its label, and for a multi-selection, a list of every selected item
 * (the IDs the agent gets).
 */
export function SelectionChip({
  selection,
  label = selectionLabel(selection),
  onRemove,
}: {
  selection: Selection;
  label?: string;
  onRemove?: () => void;
}) {
  const items = selectionItems(selection);
  const ids = [selection.part, ...items].filter(Boolean).join(", ");
  return (
    <span
      data-testid="selection-chip"
      title={ids}
      className="inline-flex max-w-full flex-col gap-1 rounded-md bg-[var(--cs-accent)]/15 px-2 py-1 text-[13px] text-[var(--cs-text)]"
    >
      <span className="inline-flex max-w-full items-center gap-1.5">
        <MousePointer2 className="size-3.5 shrink-0 text-[var(--cs-accent)]" />
        <span data-testid="selection-label" className="truncate">
          {label}
        </span>
        {onRemove && (
          <button type="button" aria-label="Clear selection" onClick={onRemove} className="text-[var(--cs-muted)] hover:text-[var(--cs-text)]">
            <X className="size-3" />
          </button>
        )}
      </span>
      {items.length > 1 && (
        <ul aria-label="Selected items" className="flex max-h-20 flex-wrap gap-1 overflow-y-auto">
          {items.map((id) => (
            <li key={id} className="rounded bg-[var(--cs-accent)]/15 px-1.5 font-mono text-[11px] text-[var(--cs-muted)]">
              {id}
            </li>
          ))}
        </ul>
      )}
    </span>
  );
}

export function isLongPaste(text: string) {
  return text.length >= LONG_PASTE_CHARS || text.split("\n").length >= LONG_PASTE_LINES;
}

/** The text sent for a draft: the typed text, then each pasted block. */
export function messageText(text: string, pasted: string[]) {
  return [text.trim(), ...pasted].filter(Boolean).join("\n\n");
}

export function PastedChip({ text, onRemove }: { text: string; onRemove?: () => void }) {
  const lines = text.replace(/\n+$/, "").split("\n").length;
  const preview = text.trim().split("\n")[0].slice(0, 80);
  return (
    <span
      data-testid="pasted-chip"
      className="inline-flex max-w-full items-center gap-2 rounded-lg border border-[var(--cs-border)] bg-[var(--cs-hover)] px-2.5 py-1.5 text-[13px]"
    >
      <FileText className="size-3.5 shrink-0 text-[var(--cs-muted)]" />
      <span className="flex min-w-0 flex-col">
        <span className="truncate">{preview || "Pasted content"}</span>
        <span className="text-[11px] text-[var(--cs-muted)]">
          Pasted content · {lines} {lines === 1 ? "line" : "lines"}
        </span>
      </span>
      {onRemove && (
        <button type="button" aria-label="Remove pasted content" onClick={onRemove} className="text-[var(--cs-muted)] hover:text-[var(--cs-text)]">
          <X className="size-3" />
        </button>
      )}
    </span>
  );
}

/**
 * Draws the draft over the (transparent) message box while dictating, so the words still
 * being recognized can be dimmed. Same font, padding and wrapping as the box.
 */
function DictationMirror({
  ref,
  parts,
  scrolls,
  home,
}: {
  ref: RefObject<HTMLDivElement | null>;
  parts: DictationParts;
  scrolls: boolean;
  home: boolean;
}) {
  return (
    <div
      ref={ref}
      aria-hidden
      data-testid="dictation-text"
      className={`pointer-events-none absolute inset-0 whitespace-pre-wrap px-1 leading-6 [overflow-wrap:anywhere] ${
        scrolls ? "cs-scroll overflow-y-scroll" : "overflow-hidden"
      } ${home ? "text-[15px]" : "text-[14px]"}`}
    >
      {parts.head}
      <span data-testid="dictation-final">{parts.final}</span>
      <span data-testid="dictation-interim" className="text-[var(--cs-muted)]">
        {parts.interim}
      </span>
      {parts.tail}
      {"\u200b"}
    </div>
  );
}

/** Grows the box with its text up to half the viewport, then scrolls inside. */
function useAutoGrow(ref: RefObject<HTMLTextAreaElement | null>, text: string) {
  const [scrolls, setScrolls] = useState(false);
  const resize = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    const max = Math.round(window.innerHeight / 2);
    el.style.height = `${Math.min(el.scrollHeight, max)}px`;
    setScrolls(el.scrollHeight > max);
  }, [ref]);
  useLayoutEffect(resize, [resize, text]);
  useEffect(() => {
    window.addEventListener("resize", resize);
    return () => window.removeEventListener("resize", resize);
  }, [resize]);
  return scrolls;
}

export function Composer({
  placeholder,
  onSubmit,
  files,
  onFilesChange,
  choices,
  onChoicesChange,
  selection,
  selectionLabel: chipLabel,
  onClearSelection,
  busy = false,
  blocked,
  queues = false,
  variant = "home",
  fileNote = null,
}: Props) {
  const [text, setText] = useState("");
  // Picked files that were left out (not a STEP file or an image), said under the box.
  const [rejected, setRejected] = useState<string | null>(null);
  const [pasted, setPasted] = useState<string[]>([]);
  const box = useRef<HTMLTextAreaElement>(null);
  const mirror = useRef<HTMLDivElement>(null);
  const caret = useRef<number | null>(null);
  const dictation = useDictation(box, (kept, at) => {
    setText(kept);
    caret.current = at;
  });
  const parts = dictation.parts;
  /** What the box shows: the draft, or while dictating the draft with the words heard so far. */
  const shown = parts ? partsText(parts) : text;
  const scrolls = useAutoGrow(box, shown);
  const hasContent = !!shown.trim() || files.length > 0 || pasted.length > 0;
  const canSend = hasContent && !busy && !blocked;
  const addFiles = (names: string[]) => onFilesChange([...files, ...names.filter((n) => !files.includes(n))]);

  // Put the cursor after the words just kept.
  useLayoutEffect(() => {
    if (caret.current === null || !box.current) return;
    box.current.setSelectionRange(caret.current, caret.current);
    caret.current = null;
  }, [text]);

  const submit = () => {
    if (!canSend) return;
    // Sending while listening keeps what was heard first.
    const draft = dictation.listening ? (dictation.finish() ?? text) : text;
    onSubmit(messageText(draft, pasted), files, choices);
    setText("");
    setPasted([]);
    onFilesChange([]);
    box.current?.focus();
  };

  const home = variant === "home";

  return (
    <div className="w-full">
      <div
        onKeyDown={(e) => {
          if (!dictation.listening) return;
          if (e.key === "Escape") {
            e.preventDefault();
            e.stopPropagation();
            dictation.cancel();
          } else if (e.key === "Enter" && e.target === box.current && !e.nativeEvent.isComposing) {
            e.preventDefault();
            dictation.stop();
          }
        }}
        // Focus on the message field brightens the box's border: a visible ring for the keyboard.
        className={`rounded-[14px] border border-[var(--cs-border)] bg-[var(--cs-surface-2)] has-[textarea:focus-visible]:border-[var(--cs-faint)] has-[textarea:focus-visible]:ring-1 has-[textarea:focus-visible]:ring-white/10 ${
          home ? "bg-[var(--cs-surface)] px-3 pb-2.5 pt-3 shadow-[0_2px_12px_rgba(0,0,0,0.2)]" : "px-2.5 pb-2 pt-2.5"
        }`}
      >
        {selection && (
          <div className="mb-2 flex px-1">
            <SelectionChip selection={selection} label={chipLabel} onRemove={onClearSelection} />
          </div>
        )}
        {pasted.length > 0 && (
          <ul aria-label="Pasted content" className="mb-2 flex flex-wrap gap-1.5 px-1">
            {pasted.map((p, i) => (
              <li key={i} className="max-w-full">
                <PastedChip text={p} onRemove={() => setPasted(pasted.filter((_, j) => j !== i))} />
              </li>
            ))}
          </ul>
        )}
        {files.length > 0 && (
          <ul aria-label="Attached files" className="mb-2 flex flex-wrap gap-1.5 px-1">
            {files.map((f) => (
              <li key={f} className="max-w-full">
                <FileChip name={f} onRemove={() => onFilesChange(files.filter((x) => x !== f))} />
              </li>
            ))}
          </ul>
        )}
        <div className="relative">
        <textarea
          ref={box}
          value={shown}
          readOnly={dictation.listening}
          onChange={(e) => {
            setText(e.target.value);
            dictation.clearError();
          }}
          onScroll={(e) => {
            if (mirror.current) mirror.current.scrollTop = e.currentTarget.scrollTop;
          }}
          onKeyDown={(e) => {
            if (dictation.listening) return;
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              submit();
            }
          }}
          onPaste={(e) => {
            const clip = e.clipboardData.getData("text/plain");
            if (!isLongPaste(clip)) return;
            e.preventDefault();
            setPasted((p) => [...p, clip]);
          }}
          rows={1}
          placeholder={dictation.listening ? "Listening…" : placeholder}
          aria-label="Message"
          data-scrolls={scrolls}
          className={`block w-full resize-none overflow-x-hidden whitespace-pre-wrap bg-transparent px-1 leading-6 outline-none [overflow-wrap:anywhere] placeholder:text-[var(--cs-faint)] ${
            scrolls ? "cs-scroll overflow-y-auto" : "overflow-y-hidden"
          } ${home ? "text-[15px]" : "text-[14px]"} ${parts ? "text-transparent" : ""}`}
        />
        {parts && <DictationMirror ref={mirror} parts={parts} scrolls={scrolls} home={home} />}
        </div>
        <div className="mt-1.5 flex items-center gap-1">
          {dictation.listening ? (
            <RecordingBar
              levels={dictation.levels}
              metered={dictation.metered}
              elapsed={dictation.elapsed}
              onCancel={dictation.cancel}
              onKeep={dictation.stop}
            />
          ) : (
            <>
              <AddMenu
                onAddFiles={() =>
                  pickFiles(
                    "any",
                    (names) => {
                      setRejected(null);
                      addFiles(names);
                    },
                    (names) => setRejected(unsupportedNote(names)),
                  )
                }
                buttonClassName={iconBtn}
                placement={home ? "below" : "above"}
              />
              <span className="ml-auto" />
            </>
          )}
          {dictation.supported && (
            <button
              type="button"
              aria-label={dictation.listening ? "Stop dictation" : "Dictate"}
              aria-pressed={dictation.listening}
              onClick={dictation.toggle}
              className={dictation.listening ? iconBtnActive : iconBtn}
            >
              <Mic className="size-4" />
            </button>
          )}
          <button
            type="button"
            aria-label="Send"
            onClick={submit}
            disabled={!canSend}
            title={canSend ? (queues ? QUEUE_NOTE : undefined) : busy ? "Claude is still replying" : (blocked ?? "Write a message to send")}
            className={`grid size-8 shrink-0 place-items-center rounded-lg ${
              canSend ? "bg-[var(--cs-accent)] text-white" : "cursor-not-allowed bg-[var(--cs-accent)]/40 text-white/60"
            }`}
          >
            <ArrowUp className="size-4" />
          </button>
        </div>
      </div>
      {(rejected ?? fileNote) && (
        <p role="alert" data-testid="file-note" className="mt-1.5 px-1 text-[13px] text-[var(--cs-muted)]">
          {rejected ?? fileNote}
        </p>
      )}
      {dictation.error && (
        <p role="alert" className="mt-1.5 px-1 text-[13px] text-[var(--cs-muted)]">
          {dictation.error}
        </p>
      )}
      <div className="mt-1.5 flex items-center px-1">
        <div className="ml-auto flex items-center gap-1">
          <ModelPicker
            model={choices.model}
            effort={choices.effort}
            onChange={(c) => onChoicesChange({ ...choices, ...c })}
            placement={home ? "below" : "above"}
          />
        </div>
      </div>
    </div>
  );
}
