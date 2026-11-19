import { recordedEvents } from "./fixtures";
import { FakeEventSource } from "./fakes";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { AppShell } from "@/components/sites/claude-shell/AppShell";
import { Composer, selectionLabel } from "@/components/sites/claude-shell/Composer";
import { DEFAULT_CHOICES, EFFORTS, MODELS } from "@/components/sites/claude-shell/data";
import { HardwareWorkspace, useCanvasSelection, useSetCanvasSelection } from "@/components/sites/claude-shell/HardwareWorkspace";
import type { Selection, SessionEvent } from "@/lib/session-api";

// These tests choose Manual mode, which is behind a flag (lib/flags).
vi.mock("@/lib/flags", async (real) => ({ ...(await real<object>()), MANUAL_MODE: true }));
vi.mock("next/navigation", () => ({ usePathname: () => "/new", useRouter: () => ({ push: () => {} }) }));

const recorded = recordedEvents;
const question = recorded.find((e) => e.type === "question") as Extract<SessionEvent, { type: "question" }>;

let posted: { url: string; body: Record<string, unknown> }[];

beforeEach(() => {
  posted = [];
  FakeEventSource.reset();
  window.history.replaceState(null, "", "/new");
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      // Reads: no sessions, messages or builds yet.
      if (!init?.body) return new Response(url.endsWith("/sessions") ? "[]" : JSON.stringify(url.match(/\/(messages|builds)$/) ? [] : { id: "s-1", title: "", status: "idle" }));
      const body = JSON.parse(String(init.body));
      posted.push({ url, body });
      const json = url === "/api/sessions" ? { id: "s-1", status: "idle" } : { id: `m-${posted.length}`, ...body };
      return new Response(JSON.stringify(json), { status: url === "/api/sessions" ? 201 : 202 });
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const selection: Selection = { part: "lid", faces: ["lid.face_1"], edges: [] };

const FEATURE = "feature_of_face";

// A multi-selection of every kind (made-up IDs; the fixture mesh is covered in selection.test.ts).
const multi: Selection = {
  part: null,
  parts: ["part_a"],
  components: ["REF1", "REF2"],
  faces: ["part_b.face_1", "part_b.face_2"],
  edges: ["part_b.edge_1"],
  point: null,
};
const MULTI_LABEL = "1 part, 2 components, 2 faces, 1 edge";

/** Stands in for the canvas: a click selects a face, and it shows what the chat will send. */
function SelectionProbe() {
  const setSelection = useSetCanvasSelection();
  const current = useCanvasSelection();
  return (
    <>
      <button type="button" onClick={() => setSelection(selection)}>
        Select a face
      </button>
      <button type="button" onClick={() => setSelection(selection, FEATURE)}>
        Select a labelled face
      </button>
      <button type="button" onClick={() => setSelection(multi, MULTI_LABEL)}>
        Select several things
      </button>
      <span data-testid="probe-selection">{current ? current.faces.join(",") : "none"}</span>
    </>
  );
}

function renderWorkspace() {
  render(
    <AppShell>
      <HardwareWorkspace canvas={<SelectionProbe />} />
    </AppShell>,
  );
  return screen.getByRole("region", { name: "Chat" });
}

const messagesPosted = () => posted.filter((p) => p.url.endsWith("/messages"));

async function sendText(chat: HTMLElement, text: string) {
  fireEvent.change(within(chat).getByRole("textbox", { name: "Message" }), { target: { value: text } });
  fireEvent.click(within(chat).getByRole("button", { name: "Send" }));
  await waitFor(() => expect(messagesPosted().length).toBeGreaterThan(0));
}

describe("chat panel", () => {
  it("sends the model, effort and Manual/Auto choices with the next message", async () => {
    const chat = renderWorkspace();
    const model = MODELS.find((m) => m.id !== DEFAULT_CHOICES.model)!;
    const effort = EFFORTS.find((e) => e.id !== DEFAULT_CHOICES.effort)!;

    within(chat).getByRole("radio", { name: "Manual" }).click();
    fireEvent.click(within(chat).getByRole("button", { expanded: false, name: /Opus|Fable|Sonnet|Haiku/ }));
    fireEvent.click(within(chat).getByRole("menuitemradio", { name: new RegExp(model.name) }));
    fireEvent.click(within(chat).getByRole("button", { expanded: false, name: /Opus|Fable|Sonnet|Haiku/ }));
    fireEvent.click(within(chat).getByRole("menuitem", { name: /Effort/ }));
    fireEvent.click(within(chat).getByRole("menuitemradio", { name: new RegExp(`^${effort.name}`) }));
    expect(within(chat).getByRole("radio", { name: "Manual" })).toHaveProperty("ariaChecked", "true");

    await sendText(chat, "Check the enclosure");
    expect(posted[0].url).toBe("/api/sessions");
    expect(window.location.pathname).toBe("/session/s-1");
    expect(messagesPosted()[0]).toEqual({
      url: "/api/sessions/s-1/messages",
      body: { text: "Check the enclosure", model: model.id, effort: effort.id, mode: "manual", selection: null, attachments: [] },
    });
    expect(FakeEventSource.all[0].url).toBe("/api/sessions/s-1/events");
  });

  it("streams progress and text, then answers the question with an option button", async () => {
    const chat = renderWorkspace();
    await sendText(chat, "Check the enclosure");
    const source = FakeEventSource.all[0];

    const asking = recorded.indexOf(question);
    act(() => [...recorded.slice(0, asking), ...recorded.slice(asking + 1)].forEach((e) => source.emit(e)));
    const progress = recorded.filter((e) => e.type === "progress").map((e) => e.text);
    expect(within(chat).getAllByTestId("progress-line").map((p) => p.textContent)).toEqual(progress);

    act(() => source.emit(question));
    const option = within(chat).getByRole("button", { name: new RegExp(question.options[0].label) });
    expect(option).toHaveProperty("disabled", false);
    fireEvent.click(within(chat).getByRole("radio", { name: "Manual" }));
    fireEvent.click(option);

    await waitFor(() => expect(messagesPosted()).toHaveLength(2));
    expect(messagesPosted()[1].body).toEqual({
      text: "",
      ...DEFAULT_CHOICES,
      mode: "manual",
      answer: { question_id: question.question_id, option_id: question.options[0].id },
    });
    expect(option).toHaveProperty("ariaPressed", "true");
    for (const other of question.options.slice(1)) {
      expect(within(chat).getByRole("button", { name: new RegExp(other.label) })).toHaveProperty("disabled", true);
    }
    // The answer shows as the user's reply; only one session and stream were opened.
    expect(within(chat).getAllByText(question.options[0].label).length).toBeGreaterThan(1);
    expect(posted.filter((p) => p.url === "/api/sessions")).toHaveLength(1);
  });

  it("answers a live run's question while the run waits, and is free again when it ends", async () => {
    // The real session API pauses the run on the question without ending the message, and the
    // answer resumes that same message: no new message_start.
    const chat = renderWorkspace();
    await sendText(chat, "Check the enclosure");
    const source = FakeEventSource.last!;
    const id = "live-1";
    act(() => {
      source.emit({ type: "message_start", message_id: id, role: "assistant" });
      source.emit({ type: "progress", message_id: id, text: "Running the checks" });
      source.emit({ ...question, message_id: id });
    });
    const option = within(chat).getByRole("button", { name: new RegExp(question.options[0].label) });
    expect(option).toHaveProperty("disabled", false);
    expect(within(chat).getByTestId("assistant-message").getAttribute("aria-busy")).toBeNull();

    // While the question is open, the composer can't send text and says why (the API answers 409).
    const send = () => within(chat).getByRole("button", { name: "Send" });
    fireEvent.change(within(chat).getByRole("textbox", { name: "Message" }), { target: { value: "Something else" } });
    expect(send()).toHaveProperty("disabled", true);
    expect(send().getAttribute("title")).toBe("Answer Claude's question first");
    fireEvent.keyDown(within(chat).getByRole("textbox", { name: "Message" }), { key: "Enter" });
    expect(messagesPosted()).toHaveLength(1);

    fireEvent.click(option);
    await waitFor(() => expect(messagesPosted()).toHaveLength(2));
    act(() => source.emit({ type: "text", message_id: id, delta: "Applying it." }));
    // The continuation shows below the answer, not above it in the paused reply.
    const items = within(chat).getAllByTestId(/^(user|assistant)-message$/);
    expect(items.map((el) => el.dataset.testid)).toEqual(["user-message", "assistant-message", "user-message", "assistant-message"]);
    expect(items[2].textContent).toContain(question.options[0].label);
    expect(items[3].textContent).toContain("Applying it.");
    expect(items[1].textContent).not.toContain("Applying it.");
    expect(items[3].getAttribute("aria-busy")).toBe("true");
    act(() => source.emit({ type: "message_end", message_id: id }));
    expect(within(chat).getAllByTestId("assistant-message")[1].getAttribute("aria-busy")).toBeNull();
    fireEvent.change(within(chat).getByRole("textbox", { name: "Message" }), { target: { value: "Next" } });
    fireEvent.change(within(chat).getByRole("textbox", { name: "Message" }), { target: { value: "Next" } });
    expect(send()).toHaveProperty("disabled", false);
  });

  it("sends the canvas selection with the next message and shows it as a chip", async () => {
    const chat = renderWorkspace();
    fireEvent.click(screen.getByRole("button", { name: "Select a face" }));
    expect(within(chat).getByTestId("selection-chip").textContent).toBe(selectionLabel(selection));

    await sendText(chat, "Make this thicker");
    expect(messagesPosted()[0].body.selection).toEqual(selection);
    // Sent: the chip moves to the message and the composer is clear for the next one.
    expect(within(chat).getAllByTestId("selection-chip")).toHaveLength(1);
    expect(within(chat).queryByRole("button", { name: "Clear selection" })).toBeNull();
  });

  it("shows the canvas's label (the face's feature) on the chip and sends the face ID", async () => {
    const chat = renderWorkspace();
    fireEvent.click(screen.getByRole("button", { name: "Select a labelled face" }));
    const chip = within(chat).getByTestId("selection-chip");
    expect(chip.textContent).toBe(FEATURE);
    expect(chip.getAttribute("title")).toContain(selection.faces[0]);
    expect(screen.getByTestId("probe-selection").textContent).toBe(selection.faces.join(","));

    await sendText(chat, "Why is this here?");
    expect(messagesPosted()[0].body.selection).toEqual(selection);
    // The sent message keeps the label; the canvas sees the selection consumed.
    expect(within(chat).getByTestId("selection-chip").textContent).toBe(FEATURE);
    expect(screen.getByTestId("probe-selection").textContent).toBe("none");
  });

  it("a multi-selection is one chip that lists every item, and the agent gets every ID", async () => {
    const chat = renderWorkspace();
    fireEvent.click(screen.getByRole("button", { name: "Select several things" }));
    const chips = within(chat).getAllByTestId("selection-chip");
    expect(chips).toHaveLength(1);
    expect(within(chips[0]).getByTestId("selection-label").textContent).toBe(MULTI_LABEL);
    const items = within(chips[0]).getAllByRole("listitem").map((li) => li.textContent);
    const ids = [...multi.parts!, ...multi.components!, ...multi.faces, ...multi.edges];
    expect(items).toEqual(ids);

    await sendText(chat, "Move these");
    expect(messagesPosted()[0].body.selection).toEqual(multi);
    // The sent message keeps the list.
    const sent = within(chat).getByTestId("selection-chip");
    expect(within(sent).getAllByRole("listitem").map((li) => li.textContent)).toEqual(ids);
  });

  it("clearing the chip clears the canvas selection", () => {
    const chat = renderWorkspace();
    fireEvent.click(screen.getByRole("button", { name: "Select a labelled face" }));
    fireEvent.click(within(chat).getByRole("button", { name: "Clear selection" }));
    expect(within(chat).queryByTestId("selection-chip")).toBeNull();
    expect(screen.getByTestId("probe-selection").textContent).toBe("none");
  });

  it("shows an error when the API cannot be reached", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "Not Found" }), { status: 404 })));
    const chat = renderWorkspace();
    fireEvent.change(within(chat).getByRole("textbox", { name: "Message" }), { target: { value: "Hello" } });
    fireEvent.click(within(chat).getByRole("button", { name: "Send" }));
    expect((await within(chat).findByRole("alert")).textContent).toContain("Not Found");
  });
});

describe("Composer", () => {
  it("does not send while Claude is replying", () => {
    const onSubmit = vi.fn();
    render(
      <Composer placeholder="Ask" busy files={[]} onFilesChange={() => {}} choices={DEFAULT_CHOICES} onChoicesChange={() => {}} onSubmit={onSubmit} />,
    );
    const box = screen.getByPlaceholderText("Ask");
    fireEvent.change(box, { target: { value: "Hi" } });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSubmit).not.toHaveBeenCalled();
    const send = screen.getByRole("button", { name: "Send" });
    expect(send).toHaveProperty("disabled", true);
    expect(send.getAttribute("title")).toBe("Claude is still replying");
  });

  it("labels selections", () => {
    expect(selectionLabel({ faces: ["a"], edges: [] })).toBe("a");
    expect(selectionLabel({ part: "lid", faces: ["a", "b"], edges: ["c"] })).toBe("2 faces, 1 edge on lid");
    expect(selectionLabel({ part: "base", faces: [], edges: [] })).toBe("base");
    expect(selectionLabel({ parts: ["a", "b"], faces: [], edges: [] })).toBe("2 parts");
    expect(selectionLabel({ components: ["R1"], faces: [], edges: [] })).toBe("R1");
  });
});
