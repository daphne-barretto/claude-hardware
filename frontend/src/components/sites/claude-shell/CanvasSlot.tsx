"use client";

/**
 * The slot the 3D canvas fills. The shell owns the panel around it (title, Share, expand,
 * close); everything inside the slot (viewer, view controls, tool
 * toolbar, Constraints panel) belongs to the canvas. Pass the viewer as `children`; with no
 * children the slot shows the empty state from the New mockup, with `overlay` over it (the
 * Constraints panel before the first build).
 */
export function CanvasSlot({
  children,
  onImport,
  overlay,
}: {
  children?: React.ReactNode;
  onImport: () => void;
  overlay?: React.ReactNode;
}) {
  const empty = !children;
  return (
    <div
      data-testid="hardware-canvas"
      data-empty={empty ? "true" : "false"}
      // Clipped, not hidden: nothing (focus, scrollIntoView) can scroll the canvas sideways.
      className="relative min-h-0 flex-1 overflow-clip"
    >
      {empty ? (
        <div className="absolute inset-0 grid place-items-center px-6">
          <div className="flex flex-col items-center gap-3 text-center">
            <p className="text-[13px] text-[var(--cs-muted)]">
              This canvas has no parts yet. Import a STEP file, or let Claude build one.
            </p>
            <button
              type="button"
              onClick={onImport}
              className="h-8 rounded-lg bg-[var(--cs-surface)] px-3 text-[14px] hover:bg-[#3a3a3a]"
            >
              Import a file
            </button>
          </div>
          {overlay}
        </div>
      ) : (
        children
      )}
    </div>
  );
}
