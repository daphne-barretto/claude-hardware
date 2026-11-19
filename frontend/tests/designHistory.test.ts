import { describe, expect, it } from "vitest";
import type { Build, DesignHistory, HistoryStep } from "../src/components/canvas/api";
import {
  failedLatest,
  failureText,
  fixFailureText,
  moveText,
  plainError,
  shownBuild,
  stepBlocked,
  stepTitle,
} from "../src/components/canvas/designHistory";
import { sessionOpenApi } from "./fixtures";

// The design timeline (undo and redo over every change, whoever made it) and the build the
// canvas shows: never a failed one, which is reported over the last good model.

const build = (id: string, over: Partial<Build> = {}): Build => ({ id, status: "ready", ...over }) as Build;
const step = (id: string, actor: "user" | "claude", label: string): HistoryStep => ({
  id,
  actor,
  kind: actor === "claude" ? "run" : "edit",
  label,
  text: `${actor === "user" ? "You" : "Claude"}: ${label}`,
  created_at: "2026-01-01T00:00:00Z",
  build_id: null,
  message_id: null,
});
const hand = step("step-1", "user", "push/pull 1 mm");
const run = step("step-2", "claude", "move opening_Q4 by (1, 0) mm and 3 more");
const history = (position: number): DesignHistory => {
  const steps = [hand, run];
  return { steps, position, undo: steps[position - 1] ?? null, redo: steps[position] ?? null };
};

describe("the shown build", () => {
  it("is the latest build with a model when the newest failed, and the failure is reported", () => {
    const good = build("a");
    const bad = build("b", { status: "failed", failure: "the modeler couldn't make the shape with these values" });
    expect(shownBuild([good, bad], null)).toBe(good);
    expect(failedLatest([good, bad], good)).toBe(bad);
    expect(failureText(bad)).toBe("This change couldn't be built: the modeler couldn't make the shape with these values.");
  });

  it("is the picked build, unless that one failed", () => {
    const [a, b] = [build("a"), build("b")];
    const bad = build("c", { status: "failed" });
    expect(shownBuild([a, b], "a")).toBe(a);
    expect(shownBuild([a, bad], "c")).toBe(a);
    expect(failedLatest([a, b], b)).toBeUndefined();
  });

  it("names a failed build's reason plainly even without one", () => {
    expect(failureText(build("x", { status: "failed", failure: null }))).toBe(
      "This change couldn't be built: the modeler couldn't make the shape.",
    );
  });

  it("asks Claude to fix the change the timeline names", () => {
    const text = fixFailureText(build("x", { failure: "it took too long to build" }), history(1));
    expect(text).toContain("(You: push/pull 1 mm) couldn't be built: it took too long to build");
    expect(text).not.toMatch(/build-/);
  });
});

describe("undo and redo", () => {
  it("are off while Claude works or its question waits, and when there is nothing to move over", () => {
    expect(stepBlocked(history(2), "undo", { busy: true, stepping: false })).toBe("Claude is working");
    expect(stepBlocked(history(2), "undo", { busy: false, blocked: "Answer Claude's question first.", stepping: false })).toBe(
      "Answer Claude's question first.",
    );
    expect(stepBlocked(history(2), "redo", { busy: false, stepping: false })).toBe("Nothing to redo");
    expect(stepBlocked(history(0), "undo", { busy: false, stepping: false })).toBe("Nothing to undo");
    expect(stepBlocked(history(1), "undo", { busy: false, stepping: false })).toBeUndefined();
    expect(stepBlocked(history(1), "redo", { busy: false, stepping: true })).toBe("Redoing");
  });

  it("say which step they move over, whoever made it", () => {
    expect(stepTitle(history(2), "undo", undefined)).toBe("Undo: Claude: move opening_Q4 by (1, 0) mm and 3 more (⌘Z)");
    expect(stepTitle(history(1), "redo", undefined)).toBe("Redo: Claude: move opening_Q4 by (1, 0) mm and 3 more (⇧⌘Z)");
    expect(moveText({ direction: "undo", step: hand })).toBe("Undid: You: push/pull 1 mm");
    expect(moveText({ direction: "redo", step: run })).toBe("Redid: Claude: move opening_Q4 by (1, 0) mm and 3 more");
  });
});

describe("errors the user sees", () => {
  it("never show build IDs or internal error text", () => {
    const fallback = "Couldn't undo that step. Try again.";
    expect(plainError("build build-8f4aa failed", fallback)).toBe(fallback);
    expect(plainError("RuntimeError: Standard_ConstructionError", fallback)).toBe(fallback);
    expect(plainError("500 Internal Server Error", fallback)).toBe(fallback);
    expect(plainError("Couldn't rebuild the design as it was before that step, so nothing changed.", fallback)).toBe(
      "Couldn't rebuild the design as it was before that step, so nothing changed.",
    );
  });
});

describe("the contract", () => {
  it("serves the timeline and a build's plain failure", () => {
    const api = sessionOpenApi;
    expect(api.paths["/api/sessions/{session_id}/history"].get).toBeTruthy();
    expect(api.paths["/api/sessions/{session_id}/history/undo"].post).toBeTruthy();
    expect(api.paths["/api/sessions/{session_id}/history/redo"].post).toBeTruthy();
    const schemas = api.components.schemas;
    expect(Object.keys(schemas.HistoryStep.properties).sort()).toEqual(Object.keys(hand).sort());
    expect(Object.keys(schemas.DesignHistory.properties).sort()).toEqual(["position", "redo", "steps", "undo"]);
    expect(schemas.Build.properties.failure).toBeTruthy();
  });
});
