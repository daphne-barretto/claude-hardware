/** Display units for lengths. Geometry is always millimetres (contracts/README.md); only the display changes. */
export type LengthUnit = "mm" | "in";

export const LENGTH_UNITS: LengthUnit[] = ["mm", "in"];

export const MM_PER_INCH = 25.4;

/** Converts a length in millimetres to the display unit. */
export function fromMm(mm: number, unit: LengthUnit): number {
  return unit === "in" ? mm / MM_PER_INCH : mm;
}

/** A length in millimetres as display text: one decimal in mm, three in inches. */
export function formatLength(mm: number, unit: LengthUnit): string {
  const digits = unit === "in" ? 3 : 1;
  return `${fromMm(mm, unit).toFixed(digits)} ${unit}`;
}

/** Overall size as "W × D × H unit". */
export function formatSize([w, d, h]: [number, number, number], unit: LengthUnit): string {
  const digits = unit === "in" ? 3 : 1;
  return `${[w, d, h].map((v) => fromMm(v, unit).toFixed(digits)).join(" × ")} ${unit}`;
}

/** A unit a check measures in (check-result contract): lengths, areas, volumes and angles. */
export type CheckUnit = "mm" | "mm2" | "mm3" | "deg";

/** A number with at most `digits` decimals, without trailing zeros. */
function trim(value: number, digits: number): string {
  return String(Number(value.toFixed(digits)));
}

// A length written in text: an optional sign, the number, then mm (not mm² or mmHg).
const MM_TEXT = /(^|[^\w.])([±+-]?)(\d+(?:\.\d+)?)\s?mm\b(?![²³23])/g;

/** Text (a check's summary, say) with every length written in mm converted to the display unit. */
export function lengthsInText(text: string, unit: LengthUnit): string {
  if (unit === "mm") return text;
  return text.replace(MM_TEXT, (_, before: string, sign: string, mm: string) => `${before}${sign}${trim(Number(mm) / MM_PER_INCH, 4)} in`);
}
