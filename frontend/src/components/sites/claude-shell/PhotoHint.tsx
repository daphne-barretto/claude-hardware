"use client";

// Photo to part: once a photo is staged for the next message, the composer says what
// Claude needs with it: what to make around the object, and one real measurement if the
// person has one. Without a measurement Claude assumes a scale and says which.

import { useEffect, useState } from "react";
import { Image as ImageIcon } from "lucide-react";
import { filesFor } from "./files";

/** The photo formats Claude can see (the backend's IMAGE_TYPES). */
export const PHOTO_TYPES = ["image/png", "image/jpeg", "image/gif", "image/webp"];
const PHOTO_EXTENSIONS: Record<string, string> = {
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  gif: "image/gif",
  webp: "image/webp",
  heic: "image/heic",
  heif: "image/heif",
  tif: "image/tiff",
  tiff: "image/tiff",
  bmp: "image/bmp",
};

export const PHOTO_PLACEHOLDER = "What should Claude make around it? A stand, a cover, a mount...";

/** The media type of a staged file: the picked file's own, else from its name's extension. */
export function photoType(name: string): string | null {
  const picked = filesFor([name])[0]?.file;
  if (picked?.type.startsWith("image/")) return picked.type;
  const ext = name.toLowerCase().split(".").pop() ?? "";
  return name.includes(".") ? (PHOTO_EXTENSIONS[ext] ?? null) : null;
}

/** The staged files that are photos, and those of them Claude can't see. */
export function stagedPhotos(files: string[]): { photos: string[]; unreadable: string[] } {
  const photos = files.filter((f) => photoType(f) !== null);
  return { photos, unreadable: photos.filter((f) => !PHOTO_TYPES.includes(photoType(f)!)) };
}

/** The composer's placeholder while a photo is staged, else `fallback`. */
export function photoPlaceholder(files: string[], fallback: string): string {
  return stagedPhotos(files).photos.length ? PHOTO_PLACEHOLDER : fallback;
}

function Thumbnail({ name }: { name: string }) {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    const file = filesFor([name])[0]?.file;
    if (!file || typeof URL.createObjectURL !== "function") return;
    const made = URL.createObjectURL(file);
    // An object URL is made and revoked with the effect, so Strict Mode never shows a revoked one.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setUrl(made);
    return () => URL.revokeObjectURL(made);
  }, [name]);
  if (!url) {
    return (
      <span className="grid size-12 shrink-0 place-items-center rounded-md bg-[var(--cs-hover)]">
        <ImageIcon className="size-4 text-[var(--cs-muted)]" aria-hidden="true" />
      </span>
    );
  }
  // eslint-disable-next-line @next/next/no-img-element -- a local object URL, not an optimizable asset
  return <img src={url} alt={name} onError={() => setUrl(null)} className="size-12 shrink-0 rounded-md object-cover" />;
}

/** What to send with a photo; nothing while no photo is staged. */
export function PhotoHint({ files }: { files: string[] }) {
  const { photos, unreadable } = stagedPhotos(files);
  if (!photos.length) return null;
  return (
    <section
      aria-label="Photo hint"
      className="mb-2 flex items-start gap-3 rounded-xl border border-[var(--cs-border)] px-3 py-2 text-left text-[13px] leading-5"
    >
      <div className="flex shrink-0 gap-1.5">
        {photos.slice(0, 3).map((p) => (
          <Thumbnail key={p} name={p} />
        ))}
      </div>
      <div className="min-w-0">
        <p>
          Say what to make around it (a stand, a cover, a mount), and give one real measurement if you have one, like
          &ldquo;it is 95 mm tall&rdquo;.
        </p>
        <p className="text-[var(--cs-muted)]">Without one, Claude assumes a scale, tells you which, and rescales when you measure.</p>
        {unreadable.length > 0 && (
          <p className="text-[var(--cs-muted)]">
            Claude can see PNG, JPEG, GIF and WebP photos, not {unreadable.join(", ")}. Export it as JPEG to send it.
          </p>
        )}
      </div>
    </section>
  );
}
