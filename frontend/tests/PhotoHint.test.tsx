import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { PHOTO_PLACEHOLDER, PhotoHint, photoPlaceholder, stagedPhotos } from "@/components/sites/claude-shell/PhotoHint";
import { rememberFiles } from "@/components/sites/claude-shell/files";

afterEach(cleanup);

describe("photo to part", () => {
  it("finds the staged photos by their picked type or their name", () => {
    rememberFiles([new File(["x"], "IMG_0042", { type: "image/jpeg" })], "image");
    expect(stagedPhotos(["board.kicad_pcb", "mug.JPG", "IMG_0042", "scan.heic"])).toEqual({
      photos: ["mug.JPG", "IMG_0042", "scan.heic"],
      unreadable: ["scan.heic"],
    });
    expect(stagedPhotos(["case.step", "notes"])).toEqual({ photos: [], unreadable: [] });
  });

  it("asks what to make around a staged photo, and keeps the placeholder otherwise", () => {
    expect(photoPlaceholder(["mug.png"], "Reply...")).toBe(PHOTO_PLACEHOLDER);
    expect(photoPlaceholder(["case.step"], "Reply...")).toBe("Reply...");
  });

  it("says to give one real measurement, and that Claude assumes a scale without one", () => {
    render(<PhotoHint files={["mug.png", "case.step"]} />);
    const hint = screen.getByRole("region", { name: "Photo hint" });
    expect(hint.textContent).toMatch(/one real measurement/);
    expect(hint.textContent).toMatch(/Claude assumes a scale/);
    expect(hint.textContent).not.toMatch(/Claude can see PNG/);
  });

  it("says which photos Claude can't see", () => {
    render(<PhotoHint files={["scan.heic"]} />);
    expect(screen.getByRole("region", { name: "Photo hint" }).textContent).toMatch(/not scan\.heic/);
  });

  it("shows nothing without a photo", () => {
    const { container } = render(<PhotoHint files={["case.step"]} />);
    expect(container.innerHTML).toBe("");
  });
});
