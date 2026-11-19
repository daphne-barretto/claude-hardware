import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { ChatMessages } from "@/components/sites/claude-shell/ChatMessages";
import type { ChatMessage } from "@/components/sites/claude-shell/chat";
import { SHARED_BLOCKED } from "@/lib/share";

// A read-only session (a shared link) shows Claude's questions without their options:
// answering would be refused (409), so it says why instead.
const options = [
  { id: "accept", label: "Accept", description: "Apply the changes and rebuild" },
  { id: "reject", label: "Reject", description: "Keep the current design" },
];
const asked = (answer?: string): ChatMessage[] => [
  {
    id: "m-1",
    role: "assistant",
    text: "",
    blocks: [{ kind: "question", questionId: "q-1", text: "Apply these changes?", options, answer }],
  },
];

afterEach(cleanup);

describe("a question in a read-only session", () => {
  it("has no options to press, and says why", () => {
    const onAnswer = vi.fn();
    render(<ChatMessages messages={asked()} busy={false} onAnswer={onAnswer} readOnly={SHARED_BLOCKED} />);
    expect(screen.queryByRole("button", { name: /Accept|Reject/ })).toBeNull();
    expect(screen.getByTestId("question-read-only").textContent).toContain(SHARED_BLOCKED);
  });

  it("still says what was chosen", () => {
    render(<ChatMessages messages={asked("accept")} busy={false} onAnswer={() => {}} readOnly={SHARED_BLOCKED} />);
    expect(screen.getByTestId("question-read-only").textContent).toBe(`Answered: Accept. ${SHARED_BLOCKED}.`);
  });

  it("keeps its options in the user's own session", () => {
    render(<ChatMessages messages={asked()} busy={false} onAnswer={() => {}} />);
    expect(screen.getByRole("button", { name: /Accept/ })).toBeTruthy();
    expect(screen.queryByTestId("question-read-only")).toBeNull();
  });
});
