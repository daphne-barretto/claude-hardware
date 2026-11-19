// File picking. The composer stages files by name; the picked files are kept here by that
// name until the message is sent, when the chat uploads them to the session API.

export type FileKind = "any" | "board" | "cad" | "image" | "folder";

/** What can be added for now: a STEP file to import, or an image for reference. The session
 * API refuses anything else the same way (`backend/app/attachments.py`). */
const STEP = /\.(step|stp)$/i;
export function isSupported(file: { name: string; type: string }): boolean {
  return STEP.test(file.name) || file.type.startsWith("image/");
}

/** Says which picked files were left out, and what can be added instead. */
export function unsupportedNote(names: string[]): string {
  const list = names.length < 3 ? names.join(" and ") : `${names.slice(0, -1).join(", ")}, and ${names.at(-1)}`;
  return `${list} ${names.length === 1 ? "isn't" : "aren't"} supported yet. Add a STEP file (.step, .stp) or an image.`;
}

/** `accept` filters for each picker. "any" covers every kind the canvas can start from. */
export const FILE_ACCEPT: Record<Exclude<FileKind, "folder">, string> = {
  board: ".kicad_pcb,.kicad_pro,.brd,.zip",
  cad: ".step,.stp",
  image: "image/*",
  any: ".step,.stp,image/*",
};

/** Names to show for picked files; a folder is shown once, by its top-level name. */
export function fileNames(files: ArrayLike<File>, kind: FileKind): string[] {
  const list = Array.from(files);
  if (kind !== "folder") return list.map((f) => f.name);
  const roots = list.map((f) => (f.webkitRelativePath || f.name).split("/")[0]);
  return [...new Set(roots)];
}

/** A file to upload, with the name it is sent under (its relative path inside a folder). */
export type PickedFile = { name: string; file: File };

const picked = new Map<string, PickedFile[]>();

/** Remembers picked files under their staged names, so the chat can upload them on send. A
 * new pick under a staged name replaces the old one, as the composer keeps one chip per name. */
export function rememberFiles(files: ArrayLike<File>, kind: FileKind) {
  const byName = new Map<string, PickedFile[]>();
  for (const file of Array.from(files)) {
    const path = kind === "folder" ? file.webkitRelativePath || file.name : file.name;
    const staged = path.split("/")[0];
    byName.set(staged, [...(byName.get(staged) ?? []), { name: path, file }]);
  }
  for (const [name, list] of byName) picked.set(name, list);
}

/** The files to upload for staged names. Names with no picked files (sample data) have none. */
export function filesFor(names: string[]): PickedFile[] {
  return names.flatMap((n) => picked.get(n) ?? []);
}

/** Opens the browser's file (or folder) chooser and reports the picked names. Files that aren't
 * supported (`isSupported`) are left out and reported to `onReject`. */
export function pickFiles(kind: FileKind, onPick: (names: string[]) => void, onReject?: (names: string[]) => void) {
  const input = document.createElement("input");
  input.type = "file";
  input.multiple = true;
  input.hidden = true;
  if (kind === "folder") input.webkitdirectory = true;
  else if (FILE_ACCEPT[kind]) input.accept = FILE_ACCEPT[kind];
  input.addEventListener("change", () => {
    const all = input.files ? Array.from(input.files) : [];
    // A folder (behind a flag) and a board file keep their own kinds of files.
    const checked = kind !== "folder" && kind !== "board";
    const files = checked ? all.filter(isSupported) : all;
    const rejected = checked ? all.filter((f) => !isSupported(f)).map((f) => f.name) : [];
    if (rejected.length) onReject?.(rejected);
    const names = fileNames(files, kind);
    rememberFiles(files, kind);
    input.remove();
    if (names.length) onPick(names);
  });
  input.addEventListener("cancel", () => input.remove());
  document.body.appendChild(input);
  input.click();
}
