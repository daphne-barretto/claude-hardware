import { describe, expect, it } from "vitest";
import { fileNames, filesFor, rememberFiles } from "@/components/sites/claude-shell/files";

function file(name: string): File {
  return new File(["x"], name);
}

describe("picked files", () => {
  it("keeps picked files under their staged names for upload", () => {
    const picked = [file("a.step"), file("b.kicad_pcb")];
    rememberFiles(picked);
    expect(filesFor(fileNames(picked)).map((f) => f.name)).toEqual(["a.step", "b.kicad_pcb"]);
    expect(filesFor(["a.step"])[0].file).toBe(picked[0]);
  });

  it("a new pick under the same name replaces the old file, and unknown names have none", () => {
    rememberFiles([file("c.step")]);
    const again = file("c.step");
    rememberFiles([again]);
    expect(filesFor(["c.step"]).map((f) => f.file)).toEqual([again]);
    expect(filesFor(["sample-only.step"])).toEqual([]);
  });
});
