import { describe, expect, it } from "vitest";
import { fileNames, filesFor, rememberFiles } from "@/components/sites/claude-shell/files";

function file(path: string): File {
  const f = new File(["x"], path.split("/").pop()!);
  Object.defineProperty(f, "webkitRelativePath", { value: path.includes("/") ? path : "" });
  return f;
}

describe("picked files", () => {
  it("keeps picked files under their staged names for upload", () => {
    const picked = [file("a.step"), file("b.kicad_pcb")];
    rememberFiles(picked, "any");
    expect(filesFor(fileNames(picked, "any")).map((f) => f.name)).toEqual(["a.step", "b.kicad_pcb"]);
    expect(filesFor(["a.step"])[0].file).toBe(picked[0]);
  });

  it("uploads every file of a folder under its relative path", () => {
    const picked = [file("board/top.kicad_pcb"), file("board/lib/part.kicad_mod")];
    rememberFiles(picked, "folder");
    expect(fileNames(picked, "folder")).toEqual(["board"]);
    expect(filesFor(["board"]).map((f) => f.name)).toEqual(["board/top.kicad_pcb", "board/lib/part.kicad_mod"]);
  });

  it("a new pick under the same name replaces the old file, and unknown names have none", () => {
    rememberFiles([file("c.step")], "any");
    const again = file("c.step");
    rememberFiles([again], "any");
    expect(filesFor(["c.step"]).map((f) => f.file)).toEqual([again]);
    expect(filesFor(["sample-only.step"])).toEqual([]);
  });
});
