import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { ModelPicker } from "@/components/sites/claude-shell/ModelPicker";
import { DEFAULT_CHOICES, MODELS } from "@/components/sites/claude-shell/data";

afterEach(cleanup);

// Regression: "More models" rendered a label and a chevron but opened nothing.
it("clicking More models opens a submenu whose models can be selected", () => {
  const onChange = vi.fn();
  render(<ModelPicker model={DEFAULT_CHOICES.model} effort={DEFAULT_CHOICES.effort} onChange={onChange} />);
  fireEvent.click(screen.getByRole("button", { expanded: false }));

  const more = screen.getByRole("menuitem", { name: /More models/ });
  fireEvent.click(more);

  expect(more.getAttribute("aria-expanded")).toBe("true");
  const submenu = screen.getByRole("menu", { name: "More models" });
  const items = within(submenu).getAllByRole("menuitemradio");
  expect(items.length).toBeGreaterThan(0);

  fireEvent.click(items[0]);
  expect(onChange).toHaveBeenCalledTimes(1);
  const picked = onChange.mock.calls[0][0].model;
  expect(MODELS.map((m) => m.id)).not.toContain(picked);
});
