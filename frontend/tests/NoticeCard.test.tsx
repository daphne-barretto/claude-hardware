// Refusals and safety notices in the chat and on the canvas header.
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import { applyEvent, type ChatMessage } from "@/components/sites/claude-shell/chat";
import { ChatMessages } from "@/components/sites/claude-shell/ChatMessages";
import { REFUSED_HEADING, SAFETY_HEADING, SafetyBadge, safetyNotices } from "@/components/sites/claude-shell/NoticeCard";
import { EVENT_TYPES, type SessionEvent } from "@/lib/session-api";

const REGULATIONS = ["toy and children's product safety rules", "food-contact material regulations"];
const SAFETY = "This looks like a toy. Requirements vary by region, and yours isn't known. None of them were checked.";

function play(events: SessionEvent[]): ChatMessage[] {
  return events.reduce<ChatMessage[]>((m, e) => applyEvent(m, e), []);
}

afterEach(cleanup);

describe("notices", () => {
  it("are a session event type the stream listens for", () => {
    expect(EVENT_TYPES).toContain("notice");
  });

  it("show a refused run as a distinct won't-design-this notice with its reason, not an error", () => {
    const messages = play([
      { type: "message_start", message_id: "msg-1", role: "assistant" },
      { type: "text", message_id: "msg-1", delta: "I won't design that. Happy to work on something else." },
      { type: "progress", message_id: "msg-1", text: "Declined the request" },
      { type: "notice", message_id: "msg-1", kind: "refused", text: "It is a part made to bypass a safety interlock." },
      { type: "message_end", message_id: "msg-1" },
    ]);
    render(<ChatMessages messages={messages} busy={false} onAnswer={() => {}} />);
    const notice = screen.getByRole("note", { name: REFUSED_HEADING });
    expect(notice.textContent).toContain(REFUSED_HEADING);
    expect(notice.textContent).toContain("bypass a safety interlock");
    expect(notice.textContent).toMatch(/nothing was designed or changed/i);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByRole("note", { name: SAFETY_HEADING })).toBeNull();
  });

  it("show a safety notice with the regulations that may apply, none checked, and the run goes on", () => {
    const messages = play([
      { type: "message_start", message_id: "msg-1", role: "assistant" },
      { type: "notice", message_id: "msg-1", kind: "safety", text: SAFETY, categories: ["a toy"], regulations: REGULATIONS, region: null },
      { type: "progress", message_id: "msg-1", text: "Writing the design" },
    ]);
    expect(messages[0].blocks?.map((b) => b.kind)).toEqual(["notice", "progress"]);
    render(<ChatMessages messages={messages} busy onAnswer={() => {}} />);
    const notice = screen.getByRole("note", { name: SAFETY_HEADING });
    expect(notice.textContent).toContain(SAFETY);
    expect(notice.textContent).toMatch(/may apply \(none were checked\)/i);
    const items = within(within(notice).getByRole("list", { name: "Regulations that may apply" })).getAllByRole("listitem");
    expect(items.map((li) => li.textContent)).toEqual(REGULATIONS);
    expect(screen.queryByRole("note", { name: REFUSED_HEADING })).toBeNull();
  });

  it("put a badge on the canvas header once a session has a safety notice", () => {
    const notice = { type: "notice", message_id: "msg-1", kind: "safety", text: SAFETY, regulations: REGULATIONS } as const;
    const messages = play([{ type: "message_start", message_id: "msg-1", role: "assistant" }, notice, { ...notice, message_id: "msg-1" }]);
    const notices = safetyNotices(messages);
    expect(notices).toHaveLength(1); // the same notice once
    render(<SafetyBadge notices={notices} />);
    const badge = screen.getByRole("note", { name: SAFETY_HEADING });
    expect(badge.getAttribute("title")).toContain(`May apply: ${REGULATIONS[0]}`);
    expect(badge.getAttribute("title")).toMatch(/none were checked/);
    cleanup();
    const refused = play([{ type: "notice", message_id: "msg-2", kind: "refused", text: "No." }]);
    const { container } = render(<SafetyBadge notices={safetyNotices(refused)} />);
    expect(container.textContent).toBe("");
  });
});
