import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { ParametersPanel } from "@/components/canvas/ParametersPanel";
import type { Parameters } from "@/components/canvas/useParameterEdits";
import { SHARED_BLOCKED } from "@/lib/share";

afterEach(cleanup);

// A shared link can't be edited (the API answers 409): the parameters panel shows the
// values with the same read-only note as the modeling tools, and edits nothing.
function parameters(): Parameters {
  return {
    params: [
      { name: "wall", value: 2, unit: "mm", locked: false },
      { name: "lid", value: 1.5, unit: "mm", locked: true },
    ],
    loadError: null,
    saving: null,
    errors: {},
    setError: vi.fn(),
    notice: null,
    rebuild: null,
    edit: vi.fn(async () => true),
    unlock: vi.fn(async () => {}),
  };
}

describe("the parameters panel on a read-only session", () => {
  it("says why and edits nothing", () => {
    const p = parameters();
    render(<ParametersPanel parameters={p} unit="mm" readOnly={SHARED_BLOCKED} />);
    const panel = screen.getByRole("region", { name: "Parameters" });
    expect(within(panel).getByRole("note").textContent).toBe(`${SHARED_BLOCKED}. Continue from it to edit.`);
    expect(within(panel).queryByText(/Type a value to lock it/)).toBeNull();
    const inputs = within(panel).getAllByRole("textbox") as HTMLInputElement[];
    expect(inputs.map((i) => i.readOnly)).toEqual([true, true]);
    fireEvent.change(inputs[0], { target: { value: "3" } });
    fireEvent.keyDown(inputs[0], { key: "Enter" });
    fireEvent.blur(inputs[0]);
    expect(p.edit).not.toHaveBeenCalled();
    // A locked value shows as locked, with nothing to unlock it.
    expect(within(panel).queryByRole("button", { name: /Unlock/ })).toBeNull();
    expect(within(panel).getByLabelText(/ is locked$/)).toBeTruthy();
  });

  it("can be edited on the user's own session", () => {
    const p = parameters();
    render(<ParametersPanel parameters={p} unit="mm" />);
    const panel = screen.getByRole("region", { name: "Parameters" });
    expect(within(panel).queryByRole("note")).toBeNull();
    const input = within(panel).getAllByRole("textbox")[0] as HTMLInputElement;
    expect(input.readOnly).toBe(false);
    fireEvent.change(input, { target: { value: "3" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(p.edit).toHaveBeenCalledTimes(1);
    expect(within(panel).getByRole("button", { name: /Unlock/ })).toBeTruthy();
  });
});
