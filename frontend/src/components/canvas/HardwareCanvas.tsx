"use client";

import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import {
  Check,
  ChevronDown,
  Code2,
  Download,
  Lock,
  Layers,
  ListChecks,
  Redo2,
  Ruler,
  RulerDimensionLine,
  Scissors,
  SlidersHorizontal,
  Undo2,
} from "lucide-react";
import {
  useCanvasSelection,
  useFoundRequest,
  useSetCanvasSelection,
  useShowRequest,
} from "@/components/sites/claude-shell/canvas-selection";
import {
  CodeEditError,
  createExport,
  getBoard,
  getChecks,
  updateSource,
  useBuildSource,
  useChangeSet,
  useParameters,
  type Build,
  type BuildSource,
  type CheckReport,
  type Export,
  type Parameter,
} from "./api";
import { findPicks } from "./find";
import {
  featureKey,
  featurePicks,
  featureTree,
  linesLabel,
  pickedFeatures,
  type FeatureNode,
  type PartNode,
} from "./featureTree";
import {
  DEFAULT_FORMATS,
  downloadLabel,
  fileCount,
  FORMAT_OPTIONS,
  noteSummary,
  requestedFormats,
  type FileFormat,
} from "./exportFormats";
import { formatLength, formatSize, fromMm, LENGTH_UNITS, type LengthUnit } from "./units";
import {
  AXES,
  componentClearance,
  LAYOUT_LABELS,
  LAYOUTS,
  type Axis,
  type Board,
  type Clearance,
  type Layout,
} from "./inspect";
import { revisionLabel } from "./history";
import { reportAction, type Location, type QuestionOption, type Selection } from "@/lib/session-api";
import type { Block } from "@/components/sites/claude-shell/chat";
import { QuestionOptions } from "@/components/sites/claude-shell/ChatMessages";
import { ConflictCard } from "@/components/sites/claude-shell/ConflictCard";
import {
  CLICK_SLOP_PX,
  MAX_ZOOM,
  Viewer,
  type Dimension,
  type FaceDragEvent,
  type ModelInfo,
  type Section,
  type ViewName,
  type ViewSettings,
  type ViewerState,
} from "./viewer";
import { AxisTriad, ViewCube } from "./ViewCube";
import type { Target } from "./orientation";
import { ShortcutSheet } from "./ShortcutSheet";
import { findShortcut, isTypingTarget, type ShortcutAction } from "./shortcuts";
import { ParametersPanel } from "./ParametersPanel";
import { CheckOverlay, CheckPins } from "./CheckOverlay";
import { ConstraintsPanel } from "./ConstraintsPanel";
import { pinFilterFor, useOpenChecksRequest, type OpenChecksRequest } from "./openChecks";
import { failedLatest, fixFailureText, shownBuild, stepBlocked, stepTitle, type Direction } from "./designHistory";
import { FailedBuildBanner, HistoryToast } from "./HistoryNotices";
import { useDesignHistory } from "./useDesignHistory";
import { ChangePanel, ChangePins } from "./ChangeOverlay";
import { Dimensions, type Dragging } from "./Dimensions";
import { draggedValue, type Rect } from "./parameters";
import { useParameterEdits } from "./useParameterEdits";
import { boardPartNames, isBoardPick, viewPartsOf } from "./boardParts";
import { BoardPartRows, PartRow } from "./PartRow";
import { locationPart } from "./explode";
import { chromeVars, outsideBand, PANEL, PLACE } from "./chromeLayout";
import { pinAnchor, type Point3 } from "./checks";
import { hasPin, loadPinFilter, savePinFilter, type PinFilter } from "./pinVisibility";
import { sectionTargets, sectionThrough, type SectionTarget } from "./sectionTargets";
import { CommentsLayer } from "./comments/CommentsLayer";
import { useCommentTool } from "./comments/useComments";
import {
  highlightPicks,
  hideParts,
  isIsolated,
  isolate,
  isolateParts,
  partView,
  pickAs,
  resolveId,
  showAll,
  toCanvasSelection,
  togglePick,
  type Picked,
  type PartViews,
} from "./selection";
import { shownError } from "@/lib/errors";


type Menu = "changes" | "layout" | "export" | "checks" | "code" | "settings" | "zoom" | "section-component";

/**
 * The code being edited: the build it was edited from, the text, and why the API refused it.
 * It lives in the canvas, so closing the code panel or a refused rebuild never loses the edit.
 */
type CodeDraft = { buildId: string; code: string; error?: { message: string; line: number | null } };

/** Claude's open question about a spot on the model, pinned there with its options. */
export type PinnedQuestion = Extract<Block, { kind: "question" }> & { location: Location };

/** A flagged spot sent to Claude: the request text and the spot as the selection. */
export type FixRequest = { text: string; selection: Selection };

const ZOOM_PRESETS = [50, 100, 200, 400];
const ZOOM_STEP = 1.25;

export { revisionLabel };

const iconBtn =
  "grid size-7 shrink-0 place-items-center rounded-md text-[var(--cs-muted)] hover:bg-[var(--cs-hover)] hover:text-[var(--cs-text)] aria-expanded:bg-[var(--cs-active)] aria-expanded:text-[var(--cs-text)]";
const pill = "flex h-8 items-center gap-1.5 rounded-lg bg-[var(--cs-surface-2)] px-2.5 text-[13px]";
const panel =
  "absolute top-full z-20 mt-1.5 rounded-xl border border-[var(--cs-border)] bg-[#262626] p-1.5 text-[13px] shadow-xl";
const menuItem =
  "flex w-full items-center gap-2 rounded-md px-2.5 py-1.5 text-left hover:bg-[var(--cs-hover)] aria-checked:text-[var(--cs-text)]";

/**
 * The 3D canvas: a three.js viewer of a build's glTF (mesh contract) with orbit, pan, zoom,
 * fit and a view cube, mm/in units and the top-right controls (undo and redo, export, run
 * checks, code, view settings, zoom). Clicking picks a face, or a
 * board component whole (shift- or Cmd-click adds or removes). The selection is
 * highlighted and becomes the chat's selection chip. The parts panel hides, isolates and makes
 * parts see-through. In the parameters panel a typed value locks a parameter
 * and rebuilds. Claude's open question about a spot (`question`) is pinned
 * there with its options, which `onAnswer` answers. With dimensions shown, each parameter's
 * dimension is drawn on the model; clicking one types a value, and dragging a face of a
 * draggable feature changes its parameter. Both are hand edits like the panel's.
 * The inspection tools draw the build's board revision inside the enclosure: a
 * section cut through any component with that component's clearance, and the Assembled /
 * Exploded / Print layouts; picks and check pins follow the parts.
 */
export function HardwareCanvas({
  sessionId,
  builds,
  selectedId: shownId,
  onSelect,
  onFixRequest,
  fixDisabled = false,
  fixBlocked,
  question,
  onAnswer,
  onSendComments,
  onAnswerInThread,
  readOnly,
}: {
  sessionId: string;
  /** Why the design can't be edited by hand (a read-only shared link); none: it can. */
  readOnly?: string;
  builds: Build[];
  /** With `onSelect`, the parent owns which build is shown (null: the latest). */
  selectedId?: string | null;
  onSelect?: (buildId: string | null) => void;
  onFixRequest?: (request: FixRequest) => void;
  /** Sends comments to Claude: one run answers each in its thread. */
  onSendComments?: (ids: string[]) => void | Promise<void>;
  /** Answers a question Claude asked in a comment's run, from the thread. */
  onAnswerInThread?: (questionId: string, optionId: string) => void | Promise<void>;
  /** Claude is busy: fix requests and answers wait. */
  fixDisabled?: boolean;
  /** Why fix requests can't be sent now although Claude isn't busy (a question is open). */
  fixBlocked?: string;
  question?: PinnedQuestion;
  onAnswer?: (questionId: string, option: QuestionOption) => void;
}) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const viewerRef = useRef<Viewer | null>(null);
  const barRef = useRef<HTMLDivElement>(null);
  const [ownId, setOwnId] = useState<string | null>(null);
  const selectedId = onSelect ? (shownId ?? null) : ownId;
  const setSelectedId = onSelect ?? setOwnId;
  // The build shown and acted on (comments, edits, export): never a failed one. A
  // failed newest build is reported over the last good model instead (`failed`).
  const build = shownBuild(builds, selectedId);
  const failed = failedLatest(builds, build);
  const [model, setModel] = useState<ModelInfo | null>(null);
  const [loadState, setLoadState] = useState<"loading" | "ready" | "error">("loading");
  const [view, setView] = useState<ViewerState | null>(null);
  const [unit, setUnit] = useState<LengthUnit>("mm");
  const [settings, setSettings] = useState<ViewSettings>({ edges: true, grid: true });
  const [menu, setMenu] = useState<Menu | null>(null);
  const [paramsOpen, setParamsOpen] = useState(false);
  const [codeDraft, setCodeDraft] = useState<CodeDraft | null>(null);
  const [dimensionsOn, setDimensionsOn] = useState(false);
  const [dragging, setDragging] = useState<Dragging | null>(null);
  // The canvas follows the rebuild after a hand edit.
  const parameters = useParameterEdits(sessionId, builds, () => setSelectedId(null));
  const [picks, setPicks] = useState<Picked[]>([]);
  const [partViews, setPartViews] = useState<PartViews>({});
  // Inspection: layout and the section cut.
  const [layout, setLayout] = useState<Layout>("assembled");
  const [sectionOn, setSectionOn] = useState(false);
  const [sectionRef, setSectionRef] = useState<string | null>(null);
  const [sectionAxis, setSectionAxis] = useState<Axis>("x");
  // The build's board, once loaded (or why not): only a build checked against a board has one.
  const [loadedBoard, setLoadedBoard] = useState<{ url: string; board: Board | null } | null>(null);
  // Each enclosure part's triangles in the assembled design, for clearances.
  const [enclosure, setEnclosure] = useState<{ part: string; triangles: Float64Array }[]>([]);
  // The comment tool: a click pins a draft comment instead of selecting.
  const commentTool = useCommentTool();
  const setChatSelection = useSetCanvasSelection();
  const chatSelection = useCanvasSelection();
  // The build's code and parameters: the Code panel and the feature tree show them.
  const code = useBuildSource(build);
  const params = useParameters(sessionId, build?.id);
  // The feature whose lines the Code panel shows, from the tree's code links.
  const [codeFeature, setCodeFeature] = useState<FeatureNode | null>(null);

  const [checks, setChecks] = useState<{ buildId: string; report: CheckReport | null; error: string | null } | null>(
    null,
  );
  const [checksRun, setChecksRun] = useState(0);
  const [selectedCheck, setSelectedCheck] = useState<string | null>(null);
  // Which check pins show (failed only by default), remembered per browser. The pin of the check
  // the user picked always shows, and so does one the chat asked to show.
  const [pinFilter, setPinFilter] = useState<PinFilter>(() => loadPinFilter());
  const choosePinFilter = (f: PinFilter) => {
    setPinFilter(f);
    savePinFilter(f);
  };
  const [chatShownCheck, setChatShownCheck] = useState<string | null>(null);
  // A check count clicked (a chat build card, say) opens the Constraints panel on its group (./openChecks).
  const [revealChecks, setRevealChecks] = useState<OpenChecksRequest | null>(null);
  useOpenChecksRequest(!!build, (r) => {
    setMenu("checks");
    setPinFilter(pinFilterFor(r.status));
    setRevealChecks(r);
  });
  // Closed, the panel forgets the group: opened again from the toolbar it marks none.
  if (revealChecks && menu !== "checks") setRevealChecks(null);

  // The build's check results: the pins and the Constraints panel show them.
  useEffect(() => {
    if (!build) return;
    let live = true;
    getChecks(build)
      .then((report) => live && setChecks({ buildId: build.id, report, error: null }))
      .catch((e: Error) => live && setChecks({ buildId: build.id, report: null, error: shownError(e) }));
    return () => {
      live = false;
    };
  }, [build, checksRun]);
  const report = checks && checks.buildId === build?.id ? checks.report : null;
  // Each pin on the model: the point on the faces its check names nearest the reported point.
  const [anchors, setAnchors] = useState<{ report: CheckReport; model: ModelInfo; points: Map<string, Point3> } | null>(null);
  // The run's proposed changes, shown on the build they apply to.
  const [changeSet, setChangeSet] = useChangeSet(sessionId);
  const [selectedChange, setSelectedChange] = useState<string | null>(null);
  const proposed = changeSet?.changes.filter((c) => c.status === "proposed").length ?? 0;
  const checksError = checks && checks.buildId === build?.id ? checks.error : null;

  // One viewer for the canvas's lifetime; builds load into it.
  useEffect(() => {
    const viewer = new Viewer(viewportRef.current!);
    viewerRef.current = viewer;
    const unsubscribe = viewer.subscribe(setView);
    return () => {
      unsubscribe();
      viewer.dispose();
      viewerRef.current = null;
    };
  }, []);

  const meshUrl = build?.mesh_url;
  useEffect(() => {
    const viewer = viewerRef.current;
    if (!viewer || !meshUrl) return;
    let live = true;
    setLoadState("loading");
    viewer
      .load(meshUrl)
      .then((info) => {
        if (!live) return;
        setModel(info);
        setEnclosure(viewer.partTriangles());
        setLoadState("ready");
      })
      .catch(() => live && setLoadState("error"));
    return () => {
      live = false;
    };
  }, [meshUrl]);

  // With dimensions shown, the faces of draggable features drag their parameter. A drag that
  // ends is a hand edit, and the parameters panel opens on its lock and the rebuild's checks.
  const faceDragged = (e: FaceDragEvent) => {
    if (!e.done) return setDragging({ name: e.name, distance: e.distance });
    setDragging(null);
    const param = parameters.params?.find((p) => p.name === e.name);
    const next = param && draggedValue(param, e.distance, unit);
    if (!param || !next) return;
    if ("error" in next) return parameters.setError(param.name, next.error);
    parameters.edit(param, next.value).then((ok) => ok && setParamsOpen(true));
  };
  const onFaceDrag = useRef(faceDragged);
  useEffect(() => {
    onFaceDrag.current = faceDragged;
  });
  const dragParams = dimensionsOn ? (parameters.params ?? []).filter((p) => p.drag) : [];
  const dragKey = JSON.stringify(dragParams.map((p) => [p.name, p.drag]));
  useEffect(() => {
    const drags = (JSON.parse(dragKey) as [string, { faces: string[]; axis: [number, number, number] }][]).map(
      ([name, drag]) => ({ name, ...drag }),
    );
    viewerRef.current?.setFaceDrags(drags, (e) => onFaceDrag.current(e));
  }, [dragKey]);

  // The build's board revision, drawn inside the enclosure. A design without a board (a plain
  // request) never asks for one; a board that fails to load is said so, never loads forever.
  const boardUrl = build?.board ? build.board_url : undefined;
  useEffect(() => {
    if (!boardUrl) return;
    let live = true;
    getBoard({ board_url: boardUrl })
      .then((b) => live && setLoadedBoard({ url: boardUrl, board: b }))
      .catch(() => live && setLoadedBoard({ url: boardUrl, board: null }));
    return () => {
      live = false;
    };
  }, [boardUrl]);
  const boardLoad = boardUrl && loadedBoard?.url === boardUrl ? loadedBoard : null;
  const board = boardLoad?.board ?? null;
  const boardFailed = !!boardLoad && !boardLoad.board;
  useEffect(() => viewerRef.current?.setBoard(board), [board]);
  // The board and its components are parts too (hide, isolate, see-through), after the enclosure's.
  const boardParts = useMemo(() => (board ? boardPartNames(board, model?.parts) : []), [board, model]);
  const allParts = useMemo(() => [...(model?.parts ?? []), ...boardParts], [model, boardParts]);
  useEffect(() => viewerRef.current?.setLayout(layout), [layout]);

  // Every component's clearance to the enclosure, once the model and its board are both loaded.
  const clearances = useMemo<Clearance[]>(
    () => (board && enclosure.length ? board.components.flatMap((c) => componentClearance(board, c, enclosure) ?? []) : []),
    [enclosure, board],
  );
  const closest = clearances.reduce<Clearance | null>((a, b) => (!a || b.distance < a.distance ? b : a), null);

  // The section: through a part of the design or a board component (the one closest to the
  // enclosure to begin with, or the first part without a board), normal to the axis.
  const targets = useMemo(() => sectionTargets(enclosure, board), [enclosure, board]);
  const sectionTarget = targets.find((t) => t.id === (sectionRef ?? closest?.ref)) ?? targets[0] ?? null;
  const section: Section | null = sectionOn && sectionTarget ? sectionThrough(sectionTarget, sectionAxis) : null;
  const sectionClearance = section ? clearances.find((c) => c.ref === section.ref) ?? null : null;
  const sectionKey = section ? `${section.ref}:${section.axis}:${section.at}` : "";
  useEffect(() => {
    viewerRef.current?.setSection(sectionKey ? section : null);
    // The section is rebuilt from its key, so a new object for the same section does nothing.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sectionKey]);

  useEffect(() => {
    const viewer = viewerRef.current;
    if (!viewer || !report || !model) return;
    const owner = new Map(model.index.flatMap((p) => p.faces.map((f) => [f.id, p.part] as const)));
    const points = new Map<string, Point3>();
    for (const r of report.results) {
      if (!hasPin(r) || !r.location) continue;
      const loc = r.location;
      const faces = (loc.faces ?? []).flatMap((id) => {
        const part = owner.get(id);
        return part ? [viewer.faceTriangles(part, id)] : [];
      });
      const part = enclosure.find((e) => e.part === loc.part)?.triangles ?? null;
      const tris = faces.some((t) => t.length) ? concatTriangles(faces) : part;
      points.set(r.id, pinAnchor(loc.point, tris));
    }
    // The anchors belong to this report and model; they are worked out once both are loaded.
    setAnchors({ report, model, points });
  }, [report, model, enclosure]);
  // The report as the pins and the card show it: each located result at its pin's anchor.
  const pinReport = useMemo(() => {
    if (!report || !anchors || anchors.report !== report || anchors.model !== model) return null;
    return {
      ...report,
      results: report.results.map((r) => {
        const at = anchors.points.get(r.id);
        return at && r.location ? { ...r, location: { ...r.location, point: at } } : r;
      }),
    };
  }, [report, anchors, model]);
  const checkLocation = <L,>(r: { id: string; location: L }) =>
    (report?.results.find((x) => x.id === r.id)?.location as L | undefined) ?? r.location;
  const revealedPins = useMemo(
    () => new Set([selectedCheck, chatShownCheck].filter((id): id is string => !!id)),
    [selectedCheck, chatShownCheck],
  );

  // Dimension lines: the section's clearance, on the assembled design only.
  const dimensions: Dimension[] =
    layout !== "assembled"
      ? []
      : [...(sectionClearance && sectionClearance.distance > 0 ? [sectionClearance] : [])].map((c) => ({ from: c.from, to: c.to, label: formatPrecise(c.distance, unit) }));
  const dimensionKey = JSON.stringify(dimensions);
  useEffect(() => {
    viewerRef.current?.setDimensions(JSON.parse(dimensionKey) as Dimension[]);
  }, [dimensionKey, model]);

  useEffect(() => viewerRef.current?.setSettings(settings), [settings]);
  useEffect(() => viewerRef.current?.setPartViews(partViews), [partViews]);
  // Re-applied after a new build loads: face and edge IDs survive rebuilds.
  useEffect(
    () =>
      viewerRef.current?.setHighlight(model ? [...highlightPicks(model.index, picks), ...picks.filter(isBoardPick)] : picks),
    [picks, model, board],
  );

  // The chat consumed the selection (sent it, or the chip was cleared): drop the highlight,
  // unless the selection was cleared to show what Claude found instead.
  const hadSelection = useRef(false);
  const keepPicks = useRef(false);
  useEffect(() => {
    if (hadSelection.current && !chatSelection && !keepPicks.current) setPicks([]);
    keepPicks.current = false;
    hadSelection.current = !!chatSelection;
  }, [chatSelection]);

  const select = (next: Picked[], point: [number, number, number] | null = null, label?: string) => {
    setPicks(next);
    const s = model ? toCanvasSelection(model.index, next, point) : null;
    setChatSelection(s?.selection ?? null, label ?? s?.label);
  };

  // What Claude found in chat, highlighted without becoming the chat's selection.
  const highlight = (next: Picked[]) => {
    if (chatSelection) {
      keepPicks.current = true;
      setChatSelection(null);
    }
    setPicks(next);
  };

  // The chat asked to show a place (a flagged risk): select its faces, or its feature,
  // and centre the view on its point. Once per request, as soon as the model is loaded.
  const showRequest = useShowRequest();
  const shown = useRef(0);
  useEffect(() => {
    if (!showRequest || !model || showRequest.n === shown.current) return;
    shown.current = showRequest.n;
    const { faces, feature, component, part, point } = showRequest.location;
    const byFace = faces.flatMap((id): Picked[] => {
      const owner = model.index.find((p) => p.faces.some((f) => f.id === id));
      return owner ? [{ kind: "face", id, part: owner.part }] : [];
    });
    const next = byFace.length ? byFace : resolveId(model.index, feature || component || part || "");
    select(next, point ?? null);
    if (point) viewerRef.current?.focus(point);
    // A check at that place shows its pin, whatever the pin filter.
    const at = point && report?.results.find((r) => r.location?.point.every((v, i) => Math.abs(v - point[i]) < 1e-6));
    setChatShownCheck(at ? at.id : null);
    // `select` is a new function each render; the request number keeps this to once per request.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showRequest, model]);

  // Faces Claude found in chat: highlighted, or made the selection. Once per request.
  const foundRequest = useFoundRequest();
  const foundShown = useRef<string | null>(null);
  useEffect(() => {
    if (!foundRequest || !model || foundRequest.key === foundShown.current) return;
    foundShown.current = foundRequest.key;
    const picks = findPicks(foundRequest.result);
    // A request from the chat, applied once (like `showRequest` above): it sets the picks.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (foundRequest.select) select(picks, null, foundRequest.result.understood);
    else highlight(picks);
    // `select` and `highlight` are new functions each render; the request number keeps this to once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [foundRequest, model]);

  // A click (not a drag) picks the face under the pointer. Shift or Cmd (Ctrl) adds to the
  // selection.
  const down = useRef<{ x: number; y: number } | null>(null);
  const onPointerDown = (e: React.PointerEvent<HTMLElement>) => {
    e.currentTarget.focus({ preventScroll: true });
    down.current = e.button === 0 ? { x: e.clientX, y: e.clientY } : null;
  };
  const onPointerMove = (e: React.PointerEvent) => {
    if (!down.current && e.pointerType === "mouse") previewHover(e.clientX, e.clientY);
  };
  const onPointerUp = (e: React.PointerEvent) => {
    const start = down.current;
    down.current = null;
    if (!start) return;
    const additive = e.shiftKey || e.metaKey || e.ctrlKey;
    if (Math.hypot(e.clientX - start.x, e.clientY - start.y) >= CLICK_SLOP_PX) return;
    if (commentTool.active) {
      const hit = viewerRef.current?.pickAt(e.clientX, e.clientY, "face");
      commentTool.place(hit ? { face: hit.pick.id, part: hit.pick.part, point: hit.point } : null);
      return;
    }
    const hit = viewerRef.current?.pickAt(e.clientX, e.clientY, "face");
    // A board part is picked whole.
    const pick = hit && model ? (hit.board ? hit.pick : pickAs(model.index, hit.pick, "face")) : null;
    if (!hit || !pick) {
      if (!additive) select([]);
      return;
    }
    select(additive ? togglePick(picks, pick) : [pick], hit.point);
  };
  // What a click would pick under the pointer, previewed on the model, once a frame at most.
  // Not while commenting, where a click does something else.
  const hoverFrame = useRef<number | null>(null);
  const [hovered, setHovered] = useState("");
  const previewHover = (x: number, y: number) => {
    if (hoverFrame.current !== null) return;
    hoverFrame.current = requestAnimationFrame(() => {
      hoverFrame.current = null;
      const viewer = viewerRef.current;
      if (!viewer || !model) return;
      let pick: Picked | null = null;
      if (!commentTool.active) {
        const hit = viewer.pickAt(x, y, "face");
        pick = hit ? (hit.board ? hit.pick : pickAs(model.index, hit.pick, "face")) : null;
      }
      const shown = pick ? (isBoardPick(pick) ? [pick] : highlightPicks(model.index, [pick])) : [];
      viewer.setHover(shown);
      setHovered(pick ? `${pick.kind}:${pick.id}` : "");
    });
  };
  const clearHover = () => {
    viewerRef.current?.setHover([]);
    setHovered("");
  };
  useEffect(
    () => () => {
      if (hoverFrame.current !== null) cancelAnimationFrame(hoverFrame.current);
    },
    [],
  );

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || menu || (e.target as HTMLElement)?.closest?.("input, textarea")) return;
      down.current = null;
      setPicks([]);
      setChatSelection(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [menu, setChatSelection]);

  // The pinned question's spot: the viewer reports where it is on screen as the view changes.
  const pinPoint = question?.location.point;
  const pinKey = pinPoint?.join(",");
  useEffect(() => {
    viewerRef.current?.setMarker(pinKey ? (pinKey.split(",").map(Number) as [number, number, number]) : null);
  }, [pinKey]);


  // Close an open menu on Escape or a click outside the control bar.
  useEffect(() => {
    if (!menu) return;
    const onDown = (e: PointerEvent) => {
      // Menus outside the control bar (the section's component menu) mark their host.
      if (barRef.current?.contains(e.target as Node) || (e.target as Element).closest?.("[data-menu-host]")) return;
      setMenu(null);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setMenu(null);
    window.addEventListener("pointerdown", onDown);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("pointerdown", onDown);
      window.removeEventListener("keydown", onKey);
    };
  }, [menu]);

  // The view cube target under the pointer, for the view label.
  const [hoverTarget, setHoverTarget] = useState<Target | null>(null);

  const toggle = (m: Menu) => setMenu((cur) => (cur === m ? null : m));
  // Undo and redo over the design timeline: every change, whoever made it (a hand edit, a
  // Claude run, a review). Off while Claude is working; like a hand edit, on while its question waits.
  const timeline = useDesignHistory(sessionId, builds, fixDisabled);
  const stepWhy = (to: Direction) =>
    readOnly ||
    stepBlocked(timeline.history, to, {
      busy: fixDisabled,
      // A shared link says why it is read-only; Claude's open question doesn't block undo.
      blocked: fixDisabled ? fixBlocked : undefined,
      stepping: timeline.stepping,
    });
  const step = (to: Direction) => {
    if (stepWhy(to)) return;
    timeline.move(to).then((moved) => {
      if (!moved) return;
      setSelectedId(null); // the canvas follows the rebuild
      // The change set comes back as it was at that step (or goes, before the run that made it).
      setChangeSet(moved.change_set ?? null);
    });
  };
  const viewer = () => viewerRef.current;
  const setCameraView = (v: ViewName) => viewer()?.setView(v);

  // Keyboard shortcuts (keymap in ./shortcuts): one handler per action the canvas supports.
  // Bindings without a handler stay off the sheet until their feature adds one.
  const [sheetOpen, setSheetOpen] = useState(false);
  // The section cut, reported to the session's telemetry log as it turns on and off.
  const toggleSection = () => {
    reportAction(sessionId, { kind: "view", view: "section", value: sectionOn ? "off" : "on" });
    setSectionOn(!sectionOn);
  };
  const shortcutHandlers: Partial<Record<ShortcutAction, () => void>> = {
    "view-front": () => setCameraView("front"),
    "view-top": () => setCameraView("top"),
    "view-right": () => setCameraView("right"),
    "view-iso": () => setCameraView("iso"),
    fit: () => viewer()?.fit(),
    "tool-section": toggleSection,
    hide: () => model && setPartViews((v) => hideParts(v, viewPartsOf(picks).filter((p) => allParts.includes(p)))),
    isolate: () =>
      model && setPartViews((v) => isolateParts(v, allParts, viewPartsOf(picks).filter((p) => allParts.includes(p)))),
    "show-all": () => model && setPartViews((v) => showAll(v, allParts)),
    "tool-comment": () => commentTool.toggle(),
    help: () => setSheetOpen((open) => !open),
    undo: () => step("undo"),
    redo: () => step("redo"),
  };
  const handlersRef = useRef(shortcutHandlers);
  useEffect(() => {
    handlersRef.current = shortcutHandlers;
  });
  const hasShortcut = (action: ShortcutAction) => action in shortcutHandlers;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.repeat || e.isComposing) return;
      // Never while typing, or while a modal (such as search) has the keyboard.
      if (isTypingTarget(e.target) || isTypingTarget(document.activeElement)) return;
      if (document.querySelector('[aria-modal="true"]')) return;
      if (e.key === "Escape") {
        setSheetOpen(false);
        return;
      }
      const handlers = handlersRef.current;
      const shortcut = findShortcut(e, (a) => !!handlers[a]);
      if (!shortcut) return;
      e.preventDefault();
      handlers[shortcut.action]!();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // With the canvas focused: arrow keys turn 90°, Shift with Left or Right rolls.
  // Home is `0` in the canvas keymap (shortcuts.ts); H hides the selection there.
  const onCanvasKey = (e: React.KeyboardEvent) => {
    const v = viewer();
    if (!v || !hasModel || e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey) return;
    if (isTypingTarget(e.target) || (e.target as Element).closest('[role="menu"], [role="dialog"]')) return;
    const turns: Record<string, "up" | "down" | "left" | "right"> = {
      ArrowUp: "up",
      ArrowDown: "down",
      ArrowLeft: "left",
      ArrowRight: "right",
    };
    if (e.shiftKey && (e.key === "ArrowLeft" || e.key === "ArrowRight")) v.roll(e.key === "ArrowLeft" ? "ccw" : "cw");
    else if (!e.shiftKey && turns[e.key]) v.turn(turns[e.key]);
    else return;
    e.preventDefault();
  };

  const hasModel = loadState === "ready" && !!model;
  // The side column of the chrome (chromeLayout.ts): the parts panel.
  const partsShown = hasModel;
  // Where the comment tools go: in the bottom bar, with the layout and inspection tools.
  const [commentSlot, setCommentSlot] = useState<HTMLDivElement | null>(null);

  return (
    <div
      className="@container absolute inset-0 overflow-clip"
      style={chromeVars({ parts: partsShown })}
      onKeyDown={onCanvasKey}
    >
      <div
        ref={viewportRef}
        data-testid="viewer"
        data-state={loadState}
        data-build={build?.id}
        data-zoom={view?.zoom}
        data-zoom-min={view?.minZoom}
        data-zoom-max={view?.maxZoom}
        data-model-rect={view?.modelRect?.join(",")}
        data-view={view?.view}
        data-target={view?.target.join(",")}
        data-fits={view?.fits ? "true" : "false"}
        data-edges={settings.edges}
        data-grid={settings.grid}
        data-commenting={commentTool.active}
        data-dimensions={dimensionsOn ? "true" : "false"}
        data-selected={picks.map((p) => p.id).join(",")}
        data-hidden={allParts.filter((p) => partView(partViews, p).hidden).join(",")}
        data-transparent={allParts.filter((p) => partView(partViews, p).transparent).join(",")}
        data-board-parts={boardParts.join(",")}
        data-view-label={view?.label}
        data-roll={view?.roll}
        data-azimuth={view?.azimuth}
        data-elevation={view?.elevation}
        data-up-angle={view?.upAngle ?? ""}
        data-animating={view?.animating ? "true" : "false"}
        data-restored-from={build?.restored_from ?? ""}
        data-animation-progress={view?.progress ?? ""}
        data-layout={layout}
        data-section={sectionKey}
        data-board={board ? `${board.name} ${board.revision}` : ""}
        data-board-to-clip={view?.boardToClip.join(",")}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerLeave={clearHover}
        data-hover={hovered}
        aria-label={`3D view. Click to select, shift- or Cmd-click to add or remove. Drag to orbit${dimensionsOn ? " (drag a face that has a dimension to change it)" : ""}, right-drag or shift-drag to pan, scroll to zoom, arrow keys to turn.`}
        role="img"
        tabIndex={0}
        className={`absolute inset-y-0 right-0 touch-none outline-none focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-[var(--cs-accent)] ${commentTool.active ? "cursor-crosshair" : "cursor-grab active:cursor-grabbing"} left-0`}
      />

      {/* The newest build failed: said plainly over the last good model, with Undo and a fix;
          and the toast after an undo or redo. */}
      {(failed || timeline.last || timeline.error) && (
        <div className="pointer-events-none absolute inset-x-0 top-14 z-30 flex flex-col items-center gap-2 px-4">
          {failed && (
            <FailedBuildBanner
              build={failed}
              onUndo={() => step("undo")}
              undoWhy={stepWhy("undo")}
              onFix={onFixRequest && (() => onFixRequest({ text: fixFailureText(failed, timeline.history), selection: { faces: [], edges: [] } }))}
              fixWhy={fixDisabled ? "Claude is working" : fixBlocked}
            />
          )}
          <HistoryToast
            move={timeline.last}
            error={timeline.error}
            onAgain={() => step(timeline.last?.direction === "undo" ? "redo" : "undo")}
            againWhy={stepWhy(timeline.last?.direction === "undo" ? "redo" : "undo")}
            onDismiss={timeline.dismiss}
          />
        </div>
      )}

      {/* The pins come before the panels, so the panels draw over them. */}
      {loadState === "ready" && pinReport && (
        <CheckPins
          report={pinReport}
          filter={pinFilter}
          revealed={revealedPins}
          selectedId={selectedCheck}
          onSelect={(r) => {
            setSelectedCheck(r.id);
            // The camera goes to the point the check reported; the pin is on the face nearest it.
            const loc = checkLocation(r);
            viewer()?.focus(loc.point, undefined, model && locationPart(loc, model.index));
          }}
          // A pin moves with its part in the exploded view.
          toScreen={(loc) => viewerRef.current?.toScreen(loc.point, model && locationPart(loc, model.index)) ?? null}
          onWheel={(e) => viewer()?.forwardWheel(e)}
          onDrag={(down, move) => viewer()?.forwardDrag(down, move)}
          // Not while a drag on a face changes a dimension.
          showPins={!dimensionsOn}
        />
      )}

      <div ref={barRef} className="pointer-events-none absolute inset-x-0 top-0 flex min-w-0 items-start gap-2 p-3">
        {/* The proposed changes, with accept and reject per change */}
        {changeSet && (
          <div className="pointer-events-auto relative shrink-0">
            <button
              type="button"
              aria-label="Proposed changes"
              title={`${proposed} of ${changeSet.changes.length} changes to review`}
              aria-expanded={menu === "changes"}
              onClick={() => toggle("changes")}
              className={`${pill} hover:bg-[var(--cs-surface)] aria-expanded:bg-[var(--cs-active)]`}
            >
              <span className="grid size-4 place-items-center rounded-full bg-[#f0a27a] text-[10px] font-bold text-[#1f1f1f]">
                {proposed}
              </span>
              Changes
              <ChevronDown className="size-3.5 text-[var(--cs-muted)]" />
            </button>
            {menu === "changes" && (
              <ChangePanel
                sessionId={sessionId}
                changeSet={changeSet}
                busy={fixDisabled}
                selectedId={selectedChange}
                onReviewed={(set) => {
                  setChangeSet(set);
                  // The canvas follows the rebuild.
                  setSelectedId(null);
                }}
                className={`${panel} left-0 w-80 p-3`}
              />
            )}
          </div>
        )}

        {/* Units are in View settings; this keeps the toolbar at the right. */}
        <span className="mx-auto" />

        {/* Top-right controls */}
        <div
          role="toolbar"
          aria-label="Canvas controls"
          data-pin-cover=""
          className="pointer-events-auto flex h-8 min-w-0 items-center gap-0.5 rounded-lg bg-[var(--cs-surface-2)] px-1"
        >
          {/* On a narrow canvas the icons scroll sideways inside the toolbar; the zoom stays in view. */}
          <div className="flex min-w-0 items-center gap-0.5 overflow-x-auto [scrollbar-width:none]">
          {/* Undo and redo lead the toolbar. */}
          {!readOnly && hasModel && (
            <>
              <button type="button" aria-label="Undo" title={stepWhy("undo") ?? stepTitle(timeline.history, "undo", undefined) ?? "Undo (⌘Z)"} disabled={!!stepWhy("undo")} onClick={() => step("undo")} className={`${iconBtn} disabled:opacity-40`}>
                <Undo2 className="size-3.5" />
              </button>
              <button type="button" aria-label="Redo" title={stepWhy("redo") ?? stepTitle(timeline.history, "redo", undefined) ?? "Redo (⇧⌘Z)"} disabled={!!stepWhy("redo")} onClick={() => step("redo")} className={`${iconBtn} disabled:opacity-40`}>
                <Redo2 className="size-3.5" />
              </button>
              <span aria-hidden className="mx-0.5 h-4 w-px bg-[var(--cs-border)]" />
            </>
          )}
          <button
            type="button"
            aria-label="Parameters"
            title="Parameters"
            aria-pressed={paramsOpen}
            onClick={() => {
              setParamsOpen((v) => !v);
            }}
            className={`${iconBtn} aria-pressed:bg-[var(--cs-active)] aria-pressed:text-[var(--cs-text)]`}
          >
            <Ruler className="size-3.5" />
          </button>
          {/* A read-only session (a shared link) has no edit tools: they'd be refused. */}
          {!readOnly && (
          <button
            type="button"
            aria-label="Dimensions"
            title="Dimensions: click one to type a value, drag a face to change it"
            aria-pressed={dimensionsOn}
            onClick={() => setDimensionsOn((v) => !v)}
            className={`${iconBtn} aria-pressed:bg-[var(--cs-active)] aria-pressed:text-[var(--cs-text)]`}
          >
            <RulerDimensionLine className="size-3.5" />
          </button>
          )}
          {hasModel && (
            <>
              <MenuButton label="Layout" icon={Layers} open={menu === "layout"} onClick={() => toggle("layout")} />
              <button type="button" aria-label="Section cut" title="Section cut" aria-pressed={sectionOn} onClick={toggleSection} className={`${iconBtn} aria-pressed:bg-[var(--cs-active)] aria-pressed:text-[var(--cs-text)]`}>
                <Scissors className="size-3.5" />
              </button>
            </>
          )}
          <MenuButton label="Export" icon={Download} open={menu === "export"} onClick={() => toggle("export")} />
          <MenuButton label="Constraints" icon={ListChecks} open={menu === "checks"} onClick={() => toggle("checks")} />
          <MenuButton
            label="Code"
            icon={Code2}
            open={menu === "code"}
            onClick={() => {
              setCodeFeature(null);
              toggle("code");
            }}
          />
          <MenuButton
            label="View settings"
            icon={SlidersHorizontal}
            open={menu === "settings"}
            onClick={() => toggle("settings")}
          />
          </div>
          <button
            type="button"
            aria-label="Zoom"
            title={view ? `Zoom ${view.minZoom}% to ${view.maxZoom}%` : undefined}
            aria-haspopup="menu"
            aria-expanded={menu === "zoom"}
            onClick={() => toggle("zoom")}
            className="flex h-7 shrink-0 items-center gap-0.5 rounded-md px-1.5 text-[13px] hover:bg-[var(--cs-hover)] aria-expanded:bg-[var(--cs-active)]"
          >
            <span data-testid="zoom-level">{view?.zoom ?? 100}%</span>
            <ChevronDown className="size-3.5 text-[var(--cs-muted)]" />
          </button>
        </div>

        {menu && menu !== "changes" && menu !== "section-component" && (
          <div className="pointer-events-auto absolute right-3 top-12">
            {menu === "export" && build && (
              <ExportPanel sessionId={sessionId} build={build} unit={unit} onClose={() => setMenu(null)} />
            )}
            {menu === "checks" && build && (
              <ConstraintsPanel
                // Above the check card its places open, as a menu is (chromeLayout: menus z-40).
                className="absolute right-0 top-full z-40 mt-1.5"
                report={report}
                against={report?.board ? `Against ${report.board.name} ${revisionLabel(report)}` : null}
                error={checksError}
                run={checksRun}
                onRun={() => {
                  setChecks(null);
                  setChecksRun((n) => n + 1);
                }}
                unit={unit}
                selectedId={selectedCheck}
                onSelect={(r) => {
                  setSelectedCheck(r.id);
                  const loc = checkLocation(r);
                  viewer()?.focus(loc.point, undefined, model && locationPart(loc, model.index));
                }}
                pinFilter={pinFilter}
                onPinFilter={choosePinFilter}
                reveal={revealChecks}
              />
            )}
            {menu === "code" && build && (
              <CodePanel
                sessionId={sessionId}
                build={build}
                source={code.source}
                error={code.error}
                feature={codeFeature}
                draft={codeDraft?.buildId === build.id ? codeDraft : null}
                onDraft={setCodeDraft}
                // The canvas follows the rebuild, as after any hand edit.
                onRebuilt={() => setSelectedId(null)}
              />
            )}
            {menu === "layout" && (
              <div role="menu" aria-label="Layout" data-pin-cover="" className={`${panel} right-0 w-44`}>
                {LAYOUTS.map((l) => (
                  <button
                    key={l}
                    type="button"
                    role="menuitemradio"
                    aria-checked={layout === l}
                    onClick={() => {
                      setLayout(l);
                      setMenu(null);
                    }}
                    className={`${menuItem} text-[var(--cs-muted)]`}
                  >
                    <Check className={`size-3.5 ${layout === l ? "" : "invisible"}`} />
                    <span className="text-[var(--cs-text)]">{LAYOUT_LABELS[l]}</span>
                  </button>
                ))}
              </div>
            )}
            {menu === "settings" && <SettingsPanel settings={settings} onChange={setSettings} unit={unit} onUnit={setUnit} />}
            {menu === "zoom" && (
              <div role="menu" aria-label="Zoom" data-pin-cover="" className={`${panel} right-0 w-44`}>
                <MenuAction label="Zoom in" onClick={() => viewer()?.zoomBy(ZOOM_STEP)} />
                <MenuAction label="Zoom out" onClick={() => viewer()?.zoomBy(1 / ZOOM_STEP)} />
                <MenuAction
                  label="Zoom to fit"
                  onClick={() => {
                    viewer()?.fit();
                    setMenu(null);
                  }}
                />
                <div className="my-1 border-t border-[var(--cs-border)]" />
                {ZOOM_PRESETS.filter((p) => p >= (view?.minZoom ?? 0) && p <= (view?.maxZoom ?? MAX_ZOOM)).map((p) => (
                  <MenuAction
                    key={p}
                    label={`${p}%`}
                    onClick={() => {
                      viewer()?.setZoom(p);
                      setMenu(null);
                    }}
                  />
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      {dimensionsOn && hasModel && view && (
        <Dimensions
          parameters={parameters}
          unit={unit}
          project={(p) => viewerRef.current?.project(p) ?? null}
          layout={() => canvasLayout(viewportRef.current)}
          hidden={(part) => partView(partViews, part).hidden}
          dragging={dragging}
          onEdited={() => setParamsOpen(true)}
          bounds={layout === "assembled" ? model?.bounds : null}
        />
      )}

      {sheetOpen && <ShortcutSheet available={hasShortcut} onClose={() => setSheetOpen(false)} />}
      {/* The left column: the parts panel, then the view label and the model's parts and size. It
          starts under the top bar; the bar's left end is empty, so on a canvas wide enough to clear
          the units switch and the toolbar it starts at the top. */}
      <div className="pointer-events-none absolute bottom-[var(--chrome-bottom)] left-3 top-[var(--chrome-top)] flex w-56 flex-col gap-2 @min-[44rem]:top-3">
      {loadState === "ready" && model && (
        <PartsPanel
          tree={featureTree(model.index, code.source?.features ?? null, params, model.materials)}
          highlighted={pickedFeatures(model.index, picks)}
          unit={unit}
          onShowCode={(feature) => {
            setCodeFeature(feature);
            setMenu("code");
          }}
          parts={model.parts}
          boardParts={boardParts}
          views={partViews}
          onViews={setPartViews}
          picks={picks}
          onSelect={(next) => select(next)}
        />
      )}
        <div className="mt-auto flex min-w-0 shrink-0 flex-col gap-0.5 text-[12px] text-[var(--cs-muted)]">
          {hasModel && view && (
            <>
              <AxisTriad rotation={view.rotation} />
              <p data-testid="view-label" className="mb-1 truncate">
                View: <b className="font-medium text-[var(--cs-text)]">{view.label}</b>
                {hoverTarget && <span> · click for {hoverTarget.label}</span>}
              </p>
            </>
          )}
          {loadState === "loading" && <span>Loading the model…</span>}
          {loadState === "error" && <span className="text-[var(--cs-accent)]">The model could not be loaded.</span>}
          {loadState === "ready" && model && (
            <>
              <span data-testid="model-parts" title={model.parts.join(", ")} className="truncate">
                {model.parts.join(", ")}
              </span>
              <span data-testid="model-size" className="truncate">
                {formatSize(model.size, unit)}
              </span>
            </>
          )}
        </div>
      </div>
      {/* The tool panels: in the tool column (chromeLayout.ts), over the view cube. */}
      {paramsOpen && (
        <div className={PLACE.tools}>
          <div data-canvas-panel data-pin-cover="" className={PANEL}>
            <ParametersPanel parameters={parameters} unit={unit} readOnly={readOnly} />
          </div>
        </div>
      )}

      {/* The view cube, directly under the toolbar with its right edge aligned. */}
      {hasModel && view && (
        <div data-canvas-panel data-pin-cover="" className="absolute right-3 top-[52px]">
          <ViewCube
            rotation={view.rotation}
            view={view.view}
            onSnap={(key) => {
              viewer()?.snapTo(key);
              reportAction(sessionId, { kind: "view", view: "camera", value: key });
            }}
            onTurn={(d) => viewer()?.turn(d)}
            onRoll={(d) => viewer()?.roll(d)}
            onHome={() => {
              viewer()?.home();
              reportAction(sessionId, { kind: "view", view: "camera", value: "home" });
            }}
            onOrbit={(dx, dy) => viewer()?.orbitBy(dx, dy)}
            onHover={setHoverTarget}
          />
        </div>
      )}

      {/* The section panel: stacked up from the bottom bar, between
          the parts panel and the right edge, below the view cube (chromeLayout.ts). */}
      {hasModel && sectionOn && (
        <div className={PLACE.inspection}>
        <div data-canvas-panel data-pin-cover="" className="pointer-events-none flex max-h-full w-72 max-w-full flex-col items-stretch gap-2">
          <div className="pointer-events-none flex min-h-0 flex-col gap-2">
            <SectionPanel
              targets={targets}
              boardFailed={boardFailed}
              current={sectionTarget}
              onComponent={(ref) => {
                setSectionRef(ref);
                setMenu(null);
              }}
              axis={sectionAxis}
              onAxis={setSectionAxis}
              section={section}
              clearance={sectionClearance}
              unit={unit}
              menuOpen={menu === "section-component"}
              onMenu={() => toggle("section-component")}
            />
          </div>
        </div>
        </div>
      )}

      {loadState === "ready" && report && (
        <CheckOverlay
          report={pinReport ?? report}
          unit={unit}
          selectedId={selectedCheck}
          onSelect={(r) => {
            setSelectedCheck(r?.id ?? null);
            const loc = r && checkLocation(r);
            if (loc) viewer()?.focus(loc.point, undefined, model && locationPart(loc, model.index));
          }}
          // The card follows its part in the exploded view.
          toScreen={(loc) => viewerRef.current?.toScreen(loc.point, model && locationPart(loc, model.index)) ?? null}
          width={view?.width ?? 0}
        />
      )}

      {changeSet && (
        <ChangePins
          changeSet={changeSet}
          // Only on the build the changes apply to, once it has loaded.
          report={loadState === "ready" && build?.id === changeSet.build ? report : null}
          unit={unit}
          selectedId={selectedChange}
          onSelect={(c, at) => {
            setSelectedChange(c.id);
            setMenu("changes");
            viewer()?.focus(at);
          }}
          toScreen={(p) => viewerRef.current?.toScreen(p) ?? null}
        />
      )}

      {question && onAnswer && loadState === "ready" && view?.marker && (
        <QuestionPin
          key={question.questionId}
          question={question}
          at={view.marker}
          size={[view.width, view.height]}
          busy={fixDisabled}
          onAnswer={onAnswer}
        />
      )}

      {/* Labels of the dimension lines, at each line's middle. */}
      {hasModel &&
        view?.dimensions.map(
          (d, i) =>
            d.at && (
              <span
                key={i}
                data-testid="dimension-label"
                className="pointer-events-none absolute -translate-y-1/2 translate-x-2 rounded bg-[#1f2a3a]/90 px-1.5 py-0.5 text-[12px] text-[#9cc4ff]"
                style={{ left: d.at[0], top: d.at[1] }}
              >
                {d.label}
              </span>
            ),
        )}

      {/* The bottom bar: the comment tools. Layouts and section are in the toolbar. */}
      <div className="pointer-events-none absolute inset-x-3 bottom-[var(--chrome-bar)] flex min-w-0 items-center justify-end gap-2">
        <div ref={setCommentSlot} className="flex shrink-0 empty:hidden" />
      </div>

      {build && (
        <CommentsLayer
          sessionId={sessionId}
          build={build}
          ready={hasModel}
          tool={commentTool}
          toScreen={(p) => viewerRef.current?.toScreen(p) ?? null}
          width={view?.width ?? 0}
          height={view?.height ?? 0}
          changeSet={changeSet}
          onReviewed={(set) => {
            setChangeSet(set);
            setSelectedId(null); // the canvas follows the rebuild
          }}
          onSend={onSendComments}
          onAnswer={onAnswerInThread}
          busy={fixDisabled}
          blocked={fixBlocked}
          onFocus={(p) => viewer()?.focus(p)}
          toolbarSlot={commentSlot}
        />
      )}

    </div>
  );
}

const PIN_CARD_WIDTH = 320;
const PIN_MARGIN = 8;
const PIN_TOP = 56; // below the top control bar

const PIN_GAP = 20; // between the spot and the card, clear of the marker
const PIN_MIN_HEIGHT = 160;

/**
 * Where the pin's card goes, inside the canvas and clear of the marker: beside the spot where
 * there is room, otherwise below it, or above it when below is too short.
 */
export function pinCardBox(at: [number, number], size: [number, number]) {
  const [w, h] = size;
  const width = Math.max(0, Math.min(PIN_CARD_WIDTH, w - 2 * PIN_MARGIN));
  const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(v, Math.max(lo, hi)));
  // A spot past the canvas edge (panned away) places the card as if it were at the edge.
  const [x, y] = [clamp(at[0], 0, w), clamp(at[1], 0, h)];
  const besideTop = clamp(y - 24, PIN_TOP, h - PIN_MIN_HEIGHT - PIN_MARGIN);
  const beside = (left: number) => ({ left, top: besideTop, width, maxHeight: Math.max(PIN_MIN_HEIGHT, h - besideTop - PIN_MARGIN) });
  if (w - x - PIN_GAP - PIN_MARGIN >= width) return beside(x + PIN_GAP);
  if (x - PIN_GAP - PIN_MARGIN >= width) return beside(x - PIN_GAP - width);
  const left = clamp(x - width / 2, PIN_MARGIN, w - width - PIN_MARGIN);
  const below = Math.max(PIN_TOP, y + PIN_GAP);
  if (h - below - PIN_MARGIN >= PIN_MIN_HEIGHT || y - PIN_GAP - PIN_TOP < h - below - PIN_MARGIN) {
    return { left, top: below, width, maxHeight: Math.max(0, h - below - PIN_MARGIN) };
  }
  return { left, top: PIN_TOP, width, maxHeight: Math.max(0, y - PIN_GAP - PIN_TOP) };
}

/**
 * Claude's question pinned at its spot: a marker, and a card beside it with the question, the
 * conflict behind it and the options. The marker opens and closes the card.
 */
function QuestionPin({
  question,
  at,
  size,
  busy,
  onAnswer,
}: {
  question: PinnedQuestion;
  /** The spot on screen, in CSS pixels from the canvas's top left. */
  at: [number, number];
  /** The canvas's size in CSS pixels. */
  size: [number, number];
  busy: boolean;
  onAnswer: (questionId: string, option: QuestionOption) => void;
}) {
  const [open, setOpen] = useState(true);
  return (
    <>
      <div
        data-testid="question-pin"
        data-point={question.location.point.join(",")}
        data-x={at[0]}
        data-y={at[1]}
        className="pointer-events-none absolute z-10"
        style={{ left: at[0], top: at[1] }}
      >
        <span
          aria-hidden
          className="absolute size-14 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-dashed border-[var(--cs-accent)] bg-[var(--cs-accent)]/20"
        />
        <button
          type="button"
          aria-label="Claude's question"
          title="Claude's question"
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
          className="pointer-events-auto absolute grid size-6 -translate-x-1/2 -translate-y-1/2 place-items-center rounded-full bg-[var(--cs-accent)] text-[13px] font-semibold text-white shadow-md"
        >
          ?
        </button>
      </div>
      {open && (
        <section
          aria-label="Question on the model"
          data-testid="question-pin-card"
          style={pinCardBox(at, size)}
          className="absolute z-10 flex flex-col gap-2 overflow-y-auto rounded-xl border border-[var(--cs-border)] bg-[#262626] p-3 text-[13px] shadow-xl"
        >
          <p className="font-[500]">{question.text}</p>
          {question.conflict && <ConflictCard conflict={question.conflict} compact />}
          <QuestionOptions question={question} busy={busy} onAnswer={onAnswer} />
        </section>
      )}
    </>
  );
}

/**
 * The 3D view's size and where the panels over it are (the parts panel, the parameters panel,
 * the view cube, a map legend), in CSS pixels relative to the view, so dimension labels can
 * keep out of them.
 */
function canvasLayout(viewport: HTMLElement | null): { size: [number, number]; panels: Rect[] } | null {
  const root = viewport?.parentElement;
  if (!viewport || !root) return null;
  const origin = viewport.getBoundingClientRect();
  const panels = Array.from(root.querySelectorAll("[data-canvas-panel]"), (el) => {
    const r = el.getBoundingClientRect();
    return { x: r.left - origin.left, y: r.top - origin.top, width: r.width, height: r.height };
  }).filter((r) => r.width > 0 && r.height > 0);
  return { size: [origin.width, origin.height], panels: [...panels, ...outsideBand(root, origin)] };
}

function MenuButton({
  label,
  icon: Icon,
  open,
  onClick,
}: {
  label: string;
  icon: typeof Download;
  open: boolean;
  onClick: () => void;
}) {
  return (
    <button type="button" aria-label={label} title={label} aria-expanded={open} onClick={onClick} className={iconBtn}>
      <Icon className="size-3.5" />
    </button>
  );
}

/**
 * The section cut: what it goes through (a part of the design, or a board component when the
 * build has a board), the axis it is normal to, where it is, and a component's clearance. A
 * board that could not be loaded is said so in a line; the section works without it.
 */
function SectionPanel({
  targets,
  boardFailed,
  current,
  onComponent,
  axis,
  onAxis,
  section,
  clearance,
  unit,
  menuOpen,
  onMenu,
}: {
  targets: SectionTarget[];
  boardFailed: boolean;
  current: SectionTarget | null;
  onComponent: (id: string) => void;
  axis: Axis;
  onAxis: (a: Axis) => void;
  section: Section | null;
  clearance: Clearance | null;
  unit: LengthUnit;
  menuOpen: boolean;
  onMenu: () => void;
}) {
  return (
    <section
      aria-label="Section"
      className="pointer-events-auto relative flex shrink-0 flex-col gap-2 rounded-xl border border-[var(--cs-border)] bg-[#262626]/95 p-2.5 text-[12px] shadow-xl"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[var(--cs-muted)]">Through</span>
        <div data-menu-host className="relative min-w-0">
          <button
            type="button"
            aria-label="Section through"
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            disabled={!targets.length}
            onClick={onMenu}
            className={`${pill} h-7 max-w-full hover:bg-[var(--cs-surface)]`}
          >
            <span className="truncate">{current?.label ?? "—"}</span>
            <ChevronDown className="size-3.5 shrink-0 text-[var(--cs-muted)]" />
          </button>
          {menuOpen && (
            <div
              role="menu"
              aria-label="Section through"
              className="cs-scroll absolute bottom-full left-0 z-20 mb-1.5 max-h-72 w-56 overflow-y-auto rounded-xl border border-[var(--cs-border)] bg-[#262626] p-1.5 text-[13px] shadow-xl"
            >
              {targets.map((t) => (
                <button
                  key={t.id}
                  type="button"
                  role="menuitemradio"
                  aria-checked={t.id === current?.id}
                  onClick={() => onComponent(t.id)}
                  className={`${menuItem} text-[var(--cs-muted)]`}
                >
                  <Check className={`size-3.5 shrink-0 ${t.id === current?.id ? "" : "invisible"}`} />
                  <span className="truncate text-[var(--cs-text)]">{t.label}</span>
                </button>
              ))}
            </div>
          )}
        </div>
        <div role="radiogroup" aria-label="Section axis" className="flex rounded-lg bg-[var(--cs-surface-2)] p-0.5">
          {AXES.map((a) => (
            <button
              key={a}
              type="button"
              role="radio"
              aria-checked={axis === a}
              onClick={() => onAxis(a)}
              className={`h-6 w-7 rounded-md ${axis === a ? "bg-[var(--cs-surface)] text-[var(--cs-text)]" : "text-[var(--cs-muted)] hover:text-[var(--cs-text)]"}`}
            >
              {a.toUpperCase()}
            </button>
          ))}
        </div>
      </div>
      {section && (
        <p data-testid="section-plane" className="text-[var(--cs-muted)]">
          Section at {section.axis} = {formatLength(section.at, unit)}
        </p>
      )}
      {clearance && (
        <p
          data-testid="section-clearance"
          data-value={clearance.distance.toFixed(3)}
          data-part={clearance.part}
          className="text-[var(--cs-text)]"
        >
          Clearance {formatPrecise(clearance.distance, unit)} from {clearance.ref} to the {clearance.part}
        </p>
      )}
      {boardFailed && (
        <p data-testid="section-board-error" className="text-[var(--cs-muted)]">
          Couldn&apos;t load the board, so it isn&apos;t shown in the section.
        </p>
      )}
    </section>
  );
}

/** A length to 0.01 mm (or 0.001 in): clearances are compared with check values. */
function formatPrecise(mm: number, unit: LengthUnit) {
  return `${fromMm(mm, unit).toFixed(unit === "in" ? 4 : 2)} ${unit}`;
}

function MenuAction({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <button type="button" role="menuitem" onClick={onClick} className={menuItem}>
      {label}
    </button>
  );
}

/** Saves a file through the browser's download, as a click on a link to it would. */
function saveFile(url: string, name: string) {
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.hidden = true;
  document.body.append(a);
  a.click();
  a.remove();
}

/**
 * The export menu: a checkbox per format the exporters write, "Include build note", and
 * Download, which exports the build in the checked formats and downloads every file. The files
 * stay listed as links, to download one again.
 */
function ExportPanel({
  sessionId,
  build,
  unit,
  onClose,
}: {
  sessionId: string;
  build: Build;
  unit: LengthUnit;
  onClose: () => void;
}) {
  const [selected, setSelected] = useState<FileFormat[]>(DEFAULT_FORMATS);
  const [includeNote, setIncludeNote] = useState(true);
  const [result, setResult] = useState<Export | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const formats = requestedFormats(selected, includeNote);
  const count = fileCount(formats);
  const toggleFormat = (format: FileFormat, on: boolean) =>
    setSelected((cur) => (on ? [...cur, format] : cur.filter((f) => f !== format)));
  const download = () => {
    setBusy(true);
    setError(null);
    createExport(sessionId, build.id, formats, unit)
      .then((exported) => {
        setResult(exported);
        for (const f of exported.files) saveFile(f.url, f.name);
      })
      .catch((e: Error) => setError(shownError(e)))
      .finally(() => setBusy(false));
  };
  const why = busy ? "Exporting the files" : count === 0 ? "Choose at least one format" : undefined;
  return (
    <section
      aria-label="Export"
      data-formats={formats.join(",")}
      data-units={unit}
      data-pin-cover=""
      className={`${panel} right-0 w-[22rem] p-1.5`}
    >
      <h2 className="px-2 pb-1.5 pt-1 text-[12px] text-[var(--cs-muted)]">Export {revisionLabel(build)}</h2>
      <fieldset className="border-t border-[var(--cs-border)]">
        <legend className="sr-only">Formats</legend>
        {FORMAT_OPTIONS.map((o) => (
          <label
            key={o.format}
            className="flex cursor-pointer items-center gap-2.5 border-b border-[var(--cs-border)] px-2 py-2 hover:bg-[var(--cs-hover)]"
          >
            <input
              type="checkbox"
              checked={selected.includes(o.format)}
              onChange={(e) => toggleFormat(o.format, e.target.checked)}
              className="accent-[var(--cs-text)]"
            />
            <span className="text-[var(--cs-text)]">{o.label}</span>
            <span aria-hidden className="ml-auto text-[12px] text-[var(--cs-muted)]">
              {o.purpose}
            </span>
          </label>
        ))}
      </fieldset>
      <label className="flex cursor-pointer items-center gap-2.5 px-2 pt-2.5 hover:text-[var(--cs-text)]">
        <input
          type="checkbox"
          checked={includeNote}
          onChange={(e) => setIncludeNote(e.target.checked)}
          aria-describedby="export-note-summary"
          className="accent-[var(--cs-text)]"
        />
        Include build note
      </label>
      <p id="export-note-summary" data-testid="export-note-summary" className="px-2 pl-[2.1rem] text-[12px] text-[var(--cs-muted)]">
        {noteSummary(build.checks)}
        {unit === "in" && <span data-testid="export-note-units"> · lengths in inches; the model files stay in mm</span>}
      </p>
      <div className="flex justify-end gap-2 px-2 pb-1 pt-3">
        <button
          type="button"
          onClick={onClose}
          className="h-8 rounded-lg border border-[var(--cs-border)] px-3 text-[13px] hover:bg-[var(--cs-hover)]"
        >
          Cancel
        </button>
        <button
          type="button"
          onClick={download}
          disabled={!!why}
          title={why}
          className="h-8 rounded-lg bg-[var(--cs-text)] px-3 text-[13px] font-medium text-[#1f1f1f] disabled:cursor-not-allowed disabled:opacity-50"
        >
          {busy ? "Exporting…" : downloadLabel(count)}
        </button>
      </div>
      {error && <p className="px-2 pb-1 text-[var(--cs-accent)]">{error}</p>}
      {result && (
        <ul aria-label="Exported files" className="mx-2 mb-1 mt-1 flex flex-col gap-1 border-t border-[var(--cs-border)] pt-2">
          {result.files.map((f) => (
            <li key={f.name}>
              <a href={f.url} download={f.name} className="text-[var(--cs-link)] hover:underline">
                {f.name}
              </a>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/**
 * The build's CAD code, editable: Rebuild (or Ctrl/Cmd+Enter) sends the edit to the API, which
 * rebuilds and rechecks it, and Claude gets the change on its next turn. Code that doesn't parse
 * or build is refused with its line, shown inline; the edit stays in the editor to fix. Opened
 * from a feature in the tree, it marks and scrolls to the lines that create that feature.
 */
function CodePanel({
  sessionId,
  build,
  source,
  error,
  feature,
  draft,
  onDraft,
  onRebuilt,
}: {
  sessionId: string;
  build: Build;
  source: BuildSource | null;
  error: string | null;
  feature: FeatureNode | null;
  draft: CodeDraft | null;
  onDraft: (draft: CodeDraft | null) => void;
  onRebuilt: (build: Build) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [rebuilt, setRebuilt] = useState<Build | null>(null);
  const editorRef = useRef<HTMLTextAreaElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const code = draft?.code ?? source?.code ?? "";
  const edited = !!source && draft !== null && draft.code !== source.code;
  const codeError = draft?.error ?? null;
  const codeLines = code.split("\n");
  // A feature's lines are those of the build's code: an edit can move them, so they show only
  // while the code is the build's.
  const lines = !edited ? (feature?.lines ?? null) : null;
  const marked = (n: number) => !!lines && n >= lines[0] && n <= lines[1];
  useEffect(() => {
    if (!lines || !source) return;
    const box = scrollRef.current;
    const first = box?.querySelector<HTMLElement>(`[data-line="${lines[0]}"]`);
    // Scroll only the code box, so the canvas never moves.
    if (first && box) box.scrollTop += first.getBoundingClientRect().top - box.getBoundingClientRect().top - box.clientHeight / 3;
  }, [lines, source]);

  const rebuild = () => {
    if (!source || !edited || busy) return;
    const sent = code;
    setBusy(true);
    setRebuilt(null);
    updateSource(sessionId, build.id, sent)
      .then((next) => {
        onDraft(null);
        setRebuilt(next);
        onRebuilt(next);
      })
      .catch((e: Error) => {
        const line = e instanceof CodeEditError ? e.line : null;
        onDraft({ buildId: build.id, code: sent, error: { message: e instanceof CodeEditError ? e.message : shownError(e), line } });
        if (line) selectLine(editorRef.current, scrollRef.current, line);
      })
      .finally(() => setBusy(false));
  };
  const why = busy ? "Rebuilding" : !edited ? "Edit the code to rebuild" : undefined;

  return (
    <section
      aria-label="Code"
      // Leaves the parts panel (left) uncovered, so its line links stay in reach.
      data-pin-cover=""
      className={`${panel} right-0 w-[max(15rem,min(28rem,calc(100cqw-15rem)))] p-3`}
    >
      {!source && !error && <p className="text-[var(--cs-muted)]">Loading the code…</p>}
      {error && <p className="text-[var(--cs-accent)]">{error}</p>}
      {source && (
        <>
          <p data-testid="code-file" className="mb-2 text-[var(--cs-muted)]">
            {source.filename} · {source.backend} · {revisionLabel(build)}
          </p>
          {feature && lines && (
            <p data-testid="code-feature" className="mb-2 text-[var(--cs-muted)]">
              <span className="text-[var(--cs-text)]">{feature.tag}</span> · {linesLabel(lines)}
            </p>
          )}
          {/* The editor is as tall and wide as the code, so it scrolls with its line numbers. */}
          <div
            ref={scrollRef}
            className="cs-scroll flex max-h-96 items-start overflow-auto rounded-md bg-[#1b1b1b] font-mono text-[11px] leading-4"
          >
            <div aria-hidden className="sticky left-0 z-10 select-none bg-[#1b1b1b] py-2 pl-2 pr-2 text-right text-[var(--cs-faint)]">
              {codeLines.map((_, i) => (
                <div
                  key={i}
                  data-error-line={codeError?.line === i + 1 ? "true" : undefined}
                  className={codeError?.line === i + 1 ? "rounded-sm bg-[var(--cs-accent)] px-0.5 text-[#1b1b1b]" : "px-0.5"}
                >
                  {i + 1}
                </div>
              ))}
            </div>
            <div data-testid="code-source" data-build={source.build_id} data-lines={lines ? lines.join("-") : ""} className="relative shrink-0">
              {/* Under the editor, line for line: the marked feature's lines. */}
              <pre aria-hidden className="pointer-events-none absolute inset-0 m-0 whitespace-pre py-2 pr-2 text-transparent">
                <code className={`language-${source.language}`}>
                  {codeLines.map((line, i, all) => (
                    <Fragment key={i}>
                      <span
                        data-line={i + 1}
                        data-marked={marked(i + 1) || undefined}
                        className={`inline-block min-h-4 min-w-full ${marked(i + 1) ? "bg-[var(--cs-accent)]/25" : ""}`}
                      >
                        {line}
                      </span>
                      {i < all.length - 1 && "\n"}
                    </Fragment>
                  ))}
                </code>
              </pre>
              <textarea
                ref={editorRef}
                aria-label="Generator code"
                aria-invalid={!!codeError}
                aria-describedby={codeError ? "code-error" : undefined}
                data-build={source.build_id}
                data-edited={edited ? "true" : "false"}
                spellCheck={false}
                wrap="off"
                rows={codeLines.length}
                cols={Math.max(...codeLines.map((l) => l.length), 40)}
                value={code}
                onChange={(e) => onDraft({ buildId: build.id, code: e.target.value })}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                    e.preventDefault();
                    rebuild();
                  }
                }}
                className="relative block resize-none overflow-hidden whitespace-pre bg-transparent py-2 pr-2 leading-4 outline-none focus:ring-1 focus:ring-inset focus:ring-[var(--cs-accent)]"
              />
            </div>
          </div>
          {codeError && (
            <p id="code-error" role="alert" data-testid="code-error" className="mt-2 text-[var(--cs-accent)]">
              {codeError.line ? `Line ${codeError.line}: ` : ""}
              {codeError.message} Your edit is kept: fix it and rebuild.
            </p>
          )}
          {rebuilt && !edited && (
            <p role="status" data-testid="code-rebuilt" className="mt-2 text-[var(--cs-muted)]">
              Rebuilt and checked: {rebuilt.checks.pass} pass · {rebuilt.checks.fail} fail · {rebuilt.checks.untested}{" "}
              untested. Claude sees your edit on its next turn.
            </p>
          )}
          <div className="mt-2 flex items-center justify-end gap-2">
            {edited && (
              <button
                type="button"
                onClick={() => onDraft(null)}
                className="h-7 rounded-md px-2 text-[var(--cs-muted)] hover:bg-[var(--cs-hover)] hover:text-[var(--cs-text)]"
              >
                Discard edits
              </button>
            )}
            <button
              type="button"
              onClick={rebuild}
              disabled={!!why}
              title={why ?? "Rebuild and recheck with this code (Ctrl+Enter)"}
              className="h-7 rounded-md bg-[var(--cs-text)] px-2.5 font-medium text-[#1f1f1f] disabled:cursor-not-allowed disabled:opacity-50"
            >
              {busy ? "Rebuilding…" : "Rebuild"}
            </button>
          </div>
        </>
      )}
    </section>
  );
}

/** Selects a 1-based line of the code editor and scrolls the editor's box to it. */
function selectLine(el: HTMLTextAreaElement | null, box: HTMLElement | null, line: number) {
  if (!el) return;
  const lines = el.value.split("\n");
  const start = lines.slice(0, line - 1).reduce((n, l) => n + l.length + 1, 0);
  el.focus({ preventScroll: true });
  el.setSelectionRange(start, start + (lines[line - 1]?.length ?? 0));
  const lineHeight = parseFloat(getComputedStyle(el).lineHeight) || 16;
  box?.scrollTo({ top: Math.max(0, (line - 3) * lineHeight) });
}

function SettingsPanel({
  settings,
  onChange,
  unit,
  onUnit,
}: {
  settings: ViewSettings;
  onChange: (s: ViewSettings) => void;
  unit: LengthUnit;
  onUnit: (u: LengthUnit) => void;
}) {
  const options: { key: keyof ViewSettings; label: string }[] = [
    { key: "edges", label: "Edges" },
    { key: "grid", label: "Grid" },
  ];
  return (
    <section aria-label="View settings" data-pin-cover="" className={`${panel} right-0 w-48`}>
      <div role="radiogroup" aria-label="Units" className="flex items-center gap-2 px-2.5 py-1.5">
        <span className="flex-1 text-[var(--cs-muted)]">Units</span>
        <div className="flex rounded-lg bg-[var(--cs-surface-2)] p-0.5 text-[13px]">
          {LENGTH_UNITS.map((u) => (
            <button
              key={u}
              type="button"
              role="radio"
              aria-checked={unit === u}
              onClick={() => onUnit(u)}
              className={`h-6 rounded-md px-2 ${unit === u ? "bg-[var(--cs-surface)] text-[var(--cs-text)]" : "text-[var(--cs-muted)] hover:text-[var(--cs-text)]"}`}
            >
              {u}
            </button>
          ))}
        </div>
      </div>
      <div className="my-1 border-t border-[var(--cs-border)]" />
      {options.map((o) => (
        <label key={o.key} className={`${menuItem} cursor-pointer`}>
          <input
            type="checkbox"
            checked={settings[o.key]}
            onChange={(e) => onChange({ ...settings, [o.key]: e.target.checked })}
            className="accent-[var(--cs-accent)]"
          />
          {o.label}
        </label>
      ))}
    </section>
  );
}

/** A map value as display text: lengths follow the mm/in toggle, angles are degrees. */
export function formatMapValue(value: number, unit: "mm" | "deg", lengthUnit: LengthUnit): string {
  if (unit === "deg") return `${value.toFixed(1)}°`;
  return `${fromMm(value, lengthUnit).toFixed(lengthUnit === "in" ? 3 : 1)} ${lengthUnit}`;
}

/**
 * The feature tree, which is also the parts tree: per part its material, select, hide, isolate and
 * see-through, with the part's features nested under it. A feature shows the parameters that
 * drive it; clicking it selects its faces, and its line link opens the code that creates it.
 * A feature with a picked face or edge is highlighted. The board revision follows the
 * enclosure's parts: the board, with its components (by reference designator) listed under it.
 */
function PartsPanel({
  tree,
  highlighted,
  unit,
  onShowCode,
  parts,
  boardParts,
  views,
  onViews,
  picks,
  onSelect,
}: {
  tree: PartNode[];
  highlighted: Set<string>;
  unit: LengthUnit;
  onShowCode: (feature: FeatureNode) => void;
  /** The enclosure's parts. */
  parts: string[];
  /** The board's parts: the board, then its components. */
  boardParts: string[];
  views: PartViews;
  onViews: (v: PartViews) => void;
  picks: Picked[];
  onSelect: (picks: Picked[]) => void;
}) {
  const listRef = useRef<HTMLUListElement>(null);
  const all = [...parts, ...boardParts];
  const anyHidden = all.some((p) => partView(views, p).hidden);

  // Picking a face on the model brings its feature into view in the tree.
  const highlightKey = [...highlighted].join(",");
  useEffect(() => {
    const list = listRef.current;
    const row = list?.querySelector<HTMLElement>("[data-highlighted=true]");
    if (!list || !row) return;
    const top = row.offsetTop - list.offsetTop;
    if (top < list.scrollTop || top + row.offsetHeight > list.scrollTop + list.clientHeight)
      list.scrollTop = top - list.clientHeight / 3;
  }, [highlightKey]);

  return (
    <section
      aria-label="Parts"
      data-canvas-panel
      data-pin-cover=""
      className="cs-scroll pointer-events-auto min-h-0 w-56 shrink overflow-y-auto rounded-xl border border-[var(--cs-border)] bg-[#262626] p-1.5 text-[12px]"
    >
      <ul ref={listRef} aria-label="Part list" className="cs-scroll relative mt-1.5 flex max-h-[55vh] flex-col overflow-y-auto">
        {[...parts, ...tree.map((n) => n.part).filter((p) => !parts.includes(p))].map((part) => {
          const v = partView(views, part);
          const selected = picks.some((p) => p.kind === "part" && p.part === part);
          const alone = isIsolated(views, all, part);
          const node = tree.find((n) => n.part === part);
          const label = (
            <>
              <span className="truncate">{part}</span>
              {node?.material && (
                <span data-testid="part-material" className="truncate text-[11px] text-[var(--cs-muted)]">
                  {node.material}
                </span>
              )}
            </>
          );
          const features = node && node.features.length > 0 && (
            <ul aria-label={`Features of ${part}`} className="mb-1 ml-2.5 flex flex-col border-l border-[var(--cs-border)] pl-1">
              {node.features.map((f) => (
                <FeatureRow
                  key={f.tag}
                  feature={f}
                  highlighted={highlighted.has(featureKey(f.part, f.tag))}
                  dimmed={v.hidden}
                  unit={unit}
                  onSelect={() => onSelect(featurePicks(f))}
                  onShowCode={() => onShowCode(f)}
                />
              ))}
            </ul>
          );
          // A part the code makes that the model has no mesh for: listed with its features,
          // but there is nothing on the canvas to select, hide or see through.
          if (!parts.includes(part))
            return (
              <li key={part} data-part={part}>
                <div
                  title="Not on the canvas: this model has no mesh for it"
                  className="flex h-6 min-w-0 items-baseline gap-1.5 rounded-md px-1 leading-6"
                >
                  {label}
                </div>
                {features}
              </li>
            );
          return (
            <li key={part} data-part={part}>
              <PartRow
                part={part}
                label={label}
                view={v}
                selected={selected}
                alone={alone}
                onSelect={() => onSelect([{ kind: "part", id: part, part }])}
                onChange={(change) => onViews({ ...views, [part]: { ...v, ...change } })}
                onIsolate={() => onViews(isolate(views, all, part))}
              />
              {features}
            </li>
          );
        })}
        <BoardPartRows names={boardParts} allParts={all} views={views} onViews={onViews} picks={picks} onSelect={onSelect} />
      </ul>
      {anyHidden && (
        <button
          type="button"
          onClick={() => onViews(showAll(views, all))}
          className="mt-1 h-6 w-full rounded-md text-[var(--cs-muted)] hover:bg-[var(--cs-hover)] hover:text-[var(--cs-text)]"
        >
          Show all
        </button>
      )}
    </section>
  );
}

/** A feature in the tree: its name (selects its faces), its line link and its parameters. */
function FeatureRow({
  feature,
  highlighted,
  dimmed,
  unit,
  onSelect,
  onShowCode,
}: {
  feature: FeatureNode;
  highlighted: boolean;
  dimmed: boolean;
  unit: LengthUnit;
  onSelect: () => void;
  onShowCode: () => void;
}) {
  const name = `${dimmed ? "text-[var(--cs-faint)]" : ""} ${highlighted ? "text-[var(--cs-accent)]" : ""}`;
  return (
    <li
      data-feature={feature.tag}
      data-faces={feature.faces.length}
      data-locked={feature.locked}
      data-highlighted={highlighted}
      className={`rounded-md px-1 ${highlighted ? "bg-[var(--cs-accent)]/15" : "hover:bg-[var(--cs-hover)]/50"}`}
    >
      <div className="flex items-center gap-1">
        {feature.faces.length > 0 ? (
          <button
            type="button"
            aria-label={`Select feature: ${feature.tag}`}
            aria-current={highlighted || undefined}
            title={`Select its ${feature.faces.length} face${feature.faces.length > 1 ? "s" : ""}`}
            onClick={onSelect}
            className={`h-5 min-w-0 flex-1 truncate text-left ${name}`}
          >
            {feature.tag}
          </button>
        ) : (
          // Created in the code but left no faces of its own on the model: nothing to select.
          <span title="No faces of its own on the model" className="h-5 min-w-0 flex-1 truncate leading-5 text-[var(--cs-muted)]">
            {feature.tag}
          </span>
        )}
        {feature.locked && (
          // Sketched by hand: a locked feature Claude keeps.
          <span role="img" aria-label={`${feature.tag} is locked`} title="Made by hand. Locked: Claude keeps it." className="shrink-0">
            <Lock className="size-3 text-[var(--cs-text)]" aria-hidden="true" />
          </span>
        )}
        {feature.lines && (
          <button
            type="button"
            aria-label={`Lines of ${feature.tag}`}
            title="Show the code that creates it"
            onClick={onShowCode}
            className="h-5 shrink-0 rounded px-1 font-mono text-[10px] text-[var(--cs-link)] hover:bg-[var(--cs-hover)] hover:underline"
          >
            {linesLabel(feature.lines)}
          </button>
        )}
      </div>
      {feature.params.length > 0 && (
        <dl aria-label={`Parameters of ${feature.tag}`} className="pb-0.5 text-[11px] text-[var(--cs-muted)]">
          {feature.params.map((p) => (
            <div key={p.name} data-param={p.name} className="flex gap-1.5">
              <dt className="truncate">{p.name}</dt>
              <dd className="ml-auto shrink-0 text-[var(--cs-text)]">{formatParameter(p, unit)}</dd>
            </div>
          ))}
        </dl>
      )}
    </li>
  );
}

/** A parameter's value as display text: lengths follow the mm/in toggle. */
export function formatParameter(p: Pick<Parameter, "value" | "unit">, unit: LengthUnit): string {
  if (typeof p.value !== "number") return p.value;
  if (p.unit === "mm") return formatMapValue(p.value, "mm", unit);
  if (p.unit === "deg") return formatMapValue(p.value, "deg", unit);
  return String(p.value);
}

/** Triangle sets end to end. */
function concatTriangles(sets: Float64Array[]): Float64Array {
  const out = new Float64Array(sets.reduce((n, t) => n + t.length, 0));
  let at = 0;
  for (const t of sets) {
    out.set(t, at);
    at += t.length;
  }
  return out;
}
