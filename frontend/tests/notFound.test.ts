import { describe, expect, it, vi } from "vitest";

const redirect = vi.fn();
vi.mock("next/navigation", () => ({ redirect: (to: string) => redirect(to) }));

describe("an unknown URL", () => {
  it("opens the New page instead of a 404", async () => {
    const { default: NotFound } = await import("../src/app/not-found");
    NotFound();
    expect(redirect).toHaveBeenCalledWith("/new");
  });
});
