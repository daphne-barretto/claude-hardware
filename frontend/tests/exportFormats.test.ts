import { describe, expect, it } from "vitest";
import { EXPORT_FORMATS } from "@/components/canvas/api";
import {
  DEFAULT_FORMATS,
  downloadLabel,
  fileCount,
  FORMAT_OPTIONS,
  noteSummary,
  requestedFormats,
} from "@/components/canvas/exportFormats";
import { sessionOpenApi } from "./fixtures";

// The export menu offers every format of the session API contract.
describe("export formats", () => {
  it("match the contract's ExportFormat", () => {
    const formats = sessionOpenApi.components.schemas.ExportCreate.properties.formats.items;
    const schema = "$ref" in formats ? sessionOpenApi.components.schemas[formats.$ref.split("/").pop()] : formats;
    expect([...EXPORT_FORMATS]).toEqual(schema.enum);
  });

  it("offer STEP and the source code, STEP checked by default", () => {
    expect(FORMAT_OPTIONS.map((o) => o.format)).toEqual(["step", "source"]);
    expect(DEFAULT_FORMATS).toEqual(["step"]);
  });

  it("request the checked formats in contract order, the note last", () => {
    expect(requestedFormats(["pdf", "step"], true)).toEqual(["step", "pdf", "build_note"]);
    expect(requestedFormats(["pdf", "step"], false)).toEqual(["step", "pdf"]);
    expect(requestedFormats([], false)).toEqual([]);
    expect(requestedFormats(DEFAULT_FORMATS, true).at(-1)).toBe("build_note");
  });

  it("count the files a download gives: the source is the code and its parameters", () => {
    expect(fileCount(["source"])).toBe(2);
    expect(fileCount(["step", "build_note"])).toBe(2);
    expect(downloadLabel(1)).toBe("Download 1 file");
    expect(downloadLabel(3)).toBe("Download 3 files");
  });

  it("summarise the checks the build note reports", () => {
    expect(noteSummary({ pass: 7, fail: 0, untested: 3 })).toBe("7 checks passed · 3 not tested");
    expect(noteSummary({ pass: 1, fail: 2, untested: 3 })).toBe("1 check passed · 2 failed · 3 not tested");
    expect(noteSummary({ pass: 1, fail: 0, untested: 3, not_applicable: 2 })).toBe("1 check passed · 3 not tested · 2 not applicable");
  });
});
