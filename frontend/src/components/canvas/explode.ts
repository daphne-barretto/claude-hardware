/** Where a check result is on the model, kept free of three.js so it can be tested on its own. */

/**
 * The part a check location is on: its `part`, or else the part that owns its first face.
 * Faces are looked up in each part's face list (mesh contract), so this works for any names.
 */
export function locationPart(
  location: { part?: string | null; faces?: string[] | null },
  index: { part: string; faces: { id: string }[] }[],
): string | null {
  if (location.part) return location.part;
  for (const face of location.faces ?? []) {
    const owner = index.find((p) => p.faces.some((f) => f.id === face));
    if (owner) return owner.part;
  }
  return null;
}
