import { readFileSync } from "node:fs";
import path from "node:path";
import * as THREE from "three";
import { describe, expect, it } from "vitest";
import { resolveBuild, withBuild, type Build } from "@/components/canvas/api";
import { formatLength, formatSize, fromMm, MM_PER_INCH } from "@/components/canvas/units";
import { cssRotation } from "@/components/canvas/ViewCube";
import {
  clipPlanes,
  fitDistance,
  matchView,
  MAX_ZOOM,
  MIN_MODEL_PX,
  minZoomFor,
  viewDirection,
  VIEW_NAMES,
} from "@/components/canvas/viewer";

// Read at run time, not imported: the image build type-checks frontend/ without the repo's fixtures/.
const fixtureBuild = JSON.parse(
  readFileSync(path.join(__dirname, "../../fixtures/build.json"), "utf8"),
) as Build;

describe("units", () => {
  it("converts millimetres to inches for display only", () => {
    expect(fromMm(MM_PER_INCH, "in")).toBe(1);
    expect(fromMm(12.5, "mm")).toBe(12.5);
    expect(formatLength(MM_PER_INCH * 2, "in")).toBe("2.000 in");
    expect(formatLength(3.14159, "mm")).toBe("3.1 mm");
    expect(formatSize([MM_PER_INCH, MM_PER_INCH / 2, 0], "in")).toBe("1.000 × 0.500 × 0.000 in");
  });
});

describe("views", () => {
  it("has a distinct unit direction per named view, and recognises each", () => {
    for (const v of VIEW_NAMES) {
      expect(viewDirection(v).length()).toBeCloseTo(1);
      expect(matchView(viewDirection(v).multiplyScalar(42))).toBe(v);
    }
    expect(matchView(new THREE.Vector3(0.3, 0.2, 1))).toBe("custom");
  });

  it("maps board axes to three.js: front looks along board +y, top down board z", () => {
    // Board (x, y, z) is three.js (x, z, −y): the front camera sits at board −y = three.js +z.
    expect(viewDirection("front").toArray()).toEqual([0, 0, 1]);
    expect(viewDirection("top").y).toBeCloseTo(1);
    expect(viewDirection("right").x).toBe(1);
  });
});

describe("fitDistance", () => {
  it("puts a bounding sphere fully inside the frustum for wide and tall viewports", () => {
    for (const aspect of [0.5, 1, 2.5]) {
      const radius = 40;
      const camera = new THREE.PerspectiveCamera(35, aspect, 0.1, 10_000);
      const distance = fitDistance(radius, camera.fov, aspect);
      camera.position.set(0, 0, distance);
      camera.lookAt(0, 0, 0);
      camera.updateMatrixWorld();
      for (const p of [
        [radius, 0, 0],
        [-radius, 0, 0],
        [0, radius, 0],
        [0, -radius, 0],
      ] as const) {
        const ndc = new THREE.Vector3(...p).project(camera);
        expect(Math.abs(ndc.x)).toBeLessThan(1);
        expect(Math.abs(ndc.y)).toBeLessThan(1);
      }
      // ...and not far away: without the margin the sphere just touches the frustum.
      expect(fitDistance(radius, camera.fov, aspect, 1)).toBeLessThan(distance);
    }
  });
});

describe("zoom limits and clip planes", () => {
  it("stops zooming out when the model's bounding sphere is MIN_MODEL_PX across", () => {
    for (const [w, h] of [
      [1200, 800],
      [600, 1000],
      [320, 480],
    ]) {
      const aspect = w / h;
      const radius = 40;
      const fov = 35;
      const fitted = fitDistance(radius, fov, aspect);
      const distance = (fitted * 100) / minZoomFor(radius, fov, w, h);
      // Apparent diameter of the sphere in pixels along the viewport's smaller side.
      const vfov = THREE.MathUtils.degToRad(fov);
      const halfAngle = Math.asin(radius / distance);
      const pxPerTan = h / 2 / Math.tan(vfov / 2);
      expect(2 * Math.tan(halfAngle) * pxPerTan).toBeCloseTo(MIN_MODEL_PX, 0);
    }
    expect(minZoomFor(40, 35, 0, 0)).toBe(100);
    expect(minZoomFor(40, 35, 10, 10)).toBe(100);
    expect(minZoomFor(40, 35, 1200, 800)).toBeLessThan(MAX_ZOOM);
  });

  it("keeps the whole scene sphere between near and far at every distance", () => {
    const radius = 50;
    for (const d of [5, 30, 60, 200, 1_000, 10_000, 100_000]) {
      const { near, far } = clipPlanes(d, radius);
      expect(near).toBeGreaterThan(0);
      expect(far).toBeGreaterThan(d + radius);
      if (d > radius * 1.1) expect(near).toBeLessThan(d - radius);
      // Depth precision: far/near stays bounded instead of growing with the distance.
      expect(far / near).toBeLessThan(Math.max(1e4, (2 * d) / radius + 100));
    }
  });
});

describe("view cube", () => {
  it("shows the front face square-on in the front view and flips y for CSS", () => {
    const camera = new THREE.PerspectiveCamera();
    camera.position.copy(viewDirection("front").multiplyScalar(10));
    camera.lookAt(0, 0, 0);
    camera.updateMatrixWorld();
    const rotation = new THREE.Matrix4().extractRotation(camera.matrixWorldInverse).elements;
    expect(cssRotation(rotation)).toBe("matrix3d(1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1)");

    camera.position.copy(viewDirection("top").multiplyScalar(10));
    camera.lookAt(0, 0, 0);
    camera.updateMatrixWorld();
    const top = new THREE.Matrix4().extractRotation(camera.matrixWorldInverse).elements;
    const css = cssRotation(top).match(/matrix3d\((.*)\)/)![1].split(",").map(Number);
    // The CSS normal of the "top" face (rotateX(90deg): (0, −1, 0)) must face the viewer (+z).
    const normal = new THREE.Vector3(0, -1, 0).applyMatrix4(new THREE.Matrix4().fromArray(css));
    expect(normal.z).toBeCloseTo(1, 3);
  });
});

describe("builds", () => {
  it("resolves the fixture build's relative URLs against the session", () => {
    const b = resolveBuild("session-1", fixtureBuild);
    expect(b.mesh_url).toBe(`/api/sessions/session-1/${fixtureBuild.mesh_url}`);
    expect(b.checks_url).toBe(`/api/sessions/session-1/${fixtureBuild.checks_url}`);
    expect(resolveBuild("session-1", b)).toEqual(b);
  });

  it("adds a new build last and replaces one with the same ID", () => {
    const other = { ...fixtureBuild, id: `${fixtureBuild.id}-next` };
    const list = withBuild(withBuild([], fixtureBuild), other);
    expect(list.map((b) => b.id)).toEqual([fixtureBuild.id, other.id]);
    expect(withBuild(list, fixtureBuild).map((b) => b.id)).toEqual([other.id, fixtureBuild.id]);
  });
});
