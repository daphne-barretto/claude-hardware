import { describe, expect, it } from "vitest";
import { FILE_ACCEPT, isSupported, unsupportedNote } from "@/components/sites/claude-shell/files";

describe("files you can add", () => {
  it("are STEP files, by extension, and images, by type", () => {
    expect(isSupported({ name: "case.step", type: "" })).toBe(true);
    expect(isSupported({ name: "CASE.STP", type: "application/octet-stream" })).toBe(true);
    expect(isSupported({ name: "mug.jpg", type: "image/jpeg" })).toBe(true);
    for (const [name, type] of [
      ["notes.txt", "text/plain"],
      ["board.kicad_pcb", ""],
      ["part.stl", "model/stl"],
      ["case.step.zip", "application/zip"],
    ]) {
      expect(isSupported({ name, type })).toBe(false);
    }
  });

  it("are all the pickers offer", () => {
    expect(FILE_ACCEPT.any).toBe(".step,.stp,image/*");
    expect(FILE_ACCEPT.cad).toBe(".step,.stp");
    expect(FILE_ACCEPT.image).toBe("image/*");
  });

  it("names what was left out and what to add instead", () => {
    expect(unsupportedNote(["a.txt"])).toBe("a.txt isn't supported yet. Add a STEP file (.step, .stp) or an image.");
    expect(unsupportedNote(["a.txt", "b.stl"])).toBe("a.txt and b.stl aren't supported yet. Add a STEP file (.step, .stp) or an image.");
    expect(unsupportedNote(["a", "b", "c"])).toMatch(/^a, b, and c aren't/);
  });
});
