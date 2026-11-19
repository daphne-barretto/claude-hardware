import { readRepoFile } from "./fixtures";
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import { applyEvent, type ChatMessage } from "@/components/sites/claude-shell/chat";
import { ChatMessages } from "@/components/sites/claude-shell/ChatMessages";
import { EVENT_TYPES, type ChangeSet } from "@/lib/session-api";

// A run's proposal (`changes_ready`) shows in the chat: every change with its proof.
const changeSet = JSON.parse(readRepoFile("fixtures/change-set.json")) as ChangeSet;

afterEach(cleanup);

describe("a proposal in the chat", () => {
  it("is subscribed to", () => {
    expect(EVENT_TYPES).toContain("changes_ready");
  });

  it("lists every proposed change of the run's message", () => {
    let messages: ChatMessage[] = [];
    messages = applyEvent(messages, { type: "message_start", message_id: "m-1", role: "assistant" });
    messages = applyEvent(messages, { type: "changes_ready", message_id: "m-1", change_set: changeSet });
    messages = applyEvent(messages, { type: "message_end", message_id: "m-1" });
    expect(messages).toHaveLength(1);
    expect(messages[0].blocks).toEqual([{ kind: "changes", changeSet }]);

    render(<ChatMessages messages={messages} busy={false} onAnswer={() => {}} />);
    const list = screen.getByRole("list", { name: "Proposed changes" });
    const items = within(list).getAllByRole("listitem");
    expect(items).toHaveLength(changeSet.changes.length);
    changeSet.changes.forEach((c, i) => expect(items[i].textContent).toContain(c.title));
    const n = changeSet.changes.length;
    expect(screen.getByText(`${n} proposed ${n === 1 ? "change" : "changes"}`)).toBeTruthy();
  });
});
