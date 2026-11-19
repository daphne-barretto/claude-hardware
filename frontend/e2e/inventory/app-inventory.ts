import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { parse } from "yaml";
import { CHOOSE_STANDARDS, DEFAULT_CHOICES, EFFORTS, EMPTY_HEADING, MODELS, MORE_MODELS } from "../../src/components/sites/claude-shell/data";
import { isMapType, MAP_LABELS } from "../../src/components/canvas/colormap";
import { BOARD_PART } from "../../src/components/canvas/boardParts";
import { featureTree } from "../../src/components/canvas/featureTree";
import { changesHeading, featureChangeText } from "../../src/components/canvas/history";
import { indexFromGltf } from "../../src/components/canvas/selection";
import {
  fieldLabel,
  OPERATION_LABELS,
  SHAPE_LABELS,
  SKETCH_OPERATIONS,
  SKETCH_SHAPES,
  sketchFields,
} from "../../src/components/canvas/sketch";
import { sketchTargets } from "../sketchFaces";
import { editFields, fieldLabel as editFieldLabel, TOOLS } from "../../src/components/canvas/modeling";
import { loadInventory } from "./ui-inventory";
import { checkLabel, locatedResults, untestedResults } from "../../src/components/canvas/checks";
import { placeLabel, requiredLabel } from "../../src/components/canvas/constraintGroups";
import type { CheckReport } from "../../src/components/canvas/api";
import { riskSubject } from "../../src/components/sites/claude-shell/chat";
import type { RiskFlag } from "../../src/lib/session-api";

// frontend/e2e/ui-inventory.yaml loaded with the app's sample data and the fixtures, for the
// inventory checks (ui-inventory.spec.ts) and the phone checks (phone.spec.ts).
const root = path.resolve(__dirname, "../../..");
const question = readFileSync(path.join(root, "fixtures/session-events.jsonl"), "utf8")
  .split("\n")
  .filter(Boolean)
  .map((line) => JSON.parse(line).event)
  .find((e) => e.type === "question");

// The can't-satisfy state: the question of the recorded conflict run.
const conflict = readFileSync(path.join(root, "fixtures/conflict-events.jsonl"), "utf8")
  .split("\n")
  .filter(Boolean)
  .map((line) => JSON.parse(line).event)
  .find((e) => e.type === "question");

// The session canvas: the fixture build, its source, checks and analysis maps.
const fixture = (name: string) => JSON.parse(readFileSync(path.join(root, "fixtures", name), "utf8"));
// The fixture build's parts and faces (fixtures/enclosure.gltf), for the canvas selection.
type Feature = { id: string; feature: string };
type MeshExtras = { part?: string; faces?: Feature[]; edges?: Feature[] };
const meshParts = (fixture("enclosure.gltf") as { nodes: { extras?: MeshExtras }[] }).nodes.flatMap((n) =>
  n.extras?.part ? [n.extras] : [],
);

// The feature tree of the fixture build: features with faces, and features with lines.
const treeFeatures = featureTree(
  indexFromGltf(fixture("enclosure.gltf")),
  fixture("build-source.json").features,
  fixture("parameters.json"),
).flatMap((p) => p.features);

const report = fixture("checks.json") as CheckReport;
/** The label of a result's item in the Constraints bar, as the spec labels its constraint (`app.spec.label`). */
function itemLabel(r: CheckReport["results"][number]): string {
  const s = r.standard ? readStandards().find((x) => x.id === r.standard) : undefined;
  if (!s) return requiredLabel(r.title, r.required, "mm");
  return requiredLabel(s.title, { kind: s.kind, value: s.value, unit: s.unit } as never, "mm");
}
/** The viewer reports its orbit centre (data-target) in board coordinates, rounded to 0.1 mm. */
const viewerTarget = (p: number[]) => p.map((v) => Math.round(v * 10) / 10 || 0).join(",");
// The openings' results first: their pins sit on the lid's top, clear of the Parts panel, which
// covers the canvas's left side and hides the pins under it (the component clearance spot among
// them), so the first located and the first failed result have a pin to wait for and click.
// Each with its item in the Constraints bar (`item`: its constraint's label, from standards.yaml,
// or its check's) and its place in that item's detail (`place`).
const located = locatedResults(report)
  .map((r) => ({ ...r, label: checkLabel(r), target: viewerTarget(r.location.point), item: itemLabel(r), place: placeLabel(r) }))
  .sort((a, b) => Number(b.check === "opening_alignment") - Number(a.check === "opening_alignment"));

// The session canvas: the fixture build, its source, checks and analysis maps.
const build = fixture("build.json") as { maps: string[]; board: { name: string; revision: string } };
// The components of the build's board revision (demos/b-parasite/), for the section cut.
const boardComponents = (
  JSON.parse(readFileSync(path.join(root, `demos/b-parasite/${build.board.name}-${build.board.revision}.json`), "utf8")) as {
    components: { ref: string; role: string | null }[];
  }
).components;
const previousBuild = fixture("previous-build.json");
const diff = fixture("diff.json");
const buildSource = fixture("build-source.json") as { code: string };
const maps = build.maps.filter(isMapType).map((type) => ({
  type,
  label: MAP_LABELS[type],
  flagged: ((fixture(`maps/${type}.json`) as { flagged?: unknown[] }).flagged ?? []).length,
}));

// The design parameters, as the parameters panel labels them, with a new value to type: one
// more for a number (a valid length or angle), a changed word for text.
const parameters = (
  fixture("parameters.json") as { name: string; value: number | string; description?: string; dimension?: unknown }[]
).map(
  (p, index) => ({
    ...p,
    index,
    label: p.description || p.name,
    edit: typeof p.value === "number" ? String(Number((p.value + 1).toFixed(3))) : `${p.value} edited`,
  }),
);
// The constraints: one per standard, and a description that gives one a value.
type Standard = { id: string; title: string; kind: string; value: number | string; unit?: string; aliases: string[] };
const standards = readStandards();
const specStandard = standards.find((s) => s.kind !== "spec")!;
const specValue = Math.round((Number(specStandard.value) + 0.5) * 1000) / 1000;
const spec = {
  id: specStandard.id,
  title: specStandard.title,
  text: `A sensor case with ${specValue}${specStandard.unit === "deg" ? "°" : ` ${specStandard.unit}`} ${specStandard.aliases[0]}`,
};

// The standards editor (docs/constraints.md): the rule catalogue, and the first built-in rule whose
// kind gives sizes (its details show a field per size, and one for the material if it names one).
type Kind = { check: string; title: string; material: boolean; dimensions: { label: string }[] };
const standardKinds = fixture("standards-kinds.json") as Kind[];
const sizedStandard = (() => {
  const s = (fixture("standards.json") as { standards: { check: string; title: string }[] }[])[0].standards.find(
    (r) => standardKinds.find((k) => k.check === r.check)?.dimensions.length,
  )!;
  const kind = standardKinds.find((k) => k.check === s.check)!;
  return { title: s.title, sizes: kind.dimensions.map((d) => d.label.toLowerCase()), material: kind.material };
})();

// The fixture change set: one proposed change per demos/b-parasite/CHANGES.md row, numbered as the pins are.
const changes = (fixture("change-set.json") as { changes: { id: string; title: string }[] }).changes.map((c, i) => ({
  ...c,
  n: i + 1,
}));

// The fixture risk flags, as the chat names their controls, with where the view centres on each.
const risks = (fixture("risk-flags.json") as { flags: (RiskFlag & { location: { point: number[] } })[] }).flags.map((f) => ({
  ...f,
  subject: riskSubject(f),
  target: viewerTarget(f.location.point),
}));
// The sample sessions, as demos/sessions/ lists them.
// Each with its latest build's number as the chat names it ("Build 2": its place in the session's
// builds, `buildName`), from its build folders in order.
const demoSession = (name: string) => JSON.parse(readFileSync(path.join(root, "demos/sessions", name), "utf8"));
const samples = (demoSession("index.json").samples as string[]).map((id) => {
  const session = demoSession(`${id}/session.json`).session;
  const ids = readdirSync(path.join(root, "demos/sessions", id, "builds")).sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
  return { ...session, latest_build_number: ids.indexOf(session.latest_build_id) + 1 };
});

/** The values the inventory's templates take (frontend/e2e/ui-inventory.yaml, "Format"). */
export const vars = {
  models: MODELS,
  more_models: MORE_MODELS,
  efforts: EFFORTS,
  default: {
    model: MODELS.find((m) => m.id === DEFAULT_CHOICES.model)!.name,
    effort: EFFORTS.find((e) => e.id === DEFAULT_CHOICES.effort)!.name,
  },
  empty_heading: EMPTY_HEADING,
  choose_standards: CHOOSE_STANDARDS,
  // The team standards sets, as the session API lists them (fixtures/standards.json).
  standards: fixture("standards.json"),
  question,
  constraints: standards.map(({ id, title }) => ({ id, title })),
  standard_kinds: standardKinds,
  sized_standard: sizedStandard,
  spec,
  risks,
  conflict,
  session: fixture("session.json"),
  samples,
  // As the canvas labels a revision (revisionLabel in HardwareCanvas).
  // Numbered as the chat and History name builds: the older revision's build is the session's
  // first, this one its second, a hand edit's rebuild its third.
  build: { ...build, revision: `v${build.board.revision}`, number: 2, next_number: 3 },
  // The build on the older revision, and the diff from it, as the compare view shows them.
  previous_build: { ...previousBuild, revision: `v${previousBuild.board.revision}`, number: 1 },
  diff: { heading: changesHeading(diff, previousBuild), first_change: featureChangeText(diff.features[0]) },
  build_source: { ...buildSource, first_line: buildSource.code.split("\n")[0] },
  checks: {
    ...report,
    located,
    failed: located.filter((r) => r.status === "fail"),
    untested: untestedResults(report).length,
  },
  changes,
  maps,
  flagged_map: maps.find((m) => m.flagged > 0),
  parts: meshParts.map((p) => p.part),
  faces: meshParts.flatMap((p) => (p.faces ?? []).map((f) => ({ ...f, part: p.part }))),
  edges: meshParts.flatMap((p) => (p.edges ?? []).map((e) => ({ ...e, part: p.part }))),
  parameters,
  // The parameter the locked state edits: the first length.
  length_param: parameters.find((p) => typeof p.value === "number"),
  tagged_features: treeFeatures.filter((f) => f.faces.length).map((f) => ({ ...f, face_ids: f.faces.join(",") })),
  coded_features: treeFeatures.filter((f) => f.lines).map((f) => ({ ...f, lines_attr: f.lines!.join("-") })),
  // Sketch on a face: a full flat face of the fixture mesh, and the panel's choices. Each
  // shape or operation names another to check first, so checking it has an effect.
  sketch_face: sketchTargets().flat,
  sketch_shapes: SKETCH_SHAPES.map((id, i) => ({
    id,
    label: SHAPE_LABELS[id],
    other: SHAPE_LABELS[SKETCH_SHAPES[i === 0 ? SKETCH_SHAPES.length - 1 : 0]],
  })),
  sketch_operations: SKETCH_OPERATIONS.map((id, i) => ({
    id,
    label: OPERATION_LABELS[id],
    other: OPERATION_LABELS[SKETCH_OPERATIONS[i === 0 ? 1 : 0]],
  })),
  sketch_fields: sketchFields("circle").map((f) => fieldLabel(f, "circle", "cut")),
  // The parameters drawn as dimensions on the model, and the one the editing state types.
  dimensions: parameters.filter((p) => p.dimension),
  dimension_param: parameters.find((p) => p.dimension),
  // Modeling tools: the toolbar's tools, and the hole tool's fields (the modeling state's tool).
  modeling_tools: TOOLS.map((t) => ({ id: t.kind, label: t.label, hint: t.hint })),
  hole_fields: editFields("hole").map((f) => editFieldLabel(f, "hole")),
  components: boardComponents,
  board_part: BOARD_PART,
};

export const inv = loadInventory(path.join(root, "frontend/e2e/ui-inventory.yaml"), vars);

function readStandards(): Standard[] {
  return (parse(readFileSync(path.join(root, "standards.yaml"), "utf8")) as { standards: Standard[] }).standards;
}
