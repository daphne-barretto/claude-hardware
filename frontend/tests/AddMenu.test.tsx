import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { AddMenu } from "@/components/sites/claude-shell/AddMenu";

afterEach(cleanup);

function setup(folder?: boolean) {
  const onAddFiles = vi.fn();
  const onAddFolder = vi.fn();
  render(<AddMenu onAddFiles={onAddFiles} onAddFolder={onAddFolder} buttonClassName="" folder={folder} />);
  return { onAddFiles, onAddFolder };
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

  it("hides Add folder while the feature flag is off (the default)", () => {
    setup();
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(screen.queryByRole("menuitem", { name: "Add folder" })).toBeNull();
  });

  it("shows Add folder when the flag is on", () => {
    const { onAddFolder } = setup(true);
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Add folder" }));
    expect(onAddFolder).toHaveBeenCalledOnce();
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
