import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import type { FindResult } from "@/components/canvas/api";
import { indexFromGltf } from "@/components/canvas/selection";
import { applyEvent, latestFound, type ChatMessage } from "@/components/sites/claude-shell/chat";
import { ChatMessages } from "@/components/sites/claude-shell/ChatMessages";
import { EVENT_TYPES, type SessionEvent } from "@/lib/session-api";
import { readRepoFile } from "./fixtures";

// Find in model from chat: the `found` event Claude's find_in_model tool sends. The
// matches are made from the fixture mesh's faces; no scenario values are written here.
const schema = JSON.parse(readRepoFile("contracts/session-events.schema.json"));
const index = indexFromGltf(JSON.parse(readRepoFile("fixtures/enclosure.gltf")));
const faces = index.flatMap((p) => p.faces.map((f) => ({ ...f, part: p.part })));
const features = [...new Set(faces.map((f) => f.feature))].slice(0, 2);
const matches = faces
  .filter((f) => features.includes(f.feature))
  .map((f) => ({ face: f.id, part: f.part, feature: f.feature, value: 0.5, unit: "deg" as const }));
const found: FindResult = { query: "faces under 1° of draft", status: "found", understood: "Faces with draft under 1°", matches };
const unclear: FindResult = {
  query: "purple widgets",
  status: "unclear",
  understood: "Nothing I can search for",
  question: "I don't know “purple” on this model. Try a feature type.",
  matches: [],
};

function reply(result: FindResult): ChatMessage[] {
  const events: SessionEvent[] = [
    { type: "message_start", message_id: "msg-2", role: "assistant" },
    { type: "found", message_id: "msg-2", build_id: "build-1", result },
    { type: "text", message_id: "msg-2", delta: "Here they are." },
    { type: "message_end", message_id: "msg-2" },
  ];
  return events.reduce(applyEvent, [] as ChatMessage[]);
}

afterEach(cleanup);

describe("found events", () => {
  it("are a session event type in the contract and the client", () => {
    expect(EVENT_TYPES).toContain("found");
    const text = JSON.stringify(schema);
    expect(text).toContain('"found"');
    expect(text).toContain("FoundEvent");
  });

  it("become a block in Claude's reply, before its text, and the latest find", () => {
    const messages = reply(found);
    const blocks = messages[0].blocks!;
    expect(blocks.map((b) => b.kind)).toEqual(["found", "text"]);
    expect(blocks[0]).toEqual({ kind: "found", buildId: "build-1", result: found });
    expect(latestFound(messages)?.block.result).toBe(found);
    expect(latestFound(reply(unclear))?.key).toBeDefined();
    expect(latestFound([])).toBeUndefined();
  });
});

describe("the found card", () => {
  it("lists the matches by feature and sends them to the canvas to highlight or select", () => {
    const onFound = vi.fn();
    render(<ChatMessages messages={reply(found)} busy={false} onAnswer={() => {}} onFound={onFound} />);
    const card = screen.getByRole("region", { name: "Found in model" });
    expect(within(card).getByTestId("found-understood").textContent).toContain(found.understood);
    const rows = within(within(card).getByRole("list", { name: "Matches" })).getAllByRole("listitem");
    expect(rows.map((r) => r.getAttribute("data-feature"))).toEqual(features);
    fireEvent.click(within(card).getByRole("button", { name: "Show on model" }));
    fireEvent.click(within(card).getByRole("button", { name: "Use as selection" }));
    expect(onFound.mock.calls.map(([block, select]) => [block.result, select])).toEqual([
      [found, false],
      [found, true],
    ]);
  });

  it("shows what was understood and the question for an unclear query, with nothing to select", () => {
    render(<ChatMessages messages={reply(unclear)} busy={false} onAnswer={() => {}} onFound={() => {}} />);
    const card = screen.getByRole("region", { name: "Found in model" });
    expect(card.getAttribute("data-status")).toBe("unclear");
    expect(within(card).getByTestId("found-understood").textContent).toContain(unclear.understood);
    expect(within(card).getByTestId("found-question").textContent).toBe(unclear.question);
    expect(within(card).queryByRole("button")).toBeNull();
  });

  it("has no controls without a canvas", () => {
    render(<ChatMessages messages={reply(found)} busy={false} onAnswer={() => {}} />);
    expect(within(screen.getByRole("region", { name: "Found in model" })).queryByRole("button")).toBeNull();
  });
});
