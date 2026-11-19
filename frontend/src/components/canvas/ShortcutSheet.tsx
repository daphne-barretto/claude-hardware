"use client";

import { useEffect, useRef } from "react";
import { X } from "lucide-react";
import { bindingKeys, SHORTCUT_GROUPS, SHORTCUTS, type ShortcutAction } from "./shortcuts";

const isMac = () => typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);

/** The `?` sheet: every canvas shortcut that has an action, by group. */
export function ShortcutSheet({
  available,
  onClose,
}: {
  available: (action: ShortcutAction) => boolean;
  onClose: () => void;
}) {
  const closeRef = useRef<HTMLButtonElement>(null);
  useEffect(() => closeRef.current?.focus(), []);
  const mac = isMac();
  const groups = SHORTCUT_GROUPS.map((group) => ({
    group,
    items: SHORTCUTS.filter((s) => s.group === group && available(s.action)),
  })).filter((g) => g.items.length > 0);

  return (
    <div
      role="dialog"
      aria-label="Keyboard shortcuts"
      data-shortcut-sheet
      className="absolute left-1/2 top-14 z-30 w-72 -translate-x-1/2 rounded-xl border border-[var(--cs-border)] bg-[#262626] p-3 text-[13px] shadow-xl"
    >
      <div className="mb-2 flex items-center justify-between">
        <h2 className="font-medium text-[var(--cs-text)]">Keyboard shortcuts</h2>
        <button
          ref={closeRef}
          type="button"
          aria-label="Close keyboard shortcuts"
          onClick={onClose}
          className="grid size-6 place-items-center rounded-md text-[var(--cs-muted)] hover:bg-[var(--cs-hover)] hover:text-[var(--cs-text)]"
        >
          <X className="size-3.5" />
        </button>
      </div>
      {groups.map(({ group, items }) => (
        <section key={group} aria-label={group} className="mt-2 first:mt-0">
          <h3 className="mb-1 text-[11px] uppercase tracking-wide text-[var(--cs-muted)]">{group}</h3>
          <dl className="flex flex-col gap-1">
            {items.map((s) => (
              <div key={s.action} data-shortcut={s.action} className="flex items-center justify-between gap-3">
                <dt className="text-[var(--cs-text)]">{s.label}</dt>
                <dd className="flex gap-1.5">
                  {s.bindings.map((b, i) => (
                    <span key={i} className="flex gap-0.5">
                      {bindingKeys(b, mac).map((k) => (
                        <kbd
                          key={k}
                          className="min-w-5 rounded border border-[var(--cs-border)] bg-[var(--cs-surface-2)] px-1 text-center font-mono text-[11px] text-[var(--cs-muted)]"
                        >
                          {k}
                        </kbd>
                      ))}
                    </span>
                  ))}
                </dd>
              </div>
            ))}
          </dl>
        </section>
      ))}
    </div>
  );
}
