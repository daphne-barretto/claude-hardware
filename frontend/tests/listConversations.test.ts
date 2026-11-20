import { afterEach, describe, expect, it, vi } from "vitest";
import { listConversations } from "@/lib/session-api";

afterEach(() => vi.unstubAllGlobals());

describe("listConversations", () => {
  it("treats an answer that isn't a list as no threads, so the chat never crashes", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "x" }), { status: 200 })));
    await expect(listConversations("session-1")).resolves.toEqual([]);
  });

  it("returns the list it gets", async () => {
    const list = [{ id: "main", kind: "main", title: "Chat", status: "idle" }];
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(list), { status: 200 })));
    await expect(listConversations("session-1")).resolves.toEqual(list);
  });
});
