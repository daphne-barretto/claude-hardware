import type { Locator } from "@playwright/test";

// The Constraints panel (ConstraintsPanel), opened from the canvas toolbar: one row per
// constraint, or per check with no constraint behind it, keyed by the result's standard or else
// its check. A row expands to list the places it is checked.

/** A check result as the fixtures and the session API give it. */
type Result = { id: string; check?: string; standard?: string | null };

/** The Constraints panel in the canvas. */
export function constraintsPanel(canvas: Locator): Locator {
  return canvas.getByTestId("constraints-panel");
}

/** Opens the Constraints panel from the canvas toolbar, unless it is open. */
export async function openConstraints(canvas: Locator): Promise<Locator> {
  const panel = constraintsPanel(canvas);
  if (!(await panel.isVisible())) await canvas.getByRole("toolbar", { name: "Canvas controls" }).getByRole("button", { name: "Constraints" }).click();
  return panel;
}

/** The panel's row for the constraint a result belongs to. */
export function constraintItem(canvas: Locator, r: Result): Locator {
  return canvas.locator(`[data-testid=constraint-item][data-constraint="${r.standard ?? r.check ?? r.id.split(":")[0]}"]`);
}

/** Expands a result's row in the open panel, unless it is expanded. */
export async function expandConstraint(canvas: Locator, r: Result): Promise<Locator> {
  const item = constraintItem(canvas, r);
  if ((await item.getAttribute("aria-expanded")) !== "true") await item.click();
  return canvas.getByTestId("constraint-detail");
}

/** The places in the expanded row. */
export function constraintPlaces(canvas: Locator): Locator {
  return canvas.getByTestId("constraint-detail").locator("button[data-check]");
}

/**
 * Picks a result as a click on its pin does: opens the panel, expands its constraint's row and
 * clicks its place. With `close`, the panel closes again (Escape twice: the row, then the panel),
 * so nothing covers the pins.
 */
export async function pickCheck(canvas: Locator, r: Result, { close = false } = {}): Promise<void> {
  await openConstraints(canvas);
  const detail = await expandConstraint(canvas, r);
  const place = detail.locator(`button[data-check="${r.id}"]`);
  await place.click();
  if (!close) return;
  await place.press("Escape");
  await constraintItem(canvas, r).press("Escape");
  await constraintsPanel(canvas).waitFor({ state: "hidden" });
}
