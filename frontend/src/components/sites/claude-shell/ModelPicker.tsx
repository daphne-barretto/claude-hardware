"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { Check, ChevronDown, ChevronRight } from "lucide-react";
import type { Effort } from "@/lib/session-api";
import { usePhone } from "./AppShell";
import { EFFORTS, type ModelOption } from "./data";
import { useAvailableModels } from "./useAvailableModels";

type Props = {
  model: string;
  effort: Effort;
  onChange: (choice: { model: string; effort: Effort }) => void;
  /** Which way the menu opens relative to the trigger. */
  placement?: "below" | "above";
  compact?: boolean;
  /**
   * The main and "More models" lists; default to the catalogue entries the server's API key can
   * use (`GET /api/models`), else the whole catalogue (contracts/models.json).
   */
  models?: readonly ModelOption[];
  moreModels?: readonly ModelOption[];
};

type Submenu = "effort" | "more";

const SUBMENU_HEIGHT = 240;

export function ModelPicker({
  model,
  effort,
  onChange,
  placement = "below",
  compact = false,
  models: givenModels,
  moreModels: givenMore,
}: Props) {
  const available = useAvailableModels();
  const models = givenModels ?? available.models;
  const moreModels = givenMore ?? available.more_models;
  // The chosen model can't be used any more (the server found it unavailable): its default instead.
  const usable = givenModels !== undefined || [...models, ...moreModels].some((m) => m.id === model);
  useEffect(() => {
    if (!usable) onChange({ model: available.default, effort });
  }, [usable, available.default, effort, onChange]);
  const [open, setOpen] = useState(false);
  const [submenu, setSubmenu] = useState<Submenu | null>(null);
  const [submenuUp, setSubmenuUp] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const triggers = useRef<Partial<Record<Submenu, HTMLButtonElement | null>>>({});
  const submenuRef = useRef<HTMLDivElement>(null);
  // On a phone the submenus open inside the menu, which scrolls within the room on its side of the trigger.
  const phone = usePhone();
  const [room, setRoom] = useState<number | null>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) {
        setOpen(false);
        setSubmenu(null);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (submenu) {
        triggers.current[submenu]?.focus();
        setSubmenu(null);
      } else setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, submenu]);

  // Move focus into a submenu when it opens, onto the checked item if it has one.
  useEffect(() => {
    if (!submenu) return;
    const menu = submenuRef.current;
    const item = menu?.querySelector<HTMLElement>('[aria-checked="true"]') ?? menu?.querySelector<HTMLElement>("button");
    item?.focus();
  }, [submenu]);

  const all = [...models, ...moreModels];
  const modelName = all.find((m) => m.id === model)?.name ?? model;
  const effortName = EFFORTS.find((e) => e.id === effort)?.name;
  const moreName = moreModels.find((m) => m.id === model)?.name;
  const vertical = placement === "below" ? "top-[calc(100%+6px)]" : "bottom-[calc(100%+6px)]";

  const close = () => {
    setSubmenu(null);
    setOpen(false);
  };
  const choose = (choice: { model: string; effort: Effort }) => {
    onChange(choice);
    close();
  };
  const toggleSubmenu = (name: Submenu, trigger: HTMLElement) => {
    const r = trigger.getBoundingClientRect();
    setSubmenuUp(r.top + SUBMENU_HEIGHT > window.innerHeight);
    setSubmenu((s) => (s === name ? null : name));
  };

  const submenuItem = (name: Submenu, label: string, value: string | undefined, items: ReactNode) => (
    <div className="relative">
      <button
        ref={(el) => {
          triggers.current[name] = el;
        }}
        type="button"
        role="menuitem"
        aria-haspopup="menu"
        aria-expanded={submenu === name}
        onClick={(e) => toggleSubmenu(name, e.currentTarget)}
        onKeyDown={(e) => {
          if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
            e.preventDefault();
            if (submenu !== name) toggleSubmenu(name, e.currentTarget);
          }
        }}
        className={`flex h-9 w-full items-center rounded-lg px-3 text-left hover:bg-[var(--cs-hover)] ${
          submenu === name ? "bg-[var(--cs-hover)]" : ""
        }`}
      >
        <span className="flex-1">{label}</span>
        {value && <span className="mr-1 text-[13px] text-[var(--cs-muted)]">{value}</span>}
        <ChevronRight className="size-4 text-[var(--cs-muted)]" />
      </button>
      {submenu === name && (
        <div
          ref={submenuRef}
          role="menu"
          // Beside the menu on a desktop; inside it on a phone, where there is no room beside it.
          aria-label={label}
          className={`absolute right-[calc(100%+10px)] z-50 max-md:static max-md:mt-1 max-md:w-auto max-md:shadow-none ${submenuUp ? "bottom-0" : "top-0"} ${
            name === "more" ? "w-[280px]" : "w-[220px]"
          } rounded-[14px] border border-[var(--cs-border)] bg-[var(--cs-surface)] p-1.5 shadow-[0_8px_30px_rgba(0,0,0,0.35)]`}
        >
          {items}
        </div>
      )}
    </div>
  );

  const modelItem = (m: ModelOption) => (
    <button
      key={m.id}
      type="button"
      role="menuitemradio"
      aria-checked={model === m.id}
      onClick={() => choose({ model: m.id, effort })}
      className="flex w-full items-center gap-2 rounded-lg px-3 py-1.5 text-left hover:bg-[var(--cs-hover)]"
    >
      <span className="flex-1">
        <span className="block text-[14px]">{m.name}</span>
        {m.blurb && <span className="block text-[13px] text-[var(--cs-muted)]">{m.blurb}</span>}
      </span>
      {model === m.id && <Check className="size-4 text-[var(--cs-link)]" />}
    </button>
  );

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={(e) => {
          const r = e.currentTarget.getBoundingClientRect();
          setRoom(Math.floor(placement === "below" ? window.innerHeight - r.bottom - 18 : r.top - 18));
          setOpen((v) => !v);
          setSubmenu(null);
        }}
        className={`flex h-8 items-center gap-1.5 rounded-lg px-2.5 text-[14px] hover:bg-[var(--cs-hover)] ${
          open ? "bg-[var(--cs-active)]" : ""
        } ${compact ? "text-[13px]" : ""}`}
      >
        <span>{modelName}</span>
        <span className="text-[var(--cs-muted)]">{effortName}</span>
        {compact && <ChevronDown className="size-3 text-[var(--cs-muted)]" />}
      </button>

      {open && (
        <div
          role="menu"
          style={phone && room ? { maxHeight: room, overflowY: "auto" } : undefined}
          className={`absolute right-0 z-40 w-[304px] rounded-[14px] border border-[var(--cs-border)] bg-[var(--cs-surface)] p-1.5 shadow-[0_8px_30px_rgba(0,0,0,0.35)] ${vertical}`}
        >
          {models.map(modelItem)}
          <div className="my-1.5 h-px bg-[var(--cs-border)]" />
          {submenuItem(
            "effort",
            "Effort",
            effortName,
            EFFORTS.map((e) => (
              <button
                key={e.id}
                type="button"
                role="menuitemradio"
                aria-checked={effort === e.id}
                onClick={() => choose({ model, effort: e.id })}
                className="flex w-full items-center gap-2 rounded-lg px-3 py-1.5 text-left hover:bg-[var(--cs-hover)]"
              >
                <span className="flex-1">
                  <span className="block text-[14px]">{e.name}</span>
                  {"note" in e && <span className="block text-[12px] text-[var(--cs-muted)]">{e.note}</span>}
                </span>
                {effort === e.id && <Check className="size-4 text-[var(--cs-link)]" />}
              </button>
            )),
          )}
          {moreModels.length > 0 && submenuItem("more", "More models", moreName, moreModels.map(modelItem))}
        </div>
      )}
    </div>
  );
}
