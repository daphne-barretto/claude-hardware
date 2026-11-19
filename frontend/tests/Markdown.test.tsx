import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { Markdown } from "@/components/sites/claude-shell/Markdown";

afterEach(cleanup);

describe("Claude's text as markdown", () => {
  // Regression: the chat showed Claude's markdown as raw text ("**Base:**", "- ").
  it("renders bold, lists, tables, and code", () => {
    const { container } = render(
      <Markdown text={"**Base:** a plate 95 × 80 mm\n\n- one\n- two\n\n| Part | Wall |\n|---|---|\n| lid | 2.5 mm |\n\nUse `wall`."} />,
    );
    expect(container.querySelector("strong")?.textContent).toBe("Base:");
    expect([...container.querySelectorAll("li")].map((li) => li.textContent)).toEqual(["one", "two"]);
    expect(screen.getByRole("table").textContent).toContain("2.5 mm");
    expect(container.querySelector("code")?.textContent).toBe("wall");
    expect(container.textContent).not.toContain("**");
  });

  it("never renders HTML or script from the text, and opens links in a new tab", () => {
    const { container } = render(<Markdown text={'<img src=x onerror="alert(1)"><script>alert(2)</script>\n\n[docs](https://example.com) [bad](javascript:alert(3))'} />);
    expect(container.querySelector("img, script")).toBeNull();
    const links = [...container.querySelectorAll("a")];
    expect(links[0].getAttribute("href")).toBe("https://example.com");
    expect(links[0].getAttribute("target")).toBe("_blank");
    expect(links[0].getAttribute("rel")).toContain("noopener");
    expect(links[1]?.getAttribute("href") ?? "").not.toMatch(/^javascript:/);
  });
});
