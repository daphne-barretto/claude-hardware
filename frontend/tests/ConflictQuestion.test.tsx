import { conflictEvents, recordedEvents } from "./fixtures";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import * as THREE from "three";
import { answerQuestion, applyEvent, pinnedQuestion, type ChatMessage } from "@/components/sites/claude-shell/chat";
import { ChatMessages } from "@/components/sites/claude-shell/ChatMessages";
import {
  bestText,
  CONSTRAINT_SOURCE,
  constraintText,
  locationText,
} from "@/components/sites/claude-shell/ConflictCard";
import { projectToCanvas } from "@/components/canvas/viewer";
import { pinCardBox } from "@/components/canvas/HardwareCanvas";
import type { SessionEvent } from "@/lib/session-api";

// Questions and the can't-satisfy state, from the conflict fixture.
type Question = Extract<SessionEvent, { type: "question" }>;
const question = conflictEvents.find((e) => e.type === "question") as Question;
const conflict = question.conflict!;
const play = (events: SessionEvent[], from: ChatMessage[] = []) => events.reduce(applyEvent, from);

afterEach(cleanup);

describe("the conflict fixture", () => {
  it("is a question with a conflict and a location", () => {
    expect(conflict.constraints.length).toBeGreaterThanOrEqual(2);
    expect(question.location).toEqual(conflict.location);
  });
});

describe("chat state", () => {
  it("keeps the question's conflict and location", () => {
    const block = play(conflictEvents)
      .flatMap((m) => m.blocks ?? [])
      .find((b) => b.kind === "question");
    expect(block).toMatchObject({ questionId: question.question_id, conflict, location: question.location });
  });

  it("pins the open question until it is answered", () => {
    const messages = play(conflictEvents);
    expect(pinnedQuestion(messages)).toMatchObject({ questionId: question.question_id, location: question.location });
    expect(pinnedQuestion(answerQuestion(messages, question.question_id, question.options[0].id))).toBeUndefined();
  });

  it("pins nothing for a question without a location", () => {
    expect(pinnedQuestion(play(recordedEvents))).toBeUndefined();
  });

  it("pins only Claude's latest question", () => {
    const messages = play(recordedEvents, play(conflictEvents));
    expect(pinnedQuestion(messages)).toBeUndefined();
    expect(pinnedQuestion(play(conflictEvents.map(renumber), play(recordedEvents)))).toBeDefined();
  });
});

function renumber(e: SessionEvent): SessionEvent {
  return "message_id" in e && e.message_id ? { ...e, message_id: `${e.message_id}-again` } : e;
}

describe("the conflict card in the chat", () => {
  function renderChat(onAnswer = vi.fn(), busy = false) {
    render(<ChatMessages messages={play(conflictEvents)} busy={busy} onAnswer={onAnswer} />);
    return within(screen.getByRole("group", { name: question.text }));
  }

  it("shows every clashing constraint with who set it, its best value and its locations", () => {
    const group = renderChat();
    const items = within(group.getByRole("list", { name: "Clashing constraints" })).getAllByRole("listitem");
    expect(items).toHaveLength(conflict.constraints.length);
    conflict.constraints.forEach((c, i) => {
      const item = within(items[i]);
      expect(items[i].dataset.id).toBe(c.id);
      expect(item.getByTestId("constraint-text").textContent).toBe(constraintText(c));
      expect(items[i].textContent).toContain(CONSTRAINT_SOURCE[c.kind]);
      expect(item.getByTestId("constraint-best").textContent).toBe(bestText(c));
      expect(item.getAllByTestId("constraint-location").map((l) => l.textContent)).toEqual(
        c.locations.map((l) => ` · ${locationText(l)}`),
      );
    });
    expect(group.getByTestId("conflict-over-by").textContent).toContain(`${conflict.over_by} mm apart`);
  });

  it("names constraints as they bind", () => {
    for (const c of conflict.constraints) {
      const text = constraintText(c);
      expect(text).toContain(c.label);
      expect(text).toContain(String(Number(c.value.toFixed(2))));
      if (c.relation === ">=") expect(text).toContain("≥");
      if (c.relation === "<=") expect(text).toContain("≤");
      expect(bestText(c)).toBe(c.best == null ? "can't change" : `best ${Number(c.best.toFixed(2))} ${c.unit}`);
    }
  });

  it("puts the location's part, face and point into words", () => {
    const l = conflict.location;
    const text = locationText(l);
    expect(text).toContain(l.part!);
    expect(text).toContain(l.faces![0]);
    for (const v of l.point) expect(text).toContain(String(Number(v.toFixed(2))));
  });

  it("answers with an option", () => {
    const onAnswer = vi.fn();
    const group = renderChat(onAnswer);
    const buttons = group.getAllByRole("button");
    expect(buttons).toHaveLength(question.options.length);
    fireEvent.click(buttons[1]);
    expect(onAnswer).toHaveBeenCalledWith(question.question_id, question.options[1]);
  });
});

describe("projecting the pin's spot", () => {
  const camera = new THREE.PerspectiveCamera(35, 2, 0.1, 1000);
  camera.position.set(0, 0, 100);
  camera.lookAt(0, 0, 0);
  camera.updateProjectionMatrix();

  it("puts the look-at point in the middle of the canvas", () => {
    expect(projectToCanvas(new THREE.Vector3(0, 0, 0), camera, 800, 400)).toEqual([400, 200]);
  });

  it("puts points up and to the right above and right of the middle", () => {
    const [x, y] = projectToCanvas(new THREE.Vector3(10, 10, 0), camera, 800, 400)!;
    expect(x).toBeGreaterThan(400);
    expect(y).toBeLessThan(200);
  });

  it("hides points behind the camera", () => {
    expect(projectToCanvas(new THREE.Vector3(0, 0, 200), camera, 800, 400)).toBeNull();
  });
});

describe("the pin's card", () => {
  const size: [number, number] = [600, 500];
  const inside = (b: ReturnType<typeof pinCardBox>) =>
    b.left >= 0 && b.left + b.width <= size[0] && b.top >= 0 && b.top + b.maxHeight <= size[1];

  it("opens to the right of a spot on the left and to the left of a spot on the right", () => {
    expect(pinCardBox([100, 200], size).left).toBeGreaterThan(100);
    const right = pinCardBox([500, 200], size);
    expect(right.left + right.width).toBeLessThan(500);
  });

  const spots = [[0, 0], [300, 250], [600, 500], [310, 60], [310, 450], [100, 480]] as [number, number][];

  it("stays inside the canvas wherever the spot is", () => {
    for (const at of [...spots, [-50, 900]] as [number, number][]) expect(inside(pinCardBox(at, size))).toBe(true);
  });

  it("never covers the marker", () => {
    for (const at of spots) {
      const b = pinCardBox(at, size);
      const clear =
        b.left >= at[0] + 12 ||
        b.left + b.width <= at[0] - 12 ||
        b.top >= at[1] + 12 ||
        b.top + b.maxHeight <= at[1] - 12;
      expect(clear, `card ${JSON.stringify(b)} for the spot ${at}`).toBe(true);
    }
  });
});
