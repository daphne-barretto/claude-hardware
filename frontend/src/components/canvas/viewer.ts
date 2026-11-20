import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { dragDistance, type Point3 } from "./parameters";
import {
  componentBody,
  boardShown,
  layoutPlacements,
  sectionPlane,
  type Axis,
  type Board,
  type Layout,
  type Triangles,
  type Vec3,
} from "./inspect";
import { BOARD_PART, boardPartOf, boardPick } from "./boardParts";
import { partView, type PartIndex, type PartViews, type Picked, type PickKind } from "./selection";
import {
  currentTarget,
  ease,
  HOME_KEY,
  orbit,
  roll,
  screenUpAngle,
  SNAP_MS,
  targetByKey,
  targetOrientation,
  toQuaternion,
  turn,
  type Orientation,
  type RollDirection,
  type TurnDirection,
} from "./orientation";

/**
 * The three.js side of the 3D canvas, kept free of React so it can be driven by the UI and
 * read by tests. Meshes follow the mesh contract (contracts/README.md): millimetres, a root
 * node that turns board Z-up into three.js Y-up, one node per part with a triangle primitive
 * and an optional edge (line) primitive.
 *
 * Board axes in three.js world space: board x → x, board z → y, board y → −z. So "front"
 * (looking along +y in board coordinates) is the camera on +z.
 */

export type ViewName = "front" | "back" | "left" | "right" | "top" | "bottom" | "iso";
export type { RollDirection, TurnDirection };

export const VIEW_NAMES: ViewName[] = ["front", "back", "left", "right", "top", "bottom", "iso"];

/** The view cube target of a named view: a face, or the home corner for "iso". */
export const viewTarget = (view: ViewName) => targetByKey(view === "iso" ? HOME_KEY : view)!;

/** Direction from the target to the camera for each named view, in three.js world space. */
export function viewDirection(view: ViewName): THREE.Vector3 {
  const [x, y, z] = viewTarget(view).dir;
  return new THREE.Vector3(x, z, -y).normalize();
}

/** The named view a camera direction matches, or "custom". */
export function matchView(direction: THREE.Vector3): ViewName | "custom" {
  const dir = direction.clone().normalize();
  return VIEW_NAMES.find((v) => viewDirection(v).dot(dir) > 1 - 1e-6) ?? "custom";
}

/**
 * The view key the canvas reports: a named view ("front", "iso" for the home corner), an edge
 * or corner key ("front-right", "back-left-bottom"), or "custom" in a free orbit.
 */
export function viewKey(o: Orientation): string {
  const t = currentTarget(o);
  if (!t) return "custom";
  return t.key === HOME_KEY ? "iso" : t.key;
}

/** A press that moves less than this many CSS pixels is a click, more is a drag. */
export const CLICK_SLOP_PX = 4;

/**
 * Camera distance from the centre of a bounding sphere so the whole sphere is in view,
 * for a perspective camera with vertical field of view `fovDeg` and the given aspect ratio.
 */
export function fitDistance(radius: number, fovDeg: number, aspect: number, margin = 1.1): number {
  const vfov = THREE.MathUtils.degToRad(fovDeg);
  const hfov = 2 * Math.atan(Math.tan(vfov / 2) * aspect);
  return (radius * margin) / Math.sin(Math.min(vfov, hfov) / 2);
}

/** Zoom in stops at this percentage of the fitted distance. */
export const MAX_ZOOM = 1000;
/** Zoom out stops when the model's bounding sphere is this many pixels across. */
export const MIN_MODEL_PX = 120;
const FIT_MARGIN = 1.1;

/**
 * The smallest zoom (percent of fitted) for a viewport of `width` × `height` CSS pixels, so a
 * bounding sphere of `radius` stays at least MIN_MODEL_PX across on the viewport's smaller side.
 */
export function minZoomFor(radius: number, fovDeg: number, width: number, height: number): number {
  if (!width || !height || !radius) return 100;
  const vfov = THREE.MathUtils.degToRad(fovDeg);
  // Pixels per unit of tan(angle) from the view axis; the same horizontally and vertically.
  const pxPerTan = height / 2 / Math.tan(vfov / 2);
  const halfAngle = Math.atan(MIN_MODEL_PX / 2 / pxPerTan);
  const farthest = radius / Math.sin(halfAngle);
  const fitted = fitDistance(radius, fovDeg, width / height, FIT_MARGIN);
  return THREE.MathUtils.clamp((100 * fitted) / farthest, 1, 100);
}

/**
 * Near and far planes for a camera at distance `d` from the centre of everything drawn (a
 * sphere of radius `r`): the far plane is just past the back of the sphere, the near plane just
 * before its front, never closer than a small fraction of the radius so depth stays precise.
 */
export function clipPlanes(d: number, r: number): { near: number; far: number } {
  const far = (d + r) * 1.05;
  const near = Math.max(d - r * 1.05, r * 1e-3, 1e-3);
  return { near, far: Math.max(far, near * 2) };
}

/** The zoom `focus` uses by default: close enough to see a feature, with its surroundings. */
export const FOCUS_ZOOM = 300;

/** A point in board coordinates (Z up) in three.js world space (Y up): board (x, y, z) → (x, z, −y). */
export function boardToWorld([x, y, z]: [number, number, number]): THREE.Vector3 {
  return new THREE.Vector3(x, z, -y);
}

export type ViewerState = {
  /** 100 when the model is fitted; 200 is twice as close. */
  zoom: number;
  /** The zoom limits, in percent, rounded like `zoom` so the readout shows them at the limits. */
  minZoom: number;
  maxZoom: number;
  /** See viewKey. */
  view: string;
  /** What the view label shows: a target's label, "Home" or "Free orbit". */
  label: string;
  /** The camera's turntable angles in degrees: azimuth about Z, elevation and roll. */
  azimuth: number;
  elevation: number;
  roll: number;
  /** Where the board's Z axis points on screen, degrees clockwise from up (null looking along it). */
  upAngle: number | null;
  /** True while a snap, turn, roll or home animates. */
  animating: boolean;
  /** How far through its SNAP_MS the animation is, 0 to 1 (two decimals); null when not animating. */
  progress: number | null;
  /** The orbit centre, in board coordinates (mm). */
  target: [number, number, number];
  /** True when the whole model's bounding box is inside the view. */
  fits: boolean;
  /** Column-major 4×4 rotation of the camera's view matrix, for the view cube. */
  rotation: number[];
  /**
   * The model's on-screen bounding box in CSS pixels relative to the canvas, [x, y, width,
   * height], clipped to the canvas; null without a model.
   */
  modelRect: [number, number, number, number] | null;
  /** Viewport size in CSS pixels. */
  width: number;
  height: number;
  /**
   * Where the marker (`setMarker`) is on screen, [x, y] in CSS pixels relative to the canvas;
   * null without a marker or a model, or when the spot is behind the camera.
   */
  marker?: [number, number] | null;
  /**
   * Column-major 4×4 matrix from board coordinates (mm, assembled) to clip space, so a board
   * point's place on the canvas can be found (the inspection labels, and tests).
   */
  boardToClip: number[];
  /** The dimension lines' labels and where each line's middle is on the canvas (CSS pixels). */
  dimensions: { label: string; at: [number, number] | null }[];
};

/**
 * A point in three.js world coordinates projected to the canvas, [x, y] in CSS pixels from its
 * top left; null when it is behind the camera.
 */
export function projectToCanvas(
  world: THREE.Vector3,
  camera: THREE.PerspectiveCamera,
  width: number,
  height: number,
): [number, number] | null {
  camera.updateMatrixWorld();
  const v = world.clone().applyMatrix4(camera.matrixWorldInverse);
  if (-v.z < camera.near) return null;
  v.applyMatrix4(camera.projectionMatrix);
  return [round1(((v.x + 1) / 2) * width), round1(((1 - v.y) / 2) * height)];
}

export type ModelInfo = {
  parts: string[];
  /** Faces and edges of each part (mesh contract `node.extras`). */
  index: PartIndex[];
  /** Each part's material, where the mesh names one (`node.extras.material`). */
  materials: Record<string, string>;
  /** Overall size in board axes: x, y, z extents in mm. */
  size: [number, number, number];
  /** The assembled enclosure parts' bounding box in board coordinates (mm): `size` is max − min. */
  bounds: { min: Vec3; max: Vec3 };
  faces: number;
  edges: number;
};

export type ViewSettings = { edges: boolean; grid: boolean };


const PART_COLORS = [0x8c8a86, 0xa9a6a0, 0x7b8796, 0x9a8a7a];
const HIGHLIGHT = 0xd97757;
const TRANSPARENT_OPACITY = 0.25;
/** The cut faces of a section. */
const SECTION_COLOR = 0xb07a52;
const PCB_COLOR = 0x2f6b4a;
const COMPONENT_COLOR = 0x2b2b2b;
/** The component a section goes through. */
const SECTIONED_COLOR = 0x9b7fd4;
const DIMENSION_COLOR = 0x6aa7ff;

/** A section through the model: the plane normal to `axis` at `at` (mm), through component `ref`. */
export type Section = { axis: Axis; at: number; ref?: string };
/** A dimension line drawn on the model, in board coordinates. */
export type Dimension = { from: Vec3; to: Vec3; label: string };

/** Faces that drag a parameter (the parameter contract's `drag`). */
export type FaceDrag = { name: string; faces: string[]; axis: Point3 };
/** A face drag in progress (`done` false) or released: how far the faces moved along the axis, mm. */
export type FaceDragEvent = { name: string; distance: number; done: boolean };
const DRAG_PREVIEW = 0x5b9bff;

/** A hit on the model: what was picked and where, in board coordinates (mm). */
/** `board` is set when the pick is a board part (the board or a component), picked whole. */
export type Hit = { pick: Picked; point: [number, number, number]; board?: boolean };

/** The three.js objects of one part. */
type PartObjects = {
  node: THREE.Object3D;
  meshes: THREE.Mesh[];
  lines: THREE.LineSegments[];
  info: PartIndex;
};

export class Viewer {
  readonly renderer: THREE.WebGLRenderer;
  readonly camera: THREE.PerspectiveCamera;
  private readonly scene = new THREE.Scene();
  private model: THREE.Object3D | null = null;
  private grid: THREE.GridHelper | null = null;
  /** The bounds of the model as drawn (in any layout), in three.js world space. */
  private box = new THREE.Box3();
  private fitDist = 1;
  private minZoom = 10;
  private settings: ViewSettings = { edges: true, grid: true };
  private resizeObserver: ResizeObserver;
  private listeners = new Set<(s: ViewerState) => void>();
  private disposed = false;
  private loaded = false;
  private parts = new Map<string, PartObjects>();
  private partViews: PartViews = {};
  /** Highlight overlays, each a child of the part mesh or edge lines it covers. */
  private overlays: THREE.Object3D[] = [];
  private readonly target = new THREE.Vector3();
  private dist = 200;
  private minDistance = 0;
  private maxDistance = Infinity;
  private orient: Orientation = targetOrientation(viewTarget("iso"));
  /** The camera's rotation; differs from `orient` only while animating. */
  private quat = toQuaternion(this.orient);
  private label = "Home";
  private animation: { frame: number; progress: number } | null = null;
  private detachInput: () => void;
  private marker: [number, number, number] | null = null;
  private drags: FaceDrag[] = [];
  private onFaceDrag: ((e: FaceDragEvent) => void) | null = null;
  /** The dragged faces drawn where the drag has moved them. */
  private dragPreview: THREE.Object3D[] = [];
  /** The node the parts are in: board coordinates (the model root turns them Y-up). */
  private frame: THREE.Object3D | null = null;
  private board: Board | null = null;
  private boardGroup: THREE.Group | null = null;
  private components = new Map<string, THREE.Mesh>();
  /** The board's meshes by part name: the board itself, and each component by reference. */
  private boardMeshes = new Map<string, THREE.Mesh>();
  private layout: Layout = "assembled";
  /** Each part node's assembled place, to come back to from another layout. */
  private assembled = new Map<string, { position: THREE.Vector3; quaternion: THREE.Quaternion }>();
  private section: Section | null = null;
  private clip: THREE.Plane | null = null;
  private dimensions: THREE.Object3D | null = null;
  private dimensionList: Dimension[] = [];

  constructor(private readonly container: HTMLElement) {
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.domElement.style.display = "block";
    this.renderer.localClippingEnabled = true;
    container.appendChild(this.renderer.domElement);

    this.camera = new THREE.PerspectiveCamera(35, 1, 0.1, 10_000);

    const key = new THREE.DirectionalLight(0xffffff, 2.2);
    key.position.set(0.5, 1, 0.8);
    this.camera.add(key);
    this.scene.add(this.camera);
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x404040, 1.4));

    this.detachInput = this.attachInput(this.renderer.domElement);
    this.placeCamera();

    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(container);
    this.resize();
  }

  /** Loads a glTF (mesh contract) from a URL, replacing any current model, and fits it. */
  async load(url: string): Promise<ModelInfo> {
    const gltf = await new GLTFLoader().loadAsync(url);
    if (this.disposed) throw new Error("viewer disposed");
    if (this.model) {
      this.scene.remove(this.model);
      this.clearHighlight();
      this.hoverPicks = [];
      this.hoverKey = "";
      this.dragPreview = [];
      disposeObject(this.model);
    }
    const root = gltf.scene;
    const parts: string[] = [];
    const index: PartIndex[] = [];
    const materials: Record<string, string> = {};
    let faces = 0;
    let edges = 0;
    this.parts.clear();
    root.traverse((obj) => {
      const extras = obj.userData as Partial<PartIndex>;
      if (extras.part) {
        const info = { part: extras.part, faces: extras.faces ?? [], edges: extras.edges ?? [] };
        parts.push(info.part);
        index.push(info);
        const material = (obj.userData as { material?: unknown }).material;
        if (typeof material === "string") materials[info.part] = material;
        faces += info.faces.length;
        edges += info.edges.length;
        this.parts.set(info.part, { node: obj, meshes: [], lines: [], info });
      }
    });
    root.traverse((obj) => {
      if ((obj as THREE.Mesh).isMesh) {
        const mesh = obj as THREE.Mesh;
        const part = partOf(mesh);
        const n = Math.max(0, parts.indexOf(part ?? ""));
        if (part) this.parts.get(part)?.meshes.push(mesh);
        mesh.material = new THREE.MeshStandardMaterial({
          color: mesh.geometry.getAttribute("color") ? 0xffffff : PART_COLORS[n % PART_COLORS.length],
          vertexColors: !!mesh.geometry.getAttribute("color"),
          roughness: 0.75,
          metalness: 0.05,
          side: THREE.DoubleSide,
          polygonOffset: true,
          polygonOffsetFactor: 1,
          polygonOffsetUnits: 1,
        });
      } else if ((obj as THREE.LineSegments).isLineSegments) {
        const line = obj as THREE.LineSegments;
        line.material = new THREE.LineBasicMaterial({ color: 0x1b1b1b, transparent: true, opacity: 0.85 });
        line.userData.isEdge = true;
        const part = partOf(line);
        if (part) this.parts.get(part)?.lines.push(line);
      }
    });
    // Cut faces: the inside of each part, seen through a section, drawn in one flat color.
    for (const { meshes } of this.parts.values())
      for (const mesh of meshes) {
        const cap = new THREE.Mesh(
          mesh.geometry,
          new THREE.MeshBasicMaterial({ color: SECTION_COLOR, side: THREE.BackSide }),
        );
        cap.userData.isCap = true;
        cap.visible = false;
        mesh.add(cap);
      }
    this.assembled.clear();
    for (const [part, { node }] of this.parts)
      this.assembled.set(part, { position: node.position.clone(), quaternion: node.quaternion.clone() });
    this.model = root;
    this.frame = this.parts.values().next().value?.node.parent ?? root;
    this.scene.add(root);
    this.dimensions = null;
    this.boardGroup = null;
    this.buildBoard();
    // The model's size, from where the parts are assembled.
    const spans = [...this.parts.keys()].flatMap((part) => {
      const b = this.assembledBounds(part);
      return b ? [{ part, min: b.min, max: b.max }] : [];
    });
    const lo = [0, 1, 2].map((k) => (spans.length ? Math.min(...spans.map((b) => b.min[k])) : 0)) as Vec3;
    const hi = [0, 1, 2].map((k) => (spans.length ? Math.max(...spans.map((b) => b.max[k])) : 0)) as Vec3;
    const size = [0, 1, 2].map((k) => hi[k] - lo[k]);
    this.applyLayout();
    root.updateMatrixWorld(true);
    this.box = this.contentBox();
    this.placeGrid();
    this.applySettings();
    this.applyPartViews();
    this.applySection();
    // The first model opens in the iso view; later builds keep the user's direction.
    this.fit(this.loaded ? undefined : "iso");
    this.loaded = true;
    return { parts, index, materials, size: [size[0], size[1], size[2]], bounds: { min: lo, max: hi }, faces, edges };
  }

  /**
   * What is under a point on the canvas (client pixels): the face, the edge or the part,
   * depending on `kind`. Hidden parts are skipped, and see-through parts only count when
   * nothing opaque is behind them.
   */
  pickAt(clientX: number, clientY: number, kind: Exclude<PickKind, "component">): Hit | null {
    if (!this.model) return null;
    const rect = this.renderer.domElement.getBoundingClientRect();
    const ndc = new THREE.Vector2(((clientX - rect.left) / rect.width) * 2 - 1, -((clientY - rect.top) / rect.height) * 2 + 1);
    const raycaster = new THREE.Raycaster();
    raycaster.setFromCamera(ndc, this.camera);
    // About 6 px either side of an edge.
    raycaster.params.Line = { threshold: (this.dist * 12) / Math.max(1, rect.height) };
    const shown = [...this.parts.values()].filter((p) => !partView(this.partViews, p.info.part).hidden);
    // The board's parts, when drawn, are picked whole (not by edge).
    const targets =
      kind === "edge"
        ? shown.flatMap((p) => p.lines)
        : [...shown.flatMap((p) => this.pickable(p.meshes)), ...this.shownBoardMeshes()];
    const hits = this.unclipped(raycaster.intersectObjects(targets, false));
    const solid = hits.filter((h) => !this.hitSeeThrough(h.object));
    const hit = solid[0] ?? hits[0];
    if (!hit) return null;
    const boardPart = hit.object.userData.boardPart as string | undefined;
    if (boardPart) {
      // Board coordinates: the board is drawn assembled whatever the layout.
      const local = hit.object.worldToLocal(hit.point.clone()).applyMatrix4(hit.object.matrix);
      const point: [number, number, number] = [round2(local.x), round2(local.y), round2(local.z)];
      return { pick: boardPick(boardPart), point, board: true };
    }
    const part = partOf(hit.object);
    const info = part ? this.parts.get(part)?.info : undefined;
    if (!part || !info) return null;
    const p = hit.point;
    // three.js world (x, y, z) is board (x, −z, y); the point is where it is in the assembly.
    const point: [number, number, number] = [round2(p.x), round2(-p.z), round2(p.y)];
    const geometry = (hit.object as THREE.Mesh).geometry;
    if (kind === "face" && hit.face) {
      const id = info.faces[geometry.getAttribute("_face_id")?.getX(hit.face.a) ?? -1]?.id;
      return id ? { pick: { kind, id, part }, point } : null;
    }
    if (kind === "edge" && hit.index !== undefined) {
      const id = info.edges[geometry.getAttribute("_edge_id")?.getX(hit.index) ?? -1]?.id;
      return id ? { pick: { kind, id, part }, point } : null;
    }
    return { pick: { kind: "part", id: part, part }, point };
  }

  /** Highlights the picked faces, edges and parts, replacing the previous highlight. */
  setHighlight(picks: Picked[]) {
    this.selectedPicks = picks;
    this.redrawHighlight();
  }

  /**
   * Previews what a click would pick where the pointer is (a face, an edge, a whole part or a
   * component), fainter than the selection; an empty list clears it.
   */
  setHover(picks: Picked[]) {
    const key = picks.map((p) => `${p.kind}:${p.id}`).join(",");
    if (key === this.hoverKey) return;
    this.hoverKey = key;
    this.hoverPicks = picks;
    this.redrawHighlight();
  }
  private selectedPicks: Picked[] = [];
  private hoverPicks: Picked[] = [];
  private hoverKey = "";

  private redrawHighlight() {
    this.clearHighlight();
    const selected = new Set(this.selectedPicks.map((p) => `${p.kind}:${p.id}`));
    this.drawHighlight(this.selectedPicks, 0.6);
    this.drawHighlight(this.hoverPicks.filter((p) => !selected.has(`${p.kind}:${p.id}`)), 0.3);
    this.applyPartViews();
    this.applySection();
    this.render();
  }

  private drawHighlight(picks: Picked[], opacity: number) {
    if (!picks.length) return;
    const faces = new Set(picks.filter((p) => p.kind === "face").map((p) => p.id));
    const edges = new Set(picks.filter((p) => p.kind === "edge").map((p) => p.id));
    const whole = new Set(picks.filter((p) => p.kind === "part").map((p) => p.part));
    for (const { info, meshes, lines } of this.parts.values()) {
      const faceIdx = new Set(info.faces.flatMap((f, i) => (whole.has(info.part) || faces.has(f.id) ? [i] : [])));
      const edgeIdx = new Set(info.edges.flatMap((e, i) => (edges.has(e.id) ? [i] : [])));
      for (const mesh of faceIdx.size ? meshes : []) {
        const positions = trianglesWhere(mesh.geometry, "_face_id", faceIdx);
        if (!positions.length) continue;
        const g = new THREE.BufferGeometry();
        g.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
        const overlay = new THREE.Mesh(
          g,
          new THREE.MeshBasicMaterial({
            color: HIGHLIGHT,
            transparent: true,
            opacity,
            side: THREE.DoubleSide,
            depthWrite: false,
            polygonOffset: true,
            polygonOffsetFactor: -1,
            polygonOffsetUnits: -1,
          }),
        );
        overlay.renderOrder = 1;
        this.addOverlay(mesh, overlay);
      }
      for (const line of edgeIdx.size ? lines : []) {
        const positions = segmentsWhere(line.geometry, "_edge_id", edgeIdx);
        if (!positions.length) continue;
        const g = new THREE.BufferGeometry();
        g.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
        const overlay = new THREE.LineSegments(g, new THREE.LineBasicMaterial({ color: HIGHLIGHT, depthTest: false }));
        overlay.renderOrder = 1;
        // On the part's mesh, not the lines, so it shows with the edges setting off too.
        this.addOverlay(meshes[0] ?? line, overlay);
      }
    }
    // Board parts picked whole: the board or a component, drawn over in the highlight colour.
    for (const name of new Set(picks.flatMap((p) => boardPartOf(p) ?? []))) {
      const mesh = this.boardMeshes.get(name);
      if (!mesh) continue;
      const overlay = new THREE.Mesh(
        mesh.geometry.clone(),
        new THREE.MeshBasicMaterial({
          color: HIGHLIGHT,
          transparent: true,
          opacity,
          side: THREE.DoubleSide,
          depthWrite: false,
          polygonOffset: true,
          polygonOffsetFactor: -1,
          polygonOffsetUnits: -1,
        }),
      );
      overlay.renderOrder = 1;
      this.addOverlay(mesh, overlay);
    }
  }

  /**
   * Makes faces draggable: a left drag that starts on one of a drag's faces moves them along its
   * axis instead of orbiting, reporting the distance as it goes and when released. Other faces
   * orbit as before. An empty list turns face dragging off.
   */
  setFaceDrags(drags: FaceDrag[], listener: ((e: FaceDragEvent) => void) | null) {
    this.drags = drags;
    this.onFaceDrag = listener;
    if (!drags.length) this.renderer.domElement.style.cursor = "";
  }

  /** The face drag that starts at a point on the canvas (client pixels), if any. */
  faceDragAt(clientX: number, clientY: number): { drag: FaceDrag; perMm: [number, number] } | null {
    if (!this.drags.length) return null;
    const hit = this.pickAt(clientX, clientY, "face");
    const drag = hit && this.drags.find((d) => d.faces.includes(hit.pick.id));
    if (!hit || !drag) return null;
    const a = this.project(hit.point);
    const [x, y, z] = hit.point;
    const b = this.project([x + drag.axis[0], y + drag.axis[1], z + drag.axis[2]]);
    if (!a || !b) return null;
    const perMm: [number, number] = [b[0] - a[0], b[1] - a[1]];
    return dragDistance(perMm, 1, 0) === null ? null : { drag, perMm };
  }



  /** Draws the dragged faces moved `distance` mm along the drag's axis (null clears it). */
  private previewDrag(drag: FaceDrag | null, distance = 0) {
    if (!drag) {
      for (const o of this.dragPreview) {
        o.removeFromParent();
        disposeObject(o);
      }
      this.dragPreview = [];
      this.render();
      return;
    }
    if (!this.dragPreview.length) {
      const faces = new Set(drag.faces);
      for (const { info, meshes } of this.parts.values()) {
        const idx = new Set(info.faces.flatMap((f, i) => (faces.has(f.id) ? [i] : [])));
        for (const mesh of idx.size ? meshes : []) {
          const positions = trianglesWhere(mesh.geometry, "_face_id", idx);
          if (!positions.length) continue;
          const g = new THREE.BufferGeometry();
          g.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
          const overlay = new THREE.Mesh(
            g,
            new THREE.MeshBasicMaterial({ color: DRAG_PREVIEW, transparent: true, opacity: 0.55, side: THREE.DoubleSide, depthWrite: false }),
          );
          overlay.renderOrder = 2;
          overlay.raycast = () => {};
          // The mesh's own frame is board coordinates (the root turns board Z-up into Y-up).
          mesh.add(overlay);
          this.dragPreview.push(overlay);
        }
      }
    }
    const [x, y, z] = drag.axis;
    for (const o of this.dragPreview) o.position.set(x * distance, y * distance, z * distance);
    this.render();
  }

  /** Hides parts and makes them see-through. Parts not listed are shown and opaque. */
  setPartViews(views: PartViews) {
    this.partViews = views;
    this.applyPartViews();
    this.render();
  }

  /** Frames the whole model, keeping the current direction or switching to `view`. */
  fit(view?: ViewName) {
    if (this.box.isEmpty()) return;
    const sphere = this.box.getBoundingSphere(new THREE.Sphere());
    this.fitDist = fitDistance(sphere.radius, this.camera.fov, this.camera.aspect, FIT_MARGIN);
    this.applyZoomLimits();
    this.target.copy(sphere.center);
    this.dist = this.fitDist;
    if (view) this.snapTo(viewTarget(view).key, { animate: false, home: view === "iso" });
    else this.placeCamera();
  }

  /**
   * Centres the view on a point in board coordinates (mm) and zooms in to `zoom` percent,
   * keeping the viewing direction. Used to take the camera to a check result. A point on `part`
   * is followed to where the part is in the exploded view.
   */
  focus(point: [number, number, number], zoom = FOCUS_ZOOM, part: string | null = null) {
    this.stopAnimation();
    this.target.copy(this.placed(point, part));
    const p = THREE.MathUtils.clamp(zoom, this.minZoom, MAX_ZOOM);
    this.dist = THREE.MathUtils.clamp((this.fitDist * 100) / p, this.minDistance, this.maxDistance);
    this.placeCamera();
  }

  /**
   * Where a point in board coordinates (mm) appears in the viewport, in CSS pixels from its
   * top left, or null when it is behind the camera or outside the view. A point on `part` moves
   * with that part in the exploded view.
   */
  toScreen(point: [number, number, number], part: string | null = null): { x: number; y: number } | null {
    this.camera.updateMatrixWorld();
    const p = this.placed(point, part).project(this.camera);
    if (Math.abs(p.x) > 1 || Math.abs(p.y) > 1 || p.z > 1 || p.z < -1) return null;
    const { clientWidth: w, clientHeight: h } = this.container;
    return { x: ((p.x + 1) / 2) * w, y: ((1 - p.y) / 2) * h };
  }

  /**
   * Passes a wheel event from an element drawn over the canvas (a check pin) on to the
   * canvas, so the wheel zooms wherever the pointer is.
   */
  forwardWheel(e: WheelEvent) {
    const { deltaX, deltaY, deltaZ, deltaMode, clientX, clientY, ctrlKey, shiftKey, altKey, metaKey } = e;
    this.renderer.domElement.dispatchEvent(
      new WheelEvent("wheel", {
        deltaX, deltaY, deltaZ, deltaMode, clientX, clientY, ctrlKey, shiftKey, altKey, metaKey,
        bubbles: true,
        cancelable: true,
      }),
    );
  }

  /**
   * Hands a drag that started on an element drawn over the canvas (a check pin) to the canvas:
   * re-dispatches the press (`down`) there and then the move that crossed the click slop, so the
   * canvas captures the pointer and the rest of the drag orbits or pans as if it began on the model.
   */
  forwardDrag(down: PointerEvent, move: PointerEvent) {
    const el = this.renderer.domElement;
    const redispatch = (type: string, e: PointerEvent, buttons: number) =>
      el.dispatchEvent(
        new PointerEvent(type, {
          pointerId: e.pointerId,
          pointerType: e.pointerType,
          isPrimary: e.isPrimary,
          button: type === "pointerdown" ? down.button : -1,
          buttons,
          clientX: e.clientX,
          clientY: e.clientY,
          shiftKey: e.shiftKey,
          ctrlKey: e.ctrlKey,
          altKey: e.altKey,
          metaKey: e.metaKey,
          bubbles: true,
          cancelable: true,
        }),
      );
    redispatch("pointerdown", down, down.buttons);
    redispatch("pointermove", move, move.buttons);
  }

  /** Looks at the model from a named view, keeping the zoom. */
  setView(view: ViewName) {
    if (view === "iso") this.home();
    else this.snapTo(viewTarget(view).key);
  }

  /** Snaps to a view cube target (a face, edge or corner key) with Z up. */
  snapTo(key: string, { animate = true, home = false }: { animate?: boolean; home?: boolean } = {}) {
    const t = targetByKey(key);
    if (!t) return;
    this.rotateTo(targetOrientation(t), home ? "Home" : t.label, animate);
  }

  /** The Front-Right-Top corner view, Z up. */
  home() {
    this.snapTo(HOME_KEY, { home: true });
  }

  /** Turns 90° to the neighbouring face on that side of the screen. */
  turn(direction: TurnDirection) {
    const o = turn(this.orient, direction);
    this.rotateTo(o, currentTarget(o)?.label ?? "Free orbit");
  }

  /** Spins the view 90° about the line of sight. */
  roll(direction: RollDirection) {
    this.rotateTo(roll(this.orient, direction), this.label);
  }

  /** Turntable orbit for a pointer move of (dx, dy) CSS pixels; follows the pointer. */
  orbitBy(dx: number, dy: number) {
    this.stopAnimation();
    this.orient = orbit(this.orient, dx, dy);
    this.quat = toQuaternion(this.orient);
    this.label = "Free orbit";
    this.placeCamera();
  }

  /** Moves the orbit centre with the pointer, by (dx, dy) CSS pixels. */
  panBy(dx: number, dy: number) {
    const h = this.container.clientHeight || 1;
    const perPx = (2 * this.dist * Math.tan(THREE.MathUtils.degToRad(this.camera.fov / 2))) / h;
    const right = new THREE.Vector3(1, 0, 0).applyQuaternion(this.quat);
    const up = new THREE.Vector3(0, 1, 0).applyQuaternion(this.quat);
    this.target.addScaledVector(right, -dx * perPx).addScaledVector(up, dy * perPx);
    this.placeCamera();
  }

  /** Sets the zoom in percent of the fitted distance (100 = fitted). */
  setZoom(percent: number) {
    const p = THREE.MathUtils.clamp(percent, this.minZoom, MAX_ZOOM);
    this.dist = (this.fitDist * 100) / p;
    this.placeCamera();
  }

  /** Zooms by a wheel delta, in pixels, within the zoom limits. */
  private wheelZoom(deltaY: number) {
    const scale = Math.pow(0.95, Math.abs(deltaY) * 0.01);
    const d = deltaY > 0 ? this.dist / scale : this.dist * scale;
    this.dist = THREE.MathUtils.clamp(d, this.minDistance, this.maxDistance);
    this.placeCamera();
  }

  zoomBy(factor: number) {
    this.setZoom(this.state().zoom * factor);
  }

  setSettings(settings: ViewSettings) {
    this.settings = settings;
    this.applySettings();
    this.render();
  }

  /** Marks a spot on the model, in board coordinates (mm), whose screen position the state reports. */
  setMarker(point: [number, number, number] | null) {
    const same = point && this.marker && point.every((v, i) => v === this.marker![i]);
    if (same || point === this.marker) return;
    this.marker = point;
    this.changed();
  }

  /** Draws a board revision (board JSON) inside the enclosure: the PCB and each component's body. */
  setBoard(board: Board | null) {
    this.board = board;
    this.buildBoard();
    this.applyLayout();
    this.applyPartViews();
    this.applySection();
    this.render();
  }

  /** The components drawn, by reference designator. */
  componentRefs(): string[] {
    return [...this.components.keys()];
  }

  /**
   * Lays the parts out: as designed, exploded along Z, or on the print bed (where the board is
   * not drawn). Sections stay in the assembled design's coordinates.
   */
  setLayout(layout: Layout) {
    if (layout === this.layout) return;
    this.layout = layout;
    this.applyLayout();
    if (this.model) {
      this.model.updateMatrixWorld(true);
      this.box = this.contentBox();
      this.placeGrid();
      this.applySettings();
      this.fit();
    }
    this.render();
  }

  /** Cuts the model with a section plane (null for none); the cut faces are drawn solid. */
  setSection(section: Section | null) {
    this.section = section;
    this.applyBoardVisibility();
    this.applySection();
    this.render();
  }

  /** Draws dimension lines (board coordinates, assembled), replacing the previous ones. */
  setDimensions(dimensions: Dimension[]) {
    this.dimensionList = dimensions;
    const root = this.frame;
    if (this.dimensions) {
      this.dimensions.removeFromParent();
      disposeObject(this.dimensions);
      this.dimensions = null;
    }
    if (root && dimensions.length) {
      const group = new THREE.Group();
      group.userData.noClip = true;
      const ends = dimensions.flatMap((d) => [...d.from, ...d.to]);
      const lines = new THREE.BufferGeometry();
      lines.setAttribute("position", new THREE.Float32BufferAttribute(ends, 3));
      const line = new THREE.LineSegments(lines, new THREE.LineBasicMaterial({ color: DIMENSION_COLOR, depthTest: false }));
      const points = new THREE.Points(
        lines.clone(),
        new THREE.PointsMaterial({ color: DIMENSION_COLOR, size: 6, sizeAttenuation: false, depthTest: false }),
      );
      for (const o of [line, points]) {
        o.renderOrder = 2;
        o.raycast = () => {};
        group.add(o);
      }
      root.add(group);
      this.dimensions = group;
    }
    this.changed();
  }

  /** Where a board point (assembled, mm) is on the canvas, in CSS pixels; null behind the camera. */
  project(p: Vec3): [number, number] | null {
    if (!this.frame) return null;
    this.frame.updateMatrixWorld();
    const v = new THREE.Vector3(...p).applyMatrix4(this.frame.matrixWorld);
    v.applyMatrix4(this.camera.matrixWorldInverse);
    if (-v.z < this.camera.near) return null;
    v.applyMatrix4(this.camera.projectionMatrix);
    const { clientWidth: w, clientHeight: h } = this.container;
    return [round1(((v.x + 1) / 2) * w), round1(((1 - v.y) / 2) * h)];
  }

  /** Each enclosure part's triangles, in board coordinates where the parts are assembled. */
  partTriangles(): { part: string; triangles: Triangles }[] {
    return [...this.parts.values()].map(({ info, meshes }) => ({
      part: info.part,
      triangles: concat(meshes.map((m) => this.assembledTriangles(m))),
    }));
  }

  /** One face's triangles, in board coordinates where the parts are assembled. */
  faceTriangles(part: string, faceId: string): Triangles {
    const p = this.parts.get(part);
    const index = p?.info.faces.findIndex((f) => f.id === faceId) ?? -1;
    if (!p || index < 0) return new Float64Array();
    return concat(p.meshes.map((m) => this.assembledTriangles(m, index)));
  }

  subscribe(listener: (s: ViewerState) => void): () => void {
    this.listeners.add(listener);
    listener(this.state());
    return () => this.listeners.delete(listener);
  }

  state(): ViewerState {
    const t = this.target;
    this.camera.updateMatrixWorld();
    const rotation = new THREE.Matrix4().extractRotation(this.camera.matrixWorldInverse).elements.slice();
    const animating = !!this.animation;
    const up = screenUpAngle(this.quat);
    return {
      zoom: Math.round((this.fitDist / this.dist) * 100),
      minZoom: Math.round(this.minZoom),
      maxZoom: MAX_ZOOM,
      view: animating ? "custom" : viewKey(this.orient),
      label: animating ? "Free orbit" : this.label,
      azimuth: round1(THREE.MathUtils.radToDeg(this.orient.azimuth)),
      elevation: round1(THREE.MathUtils.radToDeg(this.orient.elevation)),
      roll: round1(THREE.MathUtils.radToDeg(this.orient.roll)),
      upAngle: up === null ? null : round1(up),
      animating,
      progress: this.animation ? Math.round(this.animation.progress * 100) / 100 : null,
      // three.js world (x, y, z) is board (x, −z, y).
      target: [round1(t.x), round1(-t.z), round1(t.y)],
      fits: this.fits(),
      rotation,
      modelRect: this.modelRect(),
      width: this.container.clientWidth,
      height: this.container.clientHeight,
      marker: this.markerPosition(),
      boardToClip: this.boardToClip(),
      dimensions: this.dimensionList.map((d) => ({
        label: d.label,
        at: this.project(d.from.map((v, i) => (v + d.to[i]) / 2) as Vec3),
      })),
    };
  }

  dispose() {
    this.disposed = true;
    this.parts.clear();
    this.resizeObserver.disconnect();
    this.stopAnimation();
    this.detachInput();
    if (this.model) disposeObject(this.model);
    if (this.grid) disposeObject(this.grid);
    this.renderer.dispose();
    this.renderer.domElement.remove();
    this.listeners.clear();
  }

  /** Puts the camera at the current rotation, distance and orbit centre, and redraws. */
  private placeCamera() {
    const dir = new THREE.Vector3(0, 0, 1).applyQuaternion(this.quat);
    this.camera.position.copy(this.target).addScaledVector(dir, this.dist);
    this.camera.quaternion.copy(this.quat);
    this.camera.updateMatrixWorld();
    this.changed();
  }

  /** Turns the camera to an orientation over SNAP_MS, eased (at once with reduced motion). */
  private rotateTo(o: Orientation, label: string, animate = true) {
    this.stopAnimation();
    const from = this.quat.clone();
    const to = toQuaternion(o);
    this.orient = o;
    this.label = label;
    const reduce = typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (!animate || reduce || from.angleTo(to) < 1e-6) {
      this.quat = to;
      this.placeCamera();
      return;
    }
    const start = performance.now();
    const anim = { frame: 0, progress: 0 };
    const step = (now: number) => {
      const t = Math.min(1, Math.max(0, (now - start) / SNAP_MS));
      anim.progress = t;
      this.quat = from.clone().slerp(to, ease(t));
      if (t >= 1) {
        this.quat = to;
        this.animation = null;
      } else {
        anim.frame = requestAnimationFrame(step);
      }
      this.placeCamera();
    };
    this.animation = anim;
    anim.frame = requestAnimationFrame(step);
  }

  /** Ends an animation at its destination (a drag takes over from there, level). */
  private stopAnimation() {
    if (!this.animation) return;
    cancelAnimationFrame(this.animation.frame);
    this.animation = null;
    this.quat = toQuaternion(this.orient);
  }

  /**
   * Pointer input on the 3D view: left drag orbits (turntable), right drag or Shift + left drag
   * pans, the wheel and a two-finger pinch zoom. A press that moves less than CLICK_SLOP_PX is
   * a click and moves nothing.
   */
  private attachInput(el: HTMLElement): () => void {
    const pointers = new Map<number, { x: number; y: number }>();
    let drag: {
      id: number;
      mode: "orbit" | "pan" | "face";
      x0: number;
      y0: number;
      x: number;
      y: number;
      moved: boolean;
      face?: { drag: FaceDrag; perMm: [number, number]; distance: number };
    } | null = null;
    const endFaceDrag = (done: boolean) => {
      const face = drag?.mode === "face" && drag.moved ? drag.face : undefined;
      if (drag?.mode === "face") this.previewDrag(null);
      if (face) this.onFaceDrag?.({ name: face.drag.name, distance: done ? face.distance : 0, done: true });
    };
    let pinch: number | null = null;
    const spread = () => {
      const [a, b] = [...pointers.values()];
      return Math.hypot(a.x - b.x, a.y - b.y);
    };
    const onDown = (e: PointerEvent) => {
      if (e.button !== 0 && e.button !== 2) return;
      el.setPointerCapture?.(e.pointerId);
      pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (pointers.size === 2) {
        endFaceDrag(false);
        drag = null;
        pinch = spread();
        return;
      }
      const mode = e.button === 2 || e.shiftKey ? "pan" : "orbit";
      drag = { id: e.pointerId, mode, x0: e.clientX, y0: e.clientY, x: e.clientX, y: e.clientY, moved: false };
      // A left drag on a draggable face moves the face.
      const face = mode === "orbit" ? this.faceDragAt(e.clientX, e.clientY) : null;
      if (face) Object.assign(drag, { mode: "face", face: { ...face, distance: 0 } });
    };
    const onMove = (e: PointerEvent) => {
      if (!pointers.has(e.pointerId)) {
        // Hovering: a face that drags shows the move cursor.
        if (this.drags.length && e.pointerType === "mouse")
          el.style.cursor = this.faceDragAt(e.clientX, e.clientY) ? "move" : "";
        return;
      }
      pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (pinch !== null && pointers.size === 2) {
        const s = spread();
        if (s > 0 && pinch > 0) {
          this.dist = THREE.MathUtils.clamp((this.dist * pinch) / s, this.minDistance, this.maxDistance);
          this.placeCamera();
        }
        pinch = s;
        return;
      }
      if (!drag || drag.id !== e.pointerId) return;
      if (!drag.moved && Math.hypot(e.clientX - drag.x0, e.clientY - drag.y0) < CLICK_SLOP_PX) return;
      drag.moved = true;
      const dx = e.clientX - drag.x;
      const dy = e.clientY - drag.y;
      drag.x = e.clientX;
      drag.y = e.clientY;
      if (drag.mode === "face" && drag.face) {
        const distance = dragDistance(drag.face.perMm, e.clientX - drag.x0, e.clientY - drag.y0) ?? 0;
        drag.face.distance = distance;
        this.previewDrag(drag.face.drag, distance);
        this.onFaceDrag?.({ name: drag.face.drag.name, distance, done: false });
      } else if (drag.mode === "orbit") this.orbitBy(dx, dy);
      else this.panBy(dx, dy);
    };
    const onUp = (e: PointerEvent) => {
      pointers.delete(e.pointerId);
      if (pointers.size < 2) pinch = null;
      if (drag?.id === e.pointerId) {
        endFaceDrag(e.type === "pointerup");
        drag = null;
      }
    };
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      this.wheelZoom(e.deltaMode === 1 ? e.deltaY * 16 : e.deltaY);
    };
    const onMenu = (e: Event) => e.preventDefault();
    el.addEventListener("pointerdown", onDown);
    el.addEventListener("pointermove", onMove);
    el.addEventListener("pointerup", onUp);
    el.addEventListener("pointercancel", onUp);
    el.addEventListener("wheel", onWheel, { passive: false });
    el.addEventListener("contextmenu", onMenu);
    return () => {
      el.removeEventListener("pointerdown", onDown);
      el.removeEventListener("pointermove", onMove);
      el.removeEventListener("pointerup", onUp);
      el.removeEventListener("pointercancel", onUp);
      el.removeEventListener("wheel", onWheel);
      el.removeEventListener("contextmenu", onMenu);
    };
  }

  /** Limits the wheel and pinch to the zoom range. */
  private applyZoomLimits() {
    const { clientWidth: w, clientHeight: h } = this.container;
    const radius = this.box.getBoundingSphere(new THREE.Sphere()).radius;
    this.minZoom = minZoomFor(radius, this.camera.fov, w, h);
    // A hair of slack so setZoom at a limit is not clamped to just inside it.
    this.minDistance = ((this.fitDist * 100) / MAX_ZOOM) * (1 - 1e-9);
    this.maxDistance = ((this.fitDist * 100) / this.minZoom) * (1 + 1e-9);
  }

  /**
   * Fits the near and far planes around everything drawn (model and grid) from where the camera
   * is now. Runs before every render, so the wheel, pinch and panning are covered too.
   */
  private updateClipPlanes() {
    const sphere = this.drawnBounds();
    if (!sphere) return;
    const { near, far } = clipPlanes(this.camera.position.distanceTo(sphere.center), sphere.radius);
    if (near === this.camera.near && far === this.camera.far) return;
    this.camera.near = near;
    this.camera.far = far;
    this.camera.updateProjectionMatrix();
  }

  private drawnBounds(): THREE.Sphere | null {
    if (this.box.isEmpty()) return null;
    const box = this.box.clone();
    if (this.grid) box.union(new THREE.Box3().setFromObject(this.grid));
    return box.getBoundingSphere(new THREE.Sphere());
  }

  /** The model's projected bounding box on the canvas, from its mesh vertices. */
  private modelRect(): [number, number, number, number] | null {
    if (!this.model) return null;
    const { clientWidth: w, clientHeight: h } = this.container;
    this.camera.updateMatrixWorld();
    const view = this.camera.matrixWorldInverse;
    const v = new THREE.Vector3();
    let [x0, y0, x1, y1] = [Infinity, Infinity, -Infinity, -Infinity];
    let behind = false;
    this.model.traverse((obj) => {
      const mesh = obj as THREE.Mesh;
      // The enclosure: not the board, which may reach far outside it, nor the cut faces.
      if (!mesh.isMesh || mesh.userData.board || mesh.userData.isCap) return;
      const pos = mesh.geometry.getAttribute("position");
      for (let i = 0; i < pos.count; i++) {
        v.fromBufferAttribute(pos, i).applyMatrix4(mesh.matrixWorld).applyMatrix4(view);
        // In front of the near plane (or behind the camera) the projection is not meaningful;
        // the model then reaches past the canvas edges.
        if (-v.z < this.camera.near) {
          behind = true;
          continue;
        }
        v.applyMatrix4(this.camera.projectionMatrix);
        x0 = Math.min(x0, v.x);
        x1 = Math.max(x1, v.x);
        y0 = Math.min(y0, v.y);
        y1 = Math.max(y1, v.y);
      }
    });
    if (behind) [x0, y0, x1, y1] = [-1, -1, 1, 1];
    if (x0 > x1) return null;
    const px = (n: number) => ((THREE.MathUtils.clamp(n, -1, 1) + 1) / 2) * w;
    const py = (n: number) => ((1 - THREE.MathUtils.clamp(n, -1, 1)) / 2) * h;
    const left = px(x0);
    const top = py(y1);
    return [round1(left), round1(top), round1(px(x1) - left), round1(py(y0) - top)];
  }

  /** The marker on screen. Part nodes carry no transform of their own, so a part's world
   * matrix maps board coordinates to the scene (mesh contract). */
  private markerPosition(): [number, number] | null {
    if (!this.marker || !this.model) return null;
    let frame: THREE.Object3D | null = null;
    this.model.traverse((obj) => {
      if (!frame && (obj.userData as { part?: string }).part) frame = obj;
    });
    if (!frame) return null;
    const world = new THREE.Vector3(...this.marker).applyMatrix4((frame as THREE.Object3D).matrixWorld);
    const { clientWidth: w, clientHeight: h } = this.container;
    return projectToCanvas(world, this.camera, w, h);
  }

  private fits(): boolean {
    if (this.box.isEmpty()) return false;
    this.camera.updateMatrixWorld();
    const { min, max } = this.box;
    for (const x of [min.x, max.x])
      for (const y of [min.y, max.y])
        for (const z of [min.z, max.z]) {
          const p = new THREE.Vector3(x, y, z).project(this.camera);
          if (Math.abs(p.x) > 1 || Math.abs(p.y) > 1 || p.z > 1 || p.z < -1) return false;
        }
    return true;
  }

  private placeGrid() {
    if (this.grid) {
      this.scene.remove(this.grid);
      disposeObject(this.grid);
    }
    const size = this.box.getSize(new THREE.Vector3());
    const span = Math.ceil((Math.max(size.x, size.z) * 2) / 10) * 10;
    // One line every 10 mm.
    this.grid = new THREE.GridHelper(span, span / 10, 0x4a4a4a, 0x303030);
    // The grid is a backdrop: drawn first and without writing depth, so the model is always
    // drawn over it, whichever side the camera looks from.
    const material = this.grid.material as THREE.LineBasicMaterial;
    material.depthWrite = false;
    this.grid.renderOrder = -1;
    const c = this.box.getCenter(new THREE.Vector3());
    this.grid.position.set(c.x, this.box.min.y - 0.01, c.z);
    this.scene.add(this.grid);
  }

  private applySettings() {
    if (this.grid) this.grid.visible = this.settings.grid;
    this.model?.traverse((obj) => {
      if (obj.userData.isEdge) obj.visible = this.settings.edges;
    });
  }

  private clearHighlight() {
    for (const overlay of this.overlays) {
      overlay.removeFromParent();
      disposeObject(overlay);
    }
    this.overlays = [];
  }

  /** Overlays share their target's frame (board coordinates) and are hidden with its part. */
  private addOverlay(target: THREE.Object3D, overlay: THREE.Object3D) {
    overlay.raycast = () => {};
    target.add(overlay);
    this.overlays.push(overlay);
  }

  /** A point in board coordinates on `part`, in three.js world space where the part is drawn. */
  private placed(point: [number, number, number], part: string | null): THREE.Vector3 {
    // Where the part is drawn now (exploded, or laid out on the print bed): the point goes from
    // where the part is assembled to where its node is, in the mesh's own frame.
    const p = part ? this.parts.get(part) : undefined;
    const home = part ? this.assembled.get(part) : undefined;
    const parent = p?.node.parent;
    if (p && home && parent) {
      parent.updateMatrixWorld(true);
      const assembled = new THREE.Matrix4().compose(home.position, home.quaternion, p.node.scale);
      p.node.updateMatrix();
      return new THREE.Vector3(...point)
        .applyMatrix4(assembled.invert())
        .applyMatrix4(p.node.matrix)
        .applyMatrix4(parent.matrixWorld);
    }
    if (this.frame) {
      this.frame.updateMatrixWorld(true);
      return new THREE.Vector3(...point).applyMatrix4(this.frame.matrixWorld);
    }
    return boardToWorld(point);
  }

  private applyPartViews() {
    for (const { node, meshes, lines, info } of this.parts.values()) {
      const view = partView(this.partViews, info.part);
      const transparent = this.seeThrough(info.part);
      node.visible = !view.hidden;
      for (const mesh of meshes) {
        for (const m of [mesh.material, ...mesh.children.filter((c) => c.userData.isCap).map((c) => (c as THREE.Mesh).material)]) {
          const mat = m as THREE.Material;
          mat.transparent = transparent;
          mat.opacity = transparent ? TRANSPARENT_OPACITY : 1;
          // A see-through part must not hide what is behind it.
          mat.depthWrite = !transparent;
          mat.needsUpdate = true;
        }
      }
      for (const line of lines) (line.material as THREE.LineBasicMaterial).opacity = transparent ? 0.3 : 0.85;
    }
    // The board's parts: hidden or see-through by their own views.
    for (const [name, mesh] of this.boardMeshes) {
      const view = partView(this.partViews, name);
      mesh.visible = !view.hidden;
      const mat = mesh.material as THREE.Material;
      mat.transparent = view.transparent;
      mat.opacity = view.transparent ? TRANSPARENT_OPACITY : 1;
      mat.depthWrite = !view.transparent;
      mat.needsUpdate = true;
    }
    this.applyBoardVisibility();
  }

  /** A part is see-through when the parts panel says so. */
  private seeThrough(part: string) {
    return partView(this.partViews, part).transparent;
  }

  /** The meshes to raycast for a part: with a section, its cut faces too. */
  private pickable(meshes: THREE.Mesh[]): THREE.Object3D[] {
    if (!this.clip) return meshes;
    return meshes.flatMap((m) => [m, ...m.children.filter((c) => c.userData.isCap)]);
  }

  /** Hits on the kept side of the section plane. */
  private unclipped(hits: THREE.Intersection[]): THREE.Intersection[] {
    const clip = this.clip;
    return clip ? hits.filter((h) => clip.distanceToPoint(h.point) >= -1e-6) : hits;
  }

  /** A mesh's triangles (only those of face `face`, if given) in assembled board coordinates. */
  private assembledTriangles(mesh: THREE.Mesh, face?: number): Triangles {
    const part = partOf(mesh);
    const node = part ? this.parts.get(part)?.node : undefined;
    const home = part ? this.assembled.get(part) : undefined;
    // From the mesh to its part node, then the node's assembled place in the model root.
    const toNode = new THREE.Matrix4();
    for (let o: THREE.Object3D | null = mesh; o && o !== node; o = o.parent) toNode.premultiply(o.matrix);
    const nodeMatrix = home ? new THREE.Matrix4().compose(home.position, home.quaternion, node!.scale) : new THREE.Matrix4();
    const m = nodeMatrix.multiply(toNode);
    const g = mesh.geometry;
    const pos = g.getAttribute("position");
    const ids = g.getAttribute("_face_id");
    const index = g.getIndex();
    const count = index ? index.count : pos.count;
    const out: number[] = [];
    const v = new THREE.Vector3();
    for (let i = 0; i + 2 < count; i += 3) {
      const a = index ? index.getX(i) : i;
      if (face !== undefined && ids?.getX(a) !== face) continue;
      for (let k = 0; k < 3; k++) {
        v.fromBufferAttribute(pos, index ? index.getX(i + k) : i + k).applyMatrix4(m);
        out.push(v.x, v.y, v.z);
      }
    }
    return Float64Array.from(out);
  }

  /** The PCB (its outline extruded by its thickness) and a box per component, in the model root. */
  private buildBoard() {
    if (this.boardGroup) {
      this.boardGroup.removeFromParent();
      disposeObject(this.boardGroup);
      this.boardGroup = null;
    }
    this.components.clear();
    this.boardMeshes.clear();
    const board = this.board;
    if (!board || !this.frame) return;
    const group = new THREE.Group();
    group.name = "board";
    const outline = new THREE.Shape(board.pcb.outline.map(([x, y]) => new THREE.Vector2(x, y)));
    const pcb = new THREE.Mesh(
      new THREE.ExtrudeGeometry(outline, { depth: board.pcb.thickness, bevelEnabled: false, curveSegments: 1 }),
      new THREE.MeshStandardMaterial({ color: PCB_COLOR, roughness: 0.8, side: THREE.DoubleSide }),
    );
    pcb.userData.board = true;
    pcb.userData.boardPart = BOARD_PART;
    pcb.name = BOARD_PART;
    group.add(pcb);
    this.boardMeshes.set(BOARD_PART, pcb);
    for (const c of board.components) {
      const body = componentBody(board, c);
      const shape = new THREE.Shape(body.corners.map(([x, y]) => new THREE.Vector2(x, y)));
      const geometry = new THREE.ExtrudeGeometry(shape, { depth: body.z1 - body.z0, bevelEnabled: false, curveSegments: 1 });
      geometry.translate(0, 0, body.z0);
      const mesh = new THREE.Mesh(
        geometry,
        new THREE.MeshStandardMaterial({ color: COMPONENT_COLOR, roughness: 0.6, side: THREE.DoubleSide }),
      );
      mesh.userData.board = true;
      mesh.userData.component = c.ref;
      mesh.userData.boardPart = c.ref;
      mesh.name = c.ref;
      group.add(mesh);
      this.components.set(c.ref, mesh);
      this.boardMeshes.set(c.ref, mesh);
    }
    this.frame.add(group);
    this.boardGroup = group;
  }

  /** Moves the part nodes to their places in the layout. */
  private applyLayout() {
    const parts = [...this.parts.entries()].flatMap(([part, { node }]) => {
      const home = this.assembled.get(part);
      if (!home) return [];
      node.position.copy(home.position);
      node.quaternion.copy(home.quaternion);
      node.updateMatrix();
      return [{ part, node, bounds: this.assembledBounds(part) }];
    });
    const placements = layoutPlacements(
      this.layout,
      parts.flatMap((p) => (p.bounds ? [{ part: p.part, bounds: p.bounds }] : [])),
    );
    for (const { part, node, bounds } of parts) {
      const place = placements[part];
      if (!place || !bounds) continue;
      const c = new THREE.Vector3(...bounds.min).add(new THREE.Vector3(...bounds.max)).multiplyScalar(0.5);
      const turn = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(1, 0, 0), place.flip ? Math.PI : 0);
      // p' = turn (p − c) + c + offset, for the node's assembled place p.
      node.position.sub(c).applyQuaternion(turn).add(c).add(new THREE.Vector3(...place.offset));
      node.quaternion.premultiply(turn);
      node.updateMatrix();
    }
    this.applyBoardVisibility();
  }

  /** The board is drawn in every layout but the print bed (`boardShown`). */
  private applyBoardVisibility() {
    if (!this.boardGroup) return;
    const parts = [...this.parts.keys()];
    const enclosureHidden = parts.length > 0 && parts.every((p) => partView(this.partViews, p).hidden);
    this.boardGroup.visible = boardShown({
      layout: this.layout,
      section: !!this.section,
      enclosureHidden,
    });
  }

  /** The board meshes a ray can hit: those of the drawn board that are not hidden. */
  private shownBoardMeshes(): THREE.Mesh[] {
    return this.boardGroup?.visible ? [...this.boardMeshes.values()].filter((m) => m.visible) : [];
  }

  /** Whether a hit object is see-through: a board part by its own view, an enclosure part as set. */
  private hitSeeThrough(obj: THREE.Object3D): boolean {
    const board = obj.userData.boardPart as string | undefined;
    return board ? partView(this.partViews, board).transparent : this.seeThrough(partOf(obj) ?? "");
  }

  private assembledBounds(part: string): { min: Vec3; max: Vec3 } | null {
    const p = this.parts.get(part);
    if (!p) return null;
    const t = concat(p.meshes.map((m) => this.assembledTriangles(m)));
    if (!t.length) return null;
    const min: Vec3 = [Infinity, Infinity, Infinity];
    const max: Vec3 = [-Infinity, -Infinity, -Infinity];
    for (let i = 0; i < t.length; i += 3)
      for (let k = 0; k < 3; k++) {
        min[k] = Math.min(min[k], t[i + k]);
        max[k] = Math.max(max[k], t[i + k]);
      }
    return { min, max };
  }

  /** The bounding box the view fits: the enclosure parts where they are laid out. */
  private contentBox(): THREE.Box3 {
    const box = new THREE.Box3();
    if (!this.model) return box;
    this.model.updateMatrixWorld(true);
    // The enclosure only: the board may reach far outside it (a probe, a connector edge).
    for (const { node } of this.parts.values()) box.union(new THREE.Box3().setFromObject(node));
    return box;
  }

  /** Clips every material of the model by the section plane, and shows the cut faces. */
  private applySection() {
    const root = this.model;
    const s = this.section;
    if (root && this.frame && s) {
      const { normal, constant } = sectionPlane(s.axis, s.at);
      root.updateMatrixWorld(true);
      this.clip = new THREE.Plane(new THREE.Vector3(...normal), constant).applyMatrix4(this.frame.matrixWorld);
    } else {
      this.clip = null;
    }
    const planes = this.clip ? [this.clip] : null;
    root?.traverse((obj) => {
      if (obj.userData.noClip) return;
      const m = (obj as THREE.Mesh).material;
      if (!m) return;
      for (const mat of Array.isArray(m) ? m : [m]) {
        mat.clippingPlanes = planes;
        mat.needsUpdate = true;
      }
    });
    for (const { meshes } of this.parts.values())
      for (const mesh of meshes) {
        // Cut open, a part's inside shows as the cut face; outside the cut it is drawn as usual.
        (mesh.material as THREE.Material).side = this.clip ? THREE.FrontSide : THREE.DoubleSide;
        for (const cap of mesh.children) if (cap.userData.isCap) cap.visible = !!this.clip;
      }
    for (const [ref, mesh] of this.components)
      (mesh.material as THREE.MeshStandardMaterial).color.set(s?.ref === ref ? SECTIONED_COLOR : COMPONENT_COLOR);
  }

  private boardToClip(): number[] {
    const m = new THREE.Matrix4();
    if (!this.frame) return m.elements.slice();
    this.camera.updateMatrixWorld();
    this.frame.updateMatrixWorld();
    return m
      .multiplyMatrices(this.camera.projectionMatrix, this.camera.matrixWorldInverse)
      .multiply(this.frame.matrixWorld)
      .elements.slice();
  }

  private resize() {
    const { clientWidth: w, clientHeight: h } = this.container;
    if (!w || !h) return;
    this.renderer.setSize(w, h);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    if (!this.box.isEmpty()) {
      // Keep the zoom percentage meaning "relative to fitted" at the new aspect ratio.
      const zoom = THREE.MathUtils.clamp(this.fitDist / this.dist, 0, MAX_ZOOM / 100);
      const sphere = this.box.getBoundingSphere(new THREE.Sphere());
      this.fitDist = fitDistance(sphere.radius, this.camera.fov, this.camera.aspect, FIT_MARGIN);
      this.applyZoomLimits();
      this.dist = this.fitDist / zoom;
      this.placeCamera();
    } else {
      this.changed();
    }
  }

  private changed() {
    this.render();
    if (this.listeners.size === 0) return;
    const s = this.state();
    for (const l of this.listeners) l(s);
  }

  private render() {
    if (this.disposed) return;
    this.updateClipPlanes();
    this.renderer.render(this.scene, this.camera);
  }
}

function partOf(obj: THREE.Object3D): string | undefined {
  for (let o: THREE.Object3D | null = obj; o; o = o.parent) {
    const part = (o.userData as { part?: string }).part;
    if (part) return part;
  }
  return undefined;
}

/** Positions of the triangles whose vertices carry one of `ids` in attribute `attr`. */
function trianglesWhere(geometry: THREE.BufferGeometry, attr: string, ids: Set<number>): number[] {
  const pos = geometry.getAttribute("position");
  const id = geometry.getAttribute(attr);
  if (!pos || !id) return [];
  const index = geometry.getIndex();
  const count = index ? index.count : pos.count;
  const out: number[] = [];
  for (let i = 0; i + 2 < count; i += 3) {
    const a = index ? index.getX(i) : i;
    if (!ids.has(id.getX(a))) continue;
    for (let k = 0; k < 3; k++) {
      const v = index ? index.getX(i + k) : i + k;
      out.push(pos.getX(v), pos.getY(v), pos.getZ(v));
    }
  }
  return out;
}

/** Positions of the line segments whose start vertex carries one of `ids` in attribute `attr`. */
function segmentsWhere(geometry: THREE.BufferGeometry, attr: string, ids: Set<number>): number[] {
  const pos = geometry.getAttribute("position");
  const id = geometry.getAttribute(attr);
  if (!pos || !id) return [];
  const index = geometry.getIndex();
  const count = index ? index.count : pos.count;
  const out: number[] = [];
  for (let i = 0; i + 1 < count; i += 2) {
    const a = index ? index.getX(i) : i;
    if (!ids.has(id.getX(a))) continue;
    for (let k = 0; k < 2; k++) {
      const v = index ? index.getX(i + k) : i + k;
      out.push(pos.getX(v), pos.getY(v), pos.getZ(v));
    }
  }
  return out;
}

function concat(parts: Triangles[]): Triangles {
  const out = new Float64Array(parts.reduce((n, p) => n + p.length, 0));
  let i = 0;
  for (const p of parts) {
    out.set(p, i);
    i += p.length;
  }
  return out;
}

function round2(v: number) {
  const r = Math.round(v * 100) / 100;
  return r === 0 ? 0 : r;
}

function round1(v: number) {
  const r = Math.round(v * 10) / 10;
  return r === 0 ? 0 : r;
}

function disposeObject(root: THREE.Object3D) {
  root.traverse((obj) => {
    const o = obj as THREE.Mesh;
    o.geometry?.dispose();
    const m = o.material;
    if (Array.isArray(m)) m.forEach((x) => x.dispose());
    else m?.dispose();
  });
}
