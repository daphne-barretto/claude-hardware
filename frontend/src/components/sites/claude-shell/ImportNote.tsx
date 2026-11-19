import { FileUp } from "lucide-react";
import type { BuildOrigin } from "@/lib/session-api";

/** What the chat says where a build came from a file rather than a message: an import, or the existing design an example opens on. */
export function importNoteText(origin: BuildOrigin): string {
  return origin.kind === "import" ? `Imported ${origin.file_name}` : `Opened the existing design from ${origin.file_name}`;
}

/** A short line above a build that came from a file, so the chat says where it came from. */
export function ImportNote({ origin }: { origin: BuildOrigin }) {
  return (
    <p data-testid="import-note" className="flex items-center gap-1.5 text-[13px] text-[var(--cs-muted)]">
      <FileUp aria-hidden className="size-3.5 shrink-0" />
      <span>{importNoteText(origin)}</span>
    </p>
  );
}
