import { describe, expect, it } from "vitest";
import { lastChoices } from "@/components/sites/claude-shell/useChatSession";
import type { Message } from "@/lib/session-api";

const msg = (m: Partial<Message>): Message => ({ id: "m", role: "user", text: "", created_at: "", ...m });

describe("reopening a session", () => {
  it("brings back the model and effort of its last message", () => {
    const stored = [
      msg({ model: "claude-opus-5-5", effort: "medium" }),
      msg({ role: "assistant" }),
      msg({ model: "claude-haiku-5-5", effort: "high" }),
      msg({ role: "assistant" }),
    ];
    expect(lastChoices(stored)).toEqual({ model: "claude-haiku-5-5", effort: "high" });
  });

  it("keeps the defaults when no message recorded a choice", () => {
    expect(lastChoices([msg({}), msg({ role: "assistant" })])).toBeNull();
    expect(lastChoices([])).toBeNull();
  });
});
