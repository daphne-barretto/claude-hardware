import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { AddMenu } from "@/components/sites/claude-shell/AddMenu";

afterEach(cleanup);

function setup() {
  const onAddFiles = vi.fn();
  render(<AddMenu onAddFiles={onAddFiles} buttonClassName="" />);
  return { onAddFiles };
}

describe("AddMenu", () => {
  it("opens from + with Add files or photos, which picks files and closes", () => {
    const { onAddFiles } = setup();
    const plus = screen.getByRole("button", { name: "Add" });
    expect(plus.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(plus);
    expect(plus.getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(screen.getByRole("menuitem", { name: "Add files or photos" }));
    expect(onAddFiles).toHaveBeenCalledOnce();
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("closes on Escape and on a click outside", () => {
    setup();
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("menu")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole("menu")).toBeNull();
  });
});
