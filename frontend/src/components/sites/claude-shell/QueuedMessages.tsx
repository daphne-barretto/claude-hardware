"use client";

import { useEffect, useRef, useState } from "react";
import { ArrowDown, ArrowUp, Clock, Pause, Pencil, Send, Square, X } from "lucide-react";
import type { MessageQueue, QueuedMessage } from "@/lib/session-api";
import { FileChip, SelectionChip } from "./Composer";
import { PAUSED_REASON, SEND_ANYWAY_NOTE, queueHeading } from "./messageQueue";

const iconBtn =
  "grid size-7 shrink-0 place-items-center rounded-md text-[var(--cs-muted)] hover:bg-[var(--cs-hover)] hover:text-[var(--cs-text)]";

type Props = {
  queue: MessageQueue;
  onEdit: (id: string, text: string) => void;
  onMove: (id: string, by: -1 | 1) => void;
  onRemove: (id: string) => void;
  /** Send the next message now, leaving Claude's open question unanswered. */
  onSendAnyway: () => void;
  wide?: boolean;
};

/**
 * The messages waiting for Claude's reply to end ("Queued · N"), under the running reply. Each
 * can be edited, moved or removed before it starts. A question pauses the queue: it says so and
 * offers to send the next message anyway.
 */
export function QueuedMessages({ queue, onEdit, onMove, onRemove, onSendAnyway, wide }: Props) {
  if (!queue.items.length) return null;
  const last = queue.items.length - 1;
  return (
    <section
      aria-label="Queued messages"
      data-testid="message-queue"
      className={`mx-auto flex flex-col gap-1.5 pb-6 ${wide ? "max-w-[720px]" : ""}`}
    >
      <div className="flex items-center gap-2 text-[12px] text-[var(--cs-muted)]">
        {queue.paused ? <Pause aria-hidden className="size-3.5" /> : <Clock aria-hidden className="size-3.5" />}
        <h2 className="font-[500]">{queueHeading(queue.items.length)}</h2>
        {queue.paused && (
          <>
            <span role="status">{PAUSED_REASON}</span>
            <button
              type="button"
              title={SEND_ANYWAY_NOTE}
              onClick={onSendAnyway}
              className="ml-auto flex h-7 shrink-0 items-center gap-1.5 rounded-lg border border-[var(--cs-border)] px-2 text-[12px] text-[var(--cs-text)] hover:bg-[var(--cs-hover)]"
            >
              <Send aria-hidden className="size-3.5 text-[var(--cs-muted)]" />
              Send anyway
            </button>
          </>
        )}
      </div>
      <ol className="flex flex-col gap-1.5">
        {queue.items.map((item, i) => (
          <QueuedItem
            key={item.id}
            item={item}
            first={i === 0}
            last={i === last}
            onEdit={(text) => onEdit(item.id, text)}
            onMove={(by) => onMove(item.id, by)}
            onRemove={() => onRemove(item.id)}
          />
        ))}
      </ol>
    </section>
  );
}

function QueuedItem({
  item,
  first,
  last,
  onEdit,
  onMove,
  onRemove,
}: {
  item: QueuedMessage;
  first: boolean;
  last: boolean;
  onEdit: (text: string) => void;
  onMove: (by: -1 | 1) => void;
  onRemove: () => void;
}) {
  const [draft, setDraft] = useState<string | null>(null);
  const box = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    if (draft !== null) box.current?.focus();
  }, [draft !== null]); // eslint-disable-line react-hooks/exhaustive-deps
  const { message } = item;
  const files = message.attachments?.map((a) => a.name) ?? [];
  const save = () => {
    if (draft === null || !draft.trim()) return;
    if (draft !== message.text) onEdit(draft);
    setDraft(null);
  };
  return (
    <li
      data-testid="queued-message"
      className="ml-auto flex w-full max-w-[90%] flex-col gap-1.5 rounded-xl border border-dashed border-[var(--cs-border)] px-3 py-2 leading-6"
    >
      {(files.length > 0 || message.selection) && (
        <div className="flex flex-wrap gap-1.5">
          {message.selection && <SelectionChip selection={message.selection} />}
          {files.map((f) => (
            <FileChip key={f} name={f} />
          ))}
        </div>
      )}
      {draft === null ? (
        <div className="flex items-start gap-1">
          <span className="min-w-0 flex-1 whitespace-pre-wrap pt-0.5 text-[var(--cs-muted)]">{message.text}</span>
          <div className="flex shrink-0 items-center">
            {!first && (
              <button type="button" aria-label="Move queued message up" title="Send earlier" onClick={() => onMove(-1)} className={iconBtn}>
                <ArrowUp className="size-3.5" />
              </button>
            )}
            {!last && (
              <button type="button" aria-label="Move queued message down" title="Send later" onClick={() => onMove(1)} className={iconBtn}>
                <ArrowDown className="size-3.5" />
              </button>
            )}
            <button type="button" aria-label="Edit queued message" title="Edit before it is sent" onClick={() => setDraft(message.text)} className={iconBtn}>
              <Pencil className="size-3.5" />
            </button>
            <button type="button" aria-label="Remove queued message" title="Don't send it" onClick={onRemove} className={iconBtn}>
              <X className="size-3.5" />
            </button>
          </div>
        </div>
      ) : (
        <div className="flex flex-col gap-1.5">
          <textarea
            ref={box}
            aria-label="Edit queued message"
            value={draft}
            rows={Math.min(6, Math.max(1, draft.split("\n").length))}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                save();
              } else if (e.key === "Escape") {
                e.preventDefault();
                e.stopPropagation();
                setDraft(null);
              }
            }}
            className="w-full resize-none rounded-md bg-[var(--cs-surface-2)] px-2 py-1 text-[14px] outline-none focus:ring-1 focus:ring-[var(--cs-accent)]"
          />
          <div className="flex justify-end gap-1.5">
            <button
              type="button"
              onClick={() => setDraft(null)}
              className="h-7 rounded-lg px-2 text-[12px] text-[var(--cs-muted)] hover:bg-[var(--cs-hover)] hover:text-[var(--cs-text)]"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={save}
              disabled={!draft.trim()}
              title={draft.trim() ? undefined : "A queued message needs text"}
              className="h-7 rounded-lg bg-[var(--cs-accent)] px-2.5 text-[12px] text-white disabled:cursor-not-allowed disabled:opacity-40"
            >
              Save
            </button>
          </div>
        </div>
      )}
    </li>
  );
}

/** Stops Claude's run, under the running reply; the queue goes on. */
export function StopButton({ onStop, stopping }: { onStop: () => void; stopping?: boolean }) {
  return (
    <button
      type="button"
      onClick={onStop}
      disabled={stopping}
      title={stopping ? "Stopping…" : "Stop Claude's reply here; queued messages go on"}
      className="flex h-7 w-fit items-center gap-1.5 rounded-lg border border-[var(--cs-border)] px-2 text-[12px] text-[var(--cs-muted)] enabled:hover:bg-[var(--cs-hover)] enabled:hover:text-[var(--cs-text)] disabled:cursor-not-allowed disabled:opacity-50"
    >
      <Square aria-hidden className="size-3 fill-current" />
      Stop
    </button>
  );
}
