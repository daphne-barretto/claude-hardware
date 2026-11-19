import { recordedEvents } from "./fixtures";
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import type { ChatMessage } from "@/components/sites/claude-shell/chat";
import { ChatMessages } from "@/components/sites/claude-shell/ChatMessages";
import type { SessionEvent } from "@/lib/session-api";

// Disabled question options say why they are disabled.
const question = recordedEvents.find((e) => e.type === "question") as Extract<SessionEvent, { type: "question" }>;

function renderQuestion({ busy, answer }: { busy: boolean; answer?: string }) {
  const messages: ChatMessage[] = [
    {
      id: "m-1",
      role: "assistant",
      blocks: [{ kind: "question", questionId: question.question_id, text: question.text, options: question.options, answer }],
    } as ChatMessage,
  ];
  render(<ChatMessages messages={messages} busy={busy} onAnswer={() => {}} />);
  return within(screen.getByRole("group", { name: question.text })).getAllByRole("button");
}

/** The text of the elements a control's aria-describedby points at. */
function reason(el: HTMLElement) {
  return (el.getAttribute("aria-describedby") ?? "")
    .split(/\s+/)
    .filter(Boolean)
    .map((id) => document.getElementById(id)?.textContent?.trim() ?? "")
    .join(" ")
    .trim();
}

afterEach(cleanup);

describe("question options", () => {
  it("are enabled with no reason while waiting for an answer", () => {
    for (const b of renderQuestion({ busy: false })) {
      expect((b as HTMLButtonElement).disabled).toBe(false);
      expect(reason(b)).toBe("");
    }
  });

  it("say Claude is still replying while the reply streams", () => {
    const buttons = renderQuestion({ busy: true });
    expect(buttons).toHaveLength(question.options.length);
    for (const b of buttons) {
      expect((b as HTMLButtonElement).disabled).toBe(true);
      expect(reason(b)).toMatch(/still replying/i);
    }
  });

  it("say the question was answered after answering, and show the chosen one as chosen and disabled", () => {
    const chosen = question.options[1];
    const buttons = renderQuestion({ busy: false, answer: chosen.id });
    for (const b of buttons) {
      expect((b as HTMLButtonElement).disabled).toBe(true);
      expect(reason(b)).toMatch(/answered/i);
    }
    const picked = buttons.find((b) => b.getAttribute("aria-pressed") === "true")!;
    expect(picked.textContent).toContain(chosen.label);
    expect(picked.className).toMatch(/disabled:cursor-not-allowed/);
    // Answered takes precedence over a reply that is still streaming.
    cleanup();
    for (const b of renderQuestion({ busy: true, answer: chosen.id })) expect(reason(b)).toMatch(/answered/i);
  });
});

// The constraints live in the canvas's Constraints bar; a message whose words set one gets a
// one-line note under it, so the chat still says what it took from them.
describe("constraint note", () => {
  const base = { kind: "min", unit: "mm", check: "c", quote: "q", standard_value: 1 } as const;
  const mine = { ...base, id: "a", title: "A", label: "A ≥ 2 mm", value: 2, source: "user", message_id: "u-1" } as const;
  const other = { ...base, id: "b", title: "B", label: "B ≥ 3 mm", value: 3, source: "user", message_id: "u-2" } as const;
  const set = { ...base, id: "c", title: "C", label: "C ≥ 1 mm", value: 1, source: "standard", quote: null, message_id: null } as const;
  const messages = [
    { id: "u-1", role: "user", text: "first" },
    { id: "u-2", role: "user", text: "second" },
  ] as ChatMessage[];

  it("names the constraints a message's words set, under that message only", () => {
    render(<ChatMessages messages={messages} busy={false} onAnswer={() => {}} constraints={[mine, other, set]} />);
    const notes = screen.getAllByTestId("constraint-note");
    expect(notes.map((n) => n.textContent)).toEqual(["Constraint from your words: A ≥ 2 mm", "Constraint from your words: B ≥ 3 mm"]);
  });

  it("lists three or more with the Oxford comma", () => {
    const three = ["x", "y", "z"].map((id) => ({ ...mine, id, label: id.toUpperCase() }));
    render(<ChatMessages messages={messages.slice(0, 1)} busy={false} onAnswer={() => {}} constraints={three} />);
    expect(screen.getByTestId("constraint-note").textContent).toBe("Constraints from your words: X, Y, and Z");
  });
});
