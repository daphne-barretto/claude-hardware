import { sessionFixture } from "./fixtures";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));

import { SearchDialog, searchSessions } from "@/components/sites/claude-shell/SearchDialog";
import { sessionHref, type Session } from "@/lib/session-api";

const sessions: Session[] = [2, 1].map((n) => ({
  id: `s-${n}`,
  title: `${sessionFixture.title} ${n}`,
  created_at: new Date(Date.UTC(2026, 0, n)).toISOString(),
  board: sessionFixture.board,
  status: "idle",
}));

beforeEach(() => push.mockReset());
afterEach(cleanup);

function setup() {
  const onClose = vi.fn();
  render(<SearchDialog sessions={sessions} onClose={onClose} />);
  return { onClose, input: screen.getByRole("textbox") };
}

describe("SearchDialog", () => {
  it("is a modal dialog", () => {
    setup();
    expect(screen.getByRole("dialog").getAttribute("aria-modal")).toBe("true");
  });

  it("lists sessions and filters them by title and board", () => {
    setup();
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
    expect(searchSessions(sessions, sessions[1].title)).toEqual([sessions[1]]);
    expect(searchSessions(sessions, sessionFixture.board.name)).toEqual(sessions);
    expect(searchSessions(sessions, "no such session")).toEqual([]);
  });

  it("does not navigate when Enter confirms an IME composition", () => {
    const { onClose, input } = setup();
    fireEvent.change(input, { target: { value: sessions[1].title } });
    fireEvent.keyDown(input, { key: "Enter", isComposing: true });
    expect(push).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.keyDown(input, { key: "Enter" });
    expect(push).toHaveBeenCalledWith(sessionHref(sessions[1].id));
    expect(onClose).toHaveBeenCalled();
  });
});
