"use client";

import { useEffect, useRef, useState } from "react";
import { FolderPlus, Paperclip, Plus } from "lucide-react";
import { ADD_FOLDER } from "@/lib/flags";

type Props = {
  onAddFiles: () => void;
  onAddFolder: () => void;
  /** Class for the + button, matching the composer's other icon buttons. */
  buttonClassName: string;
  /** Opens above the + in a chat column, below it in the large home composer. */
  placement?: "above" | "below";
  /** Shows "Add folder"; off unless the feature flag is on (lib/flags). */
  folder?: boolean;
};

/** The composer's + menu: "Add files or photos", and "Add folder" behind a feature flag. */
export function AddMenu({ onAddFiles, onAddFolder, buttonClassName, placement = "above", folder = ADD_FOLDER }: Props) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    menu.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus();
    const onDown = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      setOpen(false);
      trigger.current?.focus();
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const choose = (action: () => void) => {
    setOpen(false);
    action();
  };
  const item = "flex w-full items-center gap-2.5 rounded-lg px-2.5 py-1.5 text-left text-[14px] hover:bg-[var(--cs-hover)] focus:bg-[var(--cs-hover)] focus:outline-none";
  const vertical = placement === "below" ? "top-[calc(100%+6px)]" : "bottom-[calc(100%+6px)]";

  return (
    <div ref={ref} className="relative">
      <button
        ref={trigger}
        type="button"
        aria-label="Add"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className={buttonClassName}
      >
        <Plus className="size-[18px]" strokeWidth={1.6} />
      </button>
      {open && (
        <div
          ref={menu}
          role="menu"
          aria-label="Add"
          onKeyDown={(e) => {
            if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
            e.preventDefault();
            const items = [...(menu.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? [])];
            const at = items.indexOf(document.activeElement as HTMLElement);
            items[(at + (e.key === "ArrowDown" ? 1 : items.length - 1)) % items.length]?.focus();
          }}
          className={`absolute left-0 z-40 w-[240px] rounded-[14px] border border-[var(--cs-border)] bg-[var(--cs-surface)] p-1.5 shadow-[0_8px_30px_rgba(0,0,0,0.35)] ${vertical}`}
        >
          <button type="button" role="menuitem" onClick={() => choose(onAddFiles)} className={item}>
            <Paperclip className="size-4 text-[var(--cs-muted)]" />
            Add files or photos
          </button>
          {folder && (
            <button type="button" role="menuitem" onClick={() => choose(onAddFolder)} className={item}>
              <FolderPlus className="size-4 text-[var(--cs-muted)]" />
              Add folder
            </button>
          )}
        </div>
      )}
    </div>
  );
}
