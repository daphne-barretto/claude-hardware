import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { ModelPicker } from "@/components/sites/claude-shell/ModelPicker";
import { DEFAULT_CHOICES, MODELS, MORE_MODELS } from "@/components/sites/claude-shell/data";
import { readRepoFile } from "./fixtures";

afterEach(cleanup);

// Read at run time: the image build type-checks frontend/ alone, without contracts/.
const catalog = JSON.parse(readRepoFile("contracts/models.json"));

function setup(props: Partial<React.ComponentProps<typeof ModelPicker>> = {}) {
  const onChange = vi.fn();
  render(<ModelPicker model={DEFAULT_CHOICES.model} effort={DEFAULT_CHOICES.effort} onChange={onChange} {...props} />);
  fireEvent.click(screen.getByRole("button", { expanded: false }));
  return { onChange };
}

const more = () => screen.queryByRole("menuitem", { name: /More models/ });

describe("ModelPicker", () => {
  it("lists the models from the shared catalogue", () => {
    expect(MODELS).toEqual(catalog.models);
    expect(MORE_MODELS).toEqual(catalog.more_models);
    expect(DEFAULT_CHOICES.model).toBe(catalog.default);
    expect(MORE_MODELS.length).toBeGreaterThan(0);
  });

  it("opens More models with a click and selects an additional model", () => {
    const { onChange } = setup();
    fireEvent.click(more()!);
    expect(more()?.getAttribute("aria-expanded")).toBe("true");
    const submenu = screen.getByRole("menu", { name: "More models" });
    const items = within(submenu).getAllByRole("menuitemradio");
    expect(items.map((i) => i.textContent)).toEqual(MORE_MODELS.map((m) => `${m.name}${m.blurb}`));
    const pick = MORE_MODELS.at(-1)!;
    fireEvent.click(within(submenu).getByRole("menuitemradio", { name: new RegExp(pick.name) }));
    expect(onChange).toHaveBeenCalledWith({ model: pick.id, effort: DEFAULT_CHOICES.effort });
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("opens More models from the keyboard, focuses its first model and closes with Escape", () => {
    setup();
    const trigger = more()!;
    trigger.focus();
    fireEvent.keyDown(trigger, { key: "ArrowLeft" });
    const submenu = screen.getByRole("menu", { name: "More models" });
    expect(document.activeElement).toBe(within(submenu).getAllByRole("menuitemradio")[0]);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("menu", { name: "More models" })).toBeNull();
    expect(document.activeElement).toBe(trigger);
    // A second Escape closes the whole menu, as with Effort.
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("shows an additional model on the button and checks it in the submenu", () => {
    const pick = MORE_MODELS[0];
    setup({ model: pick.id });
    expect(screen.getByRole("button", { expanded: true })?.textContent).toContain(pick.name);
    expect(more()?.textContent).toContain(pick.name);
    fireEvent.click(more()!);
    expect(screen.getByRole("menuitemradio", { name: new RegExp(pick.name) })?.getAttribute("aria-checked")).toBe("true");
  });

  it("only one submenu is open at a time", () => {
    setup();
    fireEvent.click(screen.getByRole("menuitem", { name: /Effort/ }));
    fireEvent.click(more()!);
    expect(screen.queryByRole("menu", { name: "Effort" })).toBeNull();
    expect(screen.getByRole("menu", { name: "More models" })).toBeTruthy();
  });

  it("hides More models when no additional models are configured", () => {
    setup({ moreModels: [] });
    expect(more()).toBeNull();
    expect(screen.getByRole("menuitem", { name: /Effort/ })).toBeTruthy();
  });
});
