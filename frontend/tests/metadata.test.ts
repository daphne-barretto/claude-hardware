import { describe, expect, it, vi } from "vitest";

vi.mock("next/font/google", () => ({
  Source_Serif_4: () => ({ variable: "font-serif" }),
}));

const { metadata } = await import("@/app/layout");

describe("page metadata", () => {
  it("titles the browser tab without a prototype marker", () => {
    expect(metadata.title).toBe("Claude Hardware");
    expect(String(metadata.title).toLowerCase()).not.toContain("prototype");
  });
});
