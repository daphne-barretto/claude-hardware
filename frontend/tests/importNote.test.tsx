import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { ChatMessages } from "@/components/sites/claude-shell/ChatMessages";
import type { ChatMessage } from "@/components/sites/claude-shell/chat";
import type { Build } from "@/lib/session-api";

// A build no message made says where it came from: an imported file, or the existing design an
// example opens on. A build Claude made in its reply has no such note.
afterEach(cleanup);

const build = (id: string, origin?: Build["origin"]): Build => ({
  id,
  created_at: "2026-01-01T00:00:00Z",
  board: null,
  status: "ready",
  checks: { pass: 3, fail: 0, untested: 0 },
  maps: [],
  mesh_url: "",
  checks_url: "",
  source_url: "",
  board_url: "",
  step_url: null,
  origin,
});

const show = (b: Build) => {
  const messages: ChatMessage[] = [{ id: "m1", role: "assistant", text: "", blocks: [{ kind: "build", build: b }] }];
  render(<ChatMessages messages={messages} busy={false} onAnswer={() => {}} />);
};

describe("the note for a build that came from a file", () => {
  it("names the imported file above its build", () => {
    show(build("build-1", { kind: "import", file_name: "Bracket.step" }));
    expect(screen.getByTestId("import-note").textContent).toBe("Imported Bracket.step");
    expect(screen.getByTestId("build-ready")).toBeTruthy();
  });

  it("names the existing design's file for the build an example opens on", () => {
    show(build("build-1", { kind: "existing", file_name: "Case.step" }));
    expect(screen.getByTestId("import-note").textContent).toMatch(/existing design from Case\.step/);
  });

  it("isn't shown for a build Claude made", () => {
    show(build("build-2"));
    expect(screen.queryByTestId("import-note")).toBeNull();
    expect(screen.getByTestId("build-ready")).toBeTruthy();
  });
});
