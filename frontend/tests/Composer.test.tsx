import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { Composer, isLongPaste, messageText } from "@/components/sites/claude-shell/Composer";
import { DEFAULT_CHOICES, LONG_PASTE_CHARS } from "@/components/sites/claude-shell/data";

const choiceProps = { choices: DEFAULT_CHOICES, onChoicesChange: () => {} };

afterEach(cleanup);

function setup(variant: "home" | "reply") {
  const onSubmit = vi.fn();
  render(<Composer placeholder="Ask" variant={variant} onSubmit={onSubmit} files={[]} onFilesChange={() => {}} {...choiceProps} />);
  const box = screen.getByPlaceholderText("Ask");
  fireEvent.change(box, { target: { value: "にほんご" } });
  return { onSubmit, box };
}

describe.each(["home", "reply"] as const)("Composer (%s)", (variant) => {
  it("does not send when Enter confirms an IME composition", () => {
    const { onSubmit, box } = setup(variant);
    fireEvent.keyDown(box, { key: "Enter", isComposing: true });
    expect(onSubmit).not.toHaveBeenCalled();
    expect(box).toHaveProperty("value", "にほんご");
  });

  it("sends on Enter once composition has ended", () => {
    const { onSubmit, box } = setup(variant);
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSubmit).toHaveBeenCalledWith("にほんご", [], DEFAULT_CHOICES);
    expect(box).toHaveProperty("value", "");
  });

  it("keeps Shift+Enter as a newline", () => {
    const { onSubmit, box } = setup(variant);
    fireEvent.keyDown(box, { key: "Enter", shiftKey: true });
    expect(onSubmit).not.toHaveBeenCalled();
  });
});

describe("Composer dictation", () => {
  afterEach(() => {
    delete (window as unknown as { SpeechRecognition?: unknown }).SpeechRecognition;
  });

  it("shows the accent colour, without the muted one, while listening", async () => {
    class FakeRecognition {
      continuous = false;
      interimResults = false;
      lang = "";
      onresult = null;
      onend = null;
      onerror = null;
      start() {}
      stop() {}
    }
    (window as unknown as { SpeechRecognition: unknown }).SpeechRecognition = FakeRecognition;
    render(<Composer placeholder="Ask" onSubmit={() => {}} files={[]} onFilesChange={() => {}} {...choiceProps} />);

    const mic = await screen.findByRole("button", { name: "Dictate" });
    expect(mic.className).toContain("text-[var(--cs-muted)]");
    fireEvent.click(mic);

    const listening = screen.getByRole("button", { name: "Stop dictation" });
    expect(listening.className).toContain("text-[var(--cs-accent)]");
    expect(listening.className).not.toContain("text-[var(--cs-muted)]");
    expect(listening.className).not.toContain("hover:text-[var(--cs-text)]");
  });
});

describe("Composer send button", () => {
  it("is disabled and says why while empty, enabled once there is text", () => {
    render(<Composer placeholder="Ask" onSubmit={() => {}} files={[]} onFilesChange={() => {}} {...choiceProps} />);
    const send = screen.getByRole("button", { name: "Send" });
    expect(send).toHaveProperty("disabled", true);
    expect(send.getAttribute("title")).toBeTruthy();
    fireEvent.change(screen.getByPlaceholderText("Ask"), { target: { value: "Hi" } });
    expect(send).toHaveProperty("disabled", false);
    expect(send.className).toContain("bg-[var(--cs-accent)]");
  });

  it("keeps focus in the box after sending", () => {
    const { box } = setup("reply");
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(box).toHaveProperty("value", "");
    expect(document.activeElement).toBe(box);
  });
});

describe("Composer long pastes", () => {
  const long = "x".repeat(LONG_PASTE_CHARS);

  it("treats long or many-line text as a long paste", () => {
    expect(isLongPaste(long)).toBe(true);
    expect(isLongPaste("a\n".repeat(60))).toBe(true);
    expect(isLongPaste("a short note")).toBe(false);
  });

  it("turns a long paste into a chip and sends it after the typed text", () => {
    const onSubmit = vi.fn();
    render(<Composer placeholder="Ask" onSubmit={onSubmit} files={[]} onFilesChange={() => {}} {...choiceProps} />);
    const box = screen.getByPlaceholderText("Ask");
    fireEvent.paste(box, { clipboardData: { getData: () => long } });
    expect(box).toHaveProperty("value", "");
    expect(screen.getByTestId("pasted-chip")).toBeTruthy();
    fireEvent.change(box, { target: { value: "Summarise this" } });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSubmit).toHaveBeenCalledWith(`Summarise this\n\n${long}`, [], DEFAULT_CHOICES);
    expect(screen.queryByTestId("pasted-chip")).toBeNull();
  });

  it("removes a pasted chip", () => {
    render(<Composer placeholder="Ask" onSubmit={() => {}} files={[]} onFilesChange={() => {}} {...choiceProps} />);
    fireEvent.paste(screen.getByPlaceholderText("Ask"), { clipboardData: { getData: () => long } });
    fireEvent.click(screen.getByRole("button", { name: "Remove pasted content" }));
    expect(screen.queryByTestId("pasted-chip")).toBeNull();
    expect(screen.getByRole("button", { name: "Send" })).toHaveProperty("disabled", true);
  });

  it("joins typed and pasted text", () => {
    expect(messageText("  hi ", ["block"])).toBe("hi\n\nblock");
    expect(messageText("", ["a", "b"])).toBe("a\n\nb");
  });
});
