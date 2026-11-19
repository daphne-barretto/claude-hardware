import { EXPORT_FORMATS, type CheckSummary, type ExportFormat } from "./api";

/** A file format offered in the export menu (the build note is its own "Include build note" option). */
export type FileFormat = Exclude<ExportFormat, "build_note">;

export type FormatOption = { format: FileFormat; label: string; purpose: string; files: number };

/** The export menu's formats, in menu order. `files` is how many files the format downloads. */
export const FORMAT_OPTIONS: FormatOption[] = [
  { format: "step", label: "STEP", purpose: "Any CAD tool", files: 1 },
  { format: "stl", label: "STL", purpose: "3D printing", files: 1 },
  { format: "3mf", label: "3MF", purpose: "3D printing with units", files: 1 },
  { format: "obj", label: "OBJ", purpose: "Rendering", files: 1 },
  { format: "glb", label: "GLB", purpose: "Web and AR", files: 1 },
  { format: "dxf", label: "DXF", purpose: "Flat profiles", files: 1 },
  { format: "pdf", label: "PDF drawing", purpose: "Key dimensions", files: 1 },
  { format: "source", label: "Source code", purpose: "The code and its parameters", files: 2 },
];

/** Checked when the menu opens: what the demo exports for the molder and a test print. */
export const DEFAULT_FORMATS: FileFormat[] = ["step", "stl"];

/** The formats to request, in contract order, with the build note last if included. */
export function requestedFormats(selected: readonly FileFormat[], includeNote: boolean): ExportFormat[] {
  return EXPORT_FORMATS.filter((f) => (f === "build_note" ? includeNote : selected.includes(f)));
}

/** How many files a download of these formats gives. */
export function fileCount(formats: readonly ExportFormat[]): number {
  return formats.reduce((n, f) => n + (FORMAT_OPTIONS.find((o) => o.format === f)?.files ?? 1), 0);
}

export function downloadLabel(count: number): string {
  return count === 1 ? "Download 1 file" : `Download ${count} files`;
}

/** The line under "Include build note": what the note will report for this build's checks. */
export function noteSummary({ pass, fail, untested, not_applicable }: CheckSummary): string {
  const parts = [`${pass} check${pass === 1 ? "" : "s"} passed`];
  if (fail) parts.push(`${fail} failed`);
  parts.push(`${untested} not tested`);
  if (not_applicable) parts.push(`${not_applicable} not applicable`);
  return parts.join(" · ");
}
