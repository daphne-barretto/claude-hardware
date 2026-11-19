import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, resolve } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { AppShell } from "@/components/sites/claude-shell/AppShell";
import { HardwareWorkspace } from "@/components/sites/claude-shell/HardwareWorkspace";
import { ApiError } from "@/lib/session-api";
import { limitMessage, limitNotice, REMOVED_NOTE, wasScrubbed } from "@/lib/limits";
import { contentSecurityPolicy, securityHeaders } from "../next.config";

// docs/security.md: the page's security headers, no secret in the browser bundle's sources, and
// the chat showing the server's limit messages and the removed-secret note as they are.

vi.mock("next/navigation", () => ({ usePathname: () => "/session/session-abc", useRouter: () => ({ push: vi.fn() }) }));

class FakeEventSource {
  constructor(readonly url: string) {}
  addEventListener() {}
  close() {}
}

const SID = "session-abc";
const LIMIT =
  "You've reached this demo's limit of 30 replies from Claude an hour. You can send another in about 12 minutes. Meanwhile you can still edit the design by hand.";
const KEY = "sk-ant-api03-" + "Ab3".repeat(15);
let answer: (body: { text: string }) => Response;
let requests: string[] = [];

beforeEach(() => {
  requests = [];
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      const method = init.method ?? "GET";
      requests.push(`${method} ${url}`);
      if (url === `/api/sessions/${SID}/messages` && method === "POST") return answer(JSON.parse(String(init.body)));
      if (url === `/api/sessions/${SID}`) return Response.json({ id: SID, title: "Demo", status: "idle", created_at: "2026-01-01T00:00:00Z" });
      return Response.json([]);
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

async function send(text: string) {
  render(
    <AppShell>
      <HardwareWorkspace sessionId={SID} />
    </AppShell>,
  );
  const chat = screen.getByRole("region", { name: "Chat" });
  // Once the chat's history has loaded.
  await waitFor(() => expect(requests).toContain(`GET /api/sessions/${SID}/messages`));
  await new Promise((r) => setTimeout(r, 50));
  fireEvent.change(within(chat).getByRole("textbox", { name: "Message" }), { target: { value: text } });
  const button = within(chat).getByRole("button", { name: "Send" }) as HTMLButtonElement;
  await waitFor(() => expect(button.disabled).toBe(false));
  fireEvent.click(button);
  return chat;
}

describe("limit messages", () => {
  it("are shown as the server wrote them, without Could not reach Claude", async () => {
    answer = () => Response.json({ detail: LIMIT }, { status: 429, headers: { "Retry-After": "700" } });
    const chat = await send("Make the lid thicker");
    await waitFor(() => expect(within(chat).getByText(LIMIT)).toBeTruthy());
    expect(within(chat).queryByText(/Could not reach Claude/)).toBeNull();
    // Through the run notices' UI, as a warning, not as a stopped reply.
    expect(within(chat).getByTestId("run-notice").getAttribute("data-kind")).toBe("warning");
    expect(within(chat).queryByTestId("run-stopped")).toBeNull();
  });

  it("words a limit as a warning notice with its reason", () => {
    expect(limitNotice(new ApiError(429, "Slow down"))).toEqual({ kind: "warning", text: "Slow down", reason: "rate_limited" });
    expect(limitNotice(new ApiError(503, "Today's budget is spent"))?.reason).toBe("billing");
    expect(limitNotice(new ApiError(500, "boom"))).toBeNull();
  });

  it("covers the limit statuses only", () => {
    for (const status of [403, 408, 413, 415, 429, 503]) expect(limitMessage(new ApiError(status, "why"))).toBe("why");
    expect(limitMessage(new ApiError(500, "boom"))).toBeNull();
    expect(limitMessage(new Error("x"))).toBeNull();
  });
});

describe("a message with a secret in it", () => {
  it("shows the text as the server kept it, and says what was removed", async () => {
    answer = (body) =>
      Response.json({ id: "msg-1", role: "user", text: body.text.replace(KEY, "[removed: API key]") }, { status: 202 });
    const chat = await send(`My key is ${KEY}`);
    await waitFor(() => expect(within(chat).getByText(REMOVED_NOTE)).toBeTruthy());
    expect(within(chat).getByText("My key is [removed: API key]")).toBeTruthy();
    expect(chat.textContent).not.toContain(KEY);
  });

  it("only when something was removed", () => {
    expect(wasScrubbed("hi", { text: "hi" })).toBe(false);
    expect(wasScrubbed("a b", { text: "a [removed: email address]" })).toBe(true);
    expect(wasScrubbed("a", null)).toBe(false);
  });
});

describe("page security headers", () => {
  const header = (key: string) => securityHeaders.find((h) => h.key === key)?.value;

  it("forbid framing and sniffing, and load nothing from other origins", () => {
    expect(header("X-Frame-Options")).toBe("DENY");
    expect(header("X-Content-Type-Options")).toBe("nosniff");
    expect(header("Referrer-Policy")).toBe("same-origin");
    expect(contentSecurityPolicy).toContain("frame-ancestors 'none'");
    expect(contentSecurityPolicy).toContain("object-src 'none'");
    expect(contentSecurityPolicy).toContain("default-src 'self'");
    // three.js meshes and workers come from data: and blob: URLs.
    expect(contentSecurityPolicy).toMatch(/connect-src 'self' data: blob:/);
    expect(contentSecurityPolicy).not.toMatch(/https?:\/\//);
  });
});

describe("the browser bundle", () => {
  const src = resolve(__dirname, "../src");
  const files = (dir: string): string[] =>
    readdirSync(dir).flatMap((name) => {
      const path = join(dir, name);
      return statSync(path).isDirectory() ? files(path) : /\.(ts|tsx)$/.test(name) ? [path] : [];
    });

  it("reads no secret: no server env and no key in the client sources", () => {
    for (const file of files(src)) {
      const text = readFileSync(file, "utf8");
      for (const m of text.matchAll(/process\.env\.([A-Z0-9_]+)/g))
        expect(m[1] === "NODE_ENV" || m[1].startsWith("NEXT_PUBLIC_"), `${file}: ${m[0]}`).toBe(true);
      expect(text, file).not.toMatch(/ANTHROPIC_API_KEY|sk-ant-[A-Za-z0-9]/);
    }
    const config = readFileSync(resolve(__dirname, "../next.config.ts"), "utf8");
    expect(config).not.toMatch(/^\s*env\s*:/m); // nothing passed through to the client bundle
  });
});
