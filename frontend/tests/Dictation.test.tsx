import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { Composer } from "@/components/sites/claude-shell/Composer";
import { DEFAULT_CHOICES } from "@/components/sites/claude-shell/data";
import {
  dictationErrorMessage,
  dictationParts,
  formatElapsed,
  partsCaret,
  partsText,
  SILENCE_MS,
  STOP_GRACE_MS,
} from "@/components/sites/claude-shell/dictation";

// A SpeechRecognition the tests drive: `hear` sends results, `end` ends it, `fail` errors.
class FakeRecognition {
  static last: FakeRecognition | null = null;
  continuous = false;
  interimResults = false;
  lang = "";
  onresult: ((e: unknown) => void) | null = null;
  onend: (() => void) | null = null;
  onerror: ((e: { error: string }) => void) | null = null;
  started = false;
  stopped = false;
  aborted = false;
  constructor() {
    FakeRecognition.last = this;
  }
  start() {
    this.started = true;
  }
  stop() {
    this.stopped = true;
  }
  abort() {
    this.aborted = true;
  }
  /** Results since start: [text, isFinal]. */
  hear(...results: [string, boolean][]) {
    const list = results.map(([t, isFinal]) => Object.assign([{ transcript: t }], { isFinal }));
    act(() => this.onresult?.({ results: list }));
  }
  end() {
    act(() => this.onend?.());
  }
  fail(error: string) {
    act(() => {
      this.onerror?.({ error });
      this.onend?.();
    });
  }
}
const rec = () => FakeRecognition.last!;
const w = window as unknown as { SpeechRecognition?: unknown };

beforeEach(() => {
  FakeRecognition.last = null;
  w.SpeechRecognition = FakeRecognition;
});
afterEach(() => {
  cleanup();
  delete w.SpeechRecognition;
  vi.useRealTimers();
});

function setup(initial = "") {
  const onSubmit = vi.fn();
  render(<Composer placeholder="Ask" onSubmit={onSubmit} files={[]} onFilesChange={() => {}} choices={DEFAULT_CHOICES} onChoicesChange={() => {}} />);
  const box = screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;
  if (initial) fireEvent.change(box, { target: { value: initial } });
  return { onSubmit, box };
}

async function dictate(box: HTMLTextAreaElement, at?: number) {
  if (at !== undefined) box.setSelectionRange(at, at);
  fireEvent.click(await screen.findByRole("button", { name: "Dictate" }));
}

const interim = () => screen.getByTestId("dictation-interim");
const final = () => screen.getByTestId("dictation-final");

describe("dictation helpers", () => {
  it("inserts at the cursor with spaces where words would touch", () => {
    const p = dictationParts("Make the wall", "two millimetres", "", "thicker");
    expect(partsText(p)).toBe("Make the wall two millimetres thicker");
    expect(partsCaret(p)).toBe("Make the wall two millimetres".length);
  });

  it("does not add a space before punctuation or after existing whitespace", () => {
    expect(partsText(dictationParts("Hi ", "there", "", ". Bye"))).toBe("Hi there. Bye");
  });

  it("capitalizes at the start of the draft and after a sentence end, not mid-sentence", () => {
    expect(partsText(dictationParts("", "add a vent", "", ""))).toBe("Add a vent");
    expect(partsText(dictationParts("Done.", "add a vent", "", ""))).toBe("Done. Add a vent");
    expect(partsText(dictationParts("Line\n", "add a vent", "", ""))).toBe("Line\nAdd a vent");
    expect(partsText(dictationParts("Please", "add a vent", "", ""))).toBe("Please add a vent");
  });

  it("keeps interim words separate, after the final ones", () => {
    const p = dictationParts("", "add a", "vent here", "");
    expect(p).toEqual({ head: "", final: "Add a", interim: " vent here", tail: "" });
  });

  it("leaves the draft unchanged when nothing was heard", () => {
    expect(partsText(dictationParts("abc", "", "", "def"))).toBe("abcdef");
  });

  it("formats the elapsed time", () => {
    expect(formatElapsed(0)).toBe("0:00");
    expect(formatElapsed(7_900)).toBe("0:07");
    expect(formatElapsed(83_000)).toBe("1:23");
  });

  it("explains a blocked microphone and ignores errors that just end dictation", () => {
    expect(dictationErrorMessage("not-allowed")).toMatch(/blocked.*allow the microphone/i);
    expect(dictationErrorMessage("no-speech")).toBeNull();
    expect(dictationErrorMessage("aborted")).toBeNull();
  });
});

describe("Composer dictation states", () => {
  it("idle: shows the mic and no recording bar", async () => {
    setup();
    expect(await screen.findByRole("button", { name: "Dictate" })).toHaveProperty("ariaPressed", "false");
    expect(screen.queryByTestId("recording-bar")).toBeNull();
    expect(screen.getByRole("button", { name: "Add" })).toBeTruthy();
  });

  it("unsupported: hides the mic", () => {
    delete w.SpeechRecognition;
    setup();
    expect(screen.queryByRole("button", { name: "Dictate" })).toBeNull();
  });

  it("recording: shows a level meter, a timer, cancel and keep in place of the idle controls", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { box } = setup();
    await dictate(box);
    expect(rec().started).toBe(true);
    expect(rec().continuous).toBe(true);
    expect(rec().interimResults).toBe(true);
    expect(screen.getByTestId("recording-bar")).toBeTruthy();
    expect(screen.getByRole("meter", { name: "Microphone level" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Cancel dictation" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Keep dictation" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Stop dictation" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.queryByRole("button", { name: "Add" })).toBeNull();
    const timer = screen.getByRole("timer", { name: "Recording time" });
    expect(timer.textContent).toBe("0:00");
    act(() => {
      vi.advanceTimersByTime(3_100);
    });
    expect(timer.textContent).toBe("0:03");
    expect(box.readOnly).toBe(true);
    expect(box.placeholder).toBe("Listening…");
  });

  it("interim: shows words live and dimmed, then replaced by final text", async () => {
    const { box } = setup();
    await dictate(box);
    rec().hear(["add a vent", false]);
    expect(interim().textContent).toBe("Add a vent");
    expect(interim().className).toContain("text-[var(--cs-muted)]");
    expect(final().textContent).toBe("");
    expect(box.value).toBe("Add a vent");

    rec().hear(["add a vent", true], ["over the", false]);
    expect(final().textContent).toBe("Add a vent");
    expect(interim().textContent).toBe(" over the");

    rec().hear(["add a vent", true], ["over the sensor", true]);
    expect(final().textContent).toBe("Add a vent over the sensor");
    expect(interim().textContent).toBe("");
  });

  it("inserts at the cursor and keeps the text on both sides", async () => {
    const { box } = setup("Make the wall thicker");
    await dictate(box, "Make the wall".length);
    rec().hear(["two millimetres", true]);
    expect(box.value).toBe("Make the wall two millimetres thicker");
    fireEvent.click(screen.getByRole("button", { name: "Keep dictation" }));
    expect(rec().stopped).toBe(true);
    rec().end();
    expect(box.value).toBe("Make the wall two millimetres thicker");
    expect(box.readOnly).toBe(false);
    expect(box.selectionStart).toBe("Make the wall two millimetres".length);
    expect(screen.queryByTestId("recording-bar")).toBeNull();
  });

  it("cancel removes only what this dictation added", async () => {
    const { box } = setup("Keep this");
    await dictate(box);
    rec().hear(["and drop this", true], ["too", false]);
    expect(box.value).toBe("Keep this and drop this too");
    fireEvent.click(screen.getByRole("button", { name: "Cancel dictation" }));
    expect(rec().aborted).toBe(true);
    expect(box.value).toBe("Keep this");
    expect(screen.getByRole("button", { name: "Dictate" })).toBeTruthy();
  });

  it("Escape cancels", async () => {
    const { box } = setup("Draft");
    await dictate(box);
    rec().hear(["spoken", true]);
    fireEvent.keyDown(box, { key: "Escape" });
    expect(box.value).toBe("Draft");
    expect(screen.queryByTestId("recording-bar")).toBeNull();
  });

  it("Enter stops and keeps, without sending", async () => {
    const { box, onSubmit } = setup();
    await dictate(box);
    rec().hear(["add a vent", true]);
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSubmit).not.toHaveBeenCalled();
    expect(rec().stopped).toBe(true);
    rec().end();
    expect(box.value).toBe("Add a vent");
    expect(screen.queryByTestId("recording-bar")).toBeNull();
  });

  it("the mic button stops and keeps", async () => {
    const { box } = setup();
    await dictate(box);
    rec().hear(["add a vent", true]);
    fireEvent.click(screen.getByRole("button", { name: "Stop dictation" }));
    rec().end();
    expect(box.value).toBe("Add a vent");
  });

  it("keeps interim words if the recognizer sends nothing more after stop", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { box } = setup();
    await dictate(box);
    rec().hear(["add a vent", false]);
    fireEvent.click(screen.getByRole("button", { name: "Keep dictation" }));
    act(() => {
      vi.advanceTimersByTime(STOP_GRACE_MS + 10);
    });
    expect(box.value).toBe("Add a vent");
    expect(screen.queryByTestId("recording-bar")).toBeNull();
  });

  it("sending while listening stops dictation first and sends what was heard", async () => {
    const { box, onSubmit } = setup("Please");
    await dictate(box);
    rec().hear(["add a vent", true], ["now", false]);
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(rec().aborted).toBe(true);
    expect(onSubmit).toHaveBeenCalledWith("Please add a vent now", [], DEFAULT_CHOICES);
    expect(box.value).toBe("");
    expect(screen.queryByTestId("recording-bar")).toBeNull();
  });

  it("silence stops listening without losing the text", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { box } = setup();
    await dictate(box);
    rec().hear(["add a vent", true]);
    act(() => {
      vi.advanceTimersByTime(SILENCE_MS + 10);
    });
    expect(rec().stopped).toBe(true);
    rec().end();
    expect(box.value).toBe("Add a vent");
  });

  it("the recognizer ending on its own keeps the text", async () => {
    const { box } = setup();
    await dictate(box);
    rec().hear(["add a vent", true]);
    rec().end();
    expect(box.value).toBe("Add a vent");
    expect(screen.queryByTestId("recording-bar")).toBeNull();
  });

  it("blocked microphone: shows how to allow it, and clears on the next try", async () => {
    const { box } = setup("Draft");
    await dictate(box);
    rec().fail("not-allowed");
    expect(screen.getByRole("alert").textContent).toMatch(/Microphone access is blocked.*allow the microphone/);
    expect(screen.queryByTestId("recording-bar")).toBeNull();
    expect(box.value).toBe("Draft");
    await dictate(box);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("blocked microphone from getUserMedia shows the same message", async () => {
    const getUserMedia = vi.fn().mockRejectedValue(Object.assign(new Error("denied"), { name: "NotAllowedError" }));
    Object.defineProperty(navigator, "mediaDevices", { value: { getUserMedia }, configurable: true });
    try {
      const { box } = setup();
      await dictate(box);
      expect(await screen.findByRole("alert")).toBeTruthy();
      expect(rec().aborted).toBe(true);
    } finally {
      delete (navigator as unknown as { mediaDevices?: unknown }).mediaDevices;
    }
  });
});
