import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { AppShell } from "@/components/sites/claude-shell/AppShell";
import { HardwareWorkspace } from "@/components/sites/claude-shell/HardwareWorkspace";
import { ApiError, isSessionGone } from "@/lib/session-api";

// A chat whose session is no longer on the server: the chat says so and offers a new
// chat, instead of "Could not reach Claude: no session ...".
const push = vi.fn();
vi.mock("next/navigation", () => ({ usePathname: () => "/session/session-9", useRouter: () => ({ push }) }));

class FakeEventSource {
  constructor(readonly url: string) {}
  addEventListener() {}
  close() {}
}

const GONE = "session-9";
let requests: { method: string; url: string }[];
/** Answers for POST /api/sessions: a new session, or a failure. */
let created: Response | null;

beforeEach(() => {
  requests = [];
  created = null;
  push.mockReset();
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      const method = init.method ?? "GET";
      requests.push({ method, url });
      if (url === "/api/sessions" && method === "POST")
        return created ?? new Response(JSON.stringify({ id: "session-10", title: "Untitled", status: "idle" }), { status: 201 });
      // The session was there when the chat opened (a 404 then is another browser's session,
      // SessionUnavailable); it went away since, so everything else about it answers 404.
      if (url === `/api/sessions/${GONE}` && method === "GET")
        return new Response(JSON.stringify({ id: GONE, title: "Untitled", status: "idle" }), { status: 200 });
      if (url === `/api/sessions/${GONE}/messages` && method === "GET") return new Response("[]", { status: 200 });
      // Everything else about the gone session answers 404, as the session API does.
      return new Response(JSON.stringify({ detail: `no session ${GONE}` }), { status: 404 });
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderGoneChat() {
  render(
    <AppShell>
      <HardwareWorkspace sessionId={GONE} />
    </AppShell>,
  );
  return screen.getByRole("region", { name: "Chat" });
}

async function send(chat: HTMLElement, text: string) {
  // The chat has loaded (empty), so the message isn't replaced by the loaded list.
  await within(chat).findByText(/Describe a part or a change/);
  fireEvent.change(within(chat).getByRole("textbox", { name: "Message" }), { target: { value: text } });
  fireEvent.click(within(chat).getByRole("button", { name: "Send" }));
}

describe("a chat whose session is gone", () => {
  it("says so on a 404 no session from postMessage, and offers a new chat instead of Could not reach Claude", async () => {
    const chat = renderGoneChat();
    await send(chat, "Make the lid thicker");
    await waitFor(() => expect(within(chat).getByText(/no longer on the server/)).toBeTruthy());
    expect(requests).toContainEqual({ method: "POST", url: `/api/sessions/${GONE}/messages` });
    expect(within(chat).getByRole("button", { name: "Start a new chat" })).toBeTruthy();
    expect(within(chat).queryByText(/Could not reach Claude/)).toBeNull();
    expect(within(chat).queryByText(/no session/)).toBeNull();
    // Nothing more can be sent to the gone session, and the composer says why.
    fireEvent.change(within(chat).getByRole("textbox", { name: "Message" }), { target: { value: "Again" } });
    const sendButton = within(chat).getByRole("button", { name: "Send" }) as HTMLButtonElement;
    expect(sendButton.disabled).toBe(true);
  });

  it("Start a new chat creates a session and opens it", async () => {
    const chat = renderGoneChat();
    await send(chat, "Make the lid thicker");
    fireEvent.click(await within(chat).findByRole("button", { name: "Start a new chat" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/session/session-10"));
    expect(requests.filter((r) => r.method === "POST" && r.url === "/api/sessions")).toHaveLength(1);
  });

  it("says so when the new chat cannot be created, and the control can be tried again", async () => {
    created = new Response(JSON.stringify({ detail: "server busy" }), { status: 503 });
    const chat = renderGoneChat();
    await send(chat, "Make the lid thicker");
    fireEvent.click(await within(chat).findByRole("button", { name: "Start a new chat" }));
    await waitFor(() => expect(within(chat).getByText(/Could not start a new chat: server busy/)).toBeTruthy());
    expect(push).not.toHaveBeenCalled();
    created = null;
    fireEvent.click(within(chat).getByRole("button", { name: "Start a new chat" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/session/session-10"));
  });
});

describe("isSessionGone", () => {
  it("is a 404 for the session itself, not other 404s or errors", () => {
    expect(isSessionGone(new ApiError(404, "no session session-9"))).toBe(true);
    expect(isSessionGone(new ApiError(404, "no build b-1"))).toBe(false);
    expect(isSessionGone(new ApiError(404, "no constraint set x"))).toBe(false);
    expect(isSessionGone(new ApiError(500, "no session session-9"))).toBe(false);
    expect(isSessionGone(new Error("no session session-9"))).toBe(false);
  });
});
