/**
 * Analysis map colors (contracts/README.md, "Analysis maps"). One scale serves both the mesh
 * and the legend, so the legend always matches the colors on the model.
 */

export type MapType = "wall_thickness" | "draft";
export type MapUnit = "mm" | "deg";

export type Legend = { min: number; max: number; limit?: number | null; unit: MapUnit };

export type MapLocation = {
  point: [number, number, number];
  part?: string | null;
  faces?: string[];
  edges?: string[];
};

export type FlaggedSpot = {
  id: string;
  kind: "thin" | "thick" | "low_draft";
  value: number;
  message: string;
  location: MapLocation;
};

export type AnalysisMap = {
  contract: "analysis-map/1";
  build_id: string;
  type: MapType;
  standard: string;
  /** The standard's title, as people read it (maps from before it was sent have none). */
  standard_title?: string | null;
  unit: MapUnit;
  legend: Legend;
  parts: { part: string; values: (number | null)[] }[];
  flagged?: FlaggedSpot[];
};

export const MAP_LABELS: Record<MapType, string> = {
  wall_thickness: "Wall thickness",
  draft: "Draft",
};

export const isMapType = (t: string): t is MapType => t in MAP_LABELS;

export type RGB = [number, number, number];

/**
 * The scale from the legend's min (0) to its max (1): red where a value is lowest (thin walls,
 * no draft), through amber and green, to blue where it is highest (thick sections).
 */
export const SCALE: { at: number; color: RGB }[] = [
  { at: 0, color: [0.86, 0.2, 0.18] },
  { at: 0.25, color: [0.95, 0.6, 0.15] },
  { at: 0.5, color: [0.38, 0.75, 0.35] },
  { at: 0.75, color: [0.2, 0.68, 0.72] },
  { at: 1, color: [0.24, 0.42, 0.88] },
];

/** The color of faces the analysis does not apply to (null values). */
export const NOT_APPLICABLE: RGB = [0.45, 0.45, 0.45];

/** Where a value sits on the legend, 0 to 1. Values outside [min, max] clamp to the ends. */
export function legendPosition(value: number, { min, max }: Pick<Legend, "min" | "max">): number {
  if (!(max > min)) return 0;
  return Math.min(1, Math.max(0, (value - min) / (max - min)));
}

/** The color of a value on a map's legend (sRGB, 0 to 1 per channel). */
export function mapColor(value: number | null, legend: Pick<Legend, "min" | "max">): RGB {
  if (value === null || !Number.isFinite(value)) return NOT_APPLICABLE;
  const t = legendPosition(value, legend);
  const i = Math.max(1, SCALE.findIndex((s) => s.at >= t));
  const a = SCALE[i - 1];
  const b = SCALE[i];
  const f = (t - a.at) / (b.at - a.at);
  return [0, 1, 2].map((c) => a.color[c] + (b.color[c] - a.color[c]) * f) as RGB;
}

export const cssColor = ([r, g, b]: RGB) =>
  `rgb(${Math.round(r * 255)}, ${Math.round(g * 255)}, ${Math.round(b * 255)})`;

/** The legend bar as a CSS gradient, from min (left) to max (right), with the same stops as the mesh. */
export function legendGradient(): string {
  return `linear-gradient(to right, ${SCALE.map((s) => `${cssColor(s.color)} ${s.at * 100}%`).join(", ")})`;
}

/**
 * Per-vertex colors for one part: three floats per vertex, in vertex order, as `COLOR_0`
 * would hold them. Returns null when the values do not line up with the part's vertices.
 */
export function vertexColors(values: (number | null)[], vertexCount: number, legend: Legend): Float32Array | null {
  if (values.length !== vertexCount) return null;
  const out = new Float32Array(vertexCount * 3);
  values.forEach((v, i) => out.set(mapColor(v, legend), i * 3));
  return out;
}

/** The fix request for a flagged spot: the text sent to Claude, with the spot's location as the selection. */
export function fixRequest(map: Pick<AnalysisMap, "type" | "standard" | "standard_title">, spot: FlaggedSpot) {
  const { point, part = null, faces = [], edges = [] } = spot.location;
  return {
    text: `Fix this flagged ${MAP_LABELS[map.type].toLowerCase()} spot: ${spot.message}. ${map.standard_title ? `Keep to ${map.standard_title}.` : "Keep to the constraint."}`,
    selection: { part, faces, edges, point },
  };
}
