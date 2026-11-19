import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { applyEvent, retryableStop, type ChatMessage } from "@/components/sites/claude-shell/chat";
import { ChatMessages } from "@/components/sites/claude-shell/ChatMessages";
import { ConnectionBanner, RunControlsProvider } from "@/components/sites/claude-shell/RunNotice";
import { errorText } from "@/components/sites/claude-shell/useChatSession";
import { ModelPicker } from "@/components/sites/claude-shell/ModelPicker";
import { loadModels } from "@/components/sites/claude-shell/useAvailableModels";
import { MORE_MODELS, MODELS } from "@/components/sites/claude-shell/data";
import { ApiError, subscribe, type SessionEvent } from "@/lib/session-api";

// The chat when a limit or failure is hit (backend app.agent.limits): a notice while the reply
// goes on, a plain message and Retry when it stops, "Reconnecting…" or a banner when the stream drops.

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

const BUSY =
  "Claude is getting a lot of requests right now, so this reply is paused. It tries again automatically (try 1 of 4). Nothing is lost.";
const CREDITS =
  "Claude can't reply right now: this app's Anthropic account has run out of credits. Your design and builds are kept. Let whoever runs this app know, then press Retry once it's topped up.";

const play = (events: SessionEvent[], from: ChatMessage[] = []) => events.reduce(applyEvent, from);
const start: SessionEvent = { type: "message_start", message_id: "m-1", role: "assistant" };
const retrying: SessionEvent = {
  type: "notice",
  message_id: "m-1",
  kind: "retrying",
  text: BUSY,
  reason: "rate_limited",
  retry_in_s: 20,
  attempt: 1,
  attempts: 4,
};

describe("the chat's notices", () => {
  it("shows a retrying notice until the reply moves on", () => {
    let messages = play([start, retrying]);
    expect(messages[0].blocks).toMatchObject([{ kind: "notice", notice: { kind: "retrying", retry_in_s: 20 } }]);
    // A second retry replaces the first.
    messages = play([{ ...retrying, attempt: 2 } as SessionEvent], messages);
    expect(messages[0].blocks).toHaveLength(1);
    messages = play([{ type: "text", message_id: "m-1", delta: "Back on it." }], messages);
    expect(messages[0].blocks).toEqual([{ kind: "text", text: "Back on it." }]);
  });

  it("keeps a warning, and drops a waiting notice when the reply ends", () => {
    const warning: SessionEvent = { type: "notice", message_id: "m-1", kind: "warning", text: "A build stopped.", reason: "build_crash" };
    const waiting: SessionEvent = { type: "notice", message_id: "m-1", kind: "waiting", text: "You're next.", reason: "run_queue", place: 1 };
    const messages = play([start, warning, waiting, { type: "message_end", message_id: "m-1" }]);
    expect(messages[0].blocks?.map((b) => b.kind)).toEqual(["notice"]);
    expect(messages[0].blocks?.[0]).toMatchObject({ notice: { kind: "warning" } });
  });

  it("renders a live countdown and Stop while retrying", () => {
    vi.useFakeTimers();
    const onStop = vi.fn();
    const messages = play([start, retrying]);
    render(
      <RunControlsProvider value={{ onStop }}>
        <ChatMessages messages={messages} busy onAnswer={() => {}} />
      </RunControlsProvider>,
    );
    const notice = screen.getByTestId("run-notice");
    expect(notice.textContent).toContain(BUSY);
    expect(within(notice).getByTestId("retry-countdown").textContent).toBe("Retrying in 20 s");
    act(() => vi.advanceTimersByTime(5000));
    expect(within(notice).getByTestId("retry-countdown").textContent).toBe("Retrying in 15 s");
    fireEvent.click(within(notice).getByRole("button", { name: "Stop" }));
    expect(onStop).toHaveBeenCalledTimes(1);
  });
});

describe("a reply that stopped", () => {
  const stopped = play([
    start,
    { type: "error", message_id: "m-1", message: CREDITS, retryable: true, reason: "billing" },
    { type: "message_end", message_id: "m-1" },
  ]);

  it("says plainly what happened and offers Retry on the latest stop only", () => {
    const onRetry = vi.fn();
    const latestStop = retryableStop(stopped);
    expect(latestStop).toMatchObject({ kind: "error", reason: "billing" });
    render(
      <RunControlsProvider value={{ onRetry, latestStop }}>
        <ChatMessages messages={stopped} busy={false} onAnswer={() => {}} />
      </RunControlsProvider>,
    );
    const alert = screen.getByTestId("run-stopped");
    expect(alert.textContent).toContain(CREDITS);
    expect(alert.textContent).not.toMatch(/429|API Error|Exception/);
    fireEvent.click(within(alert).getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("offers no Retry once something was sent after it, or when Retry can't help", () => {
    expect(retryableStop([...stopped, { id: "u-2", role: "user", text: "next" }])).toBeUndefined();
    const notSetUp = play([
      start,
      { type: "error", message_id: "m-1", message: "Claude isn't set up on this server yet.", retryable: false, reason: "not_configured" },
    ]);
    expect(retryableStop(notSetUp)).toBeUndefined();
  });
});

describe("a request that failed", () => {
  it("is said in plain words, never as a raw error", () => {
    expect(errorText(new TypeError("Failed to fetch"))).toMatch(/^Couldn't reach the server.*Nothing is lost/);
    expect(errorText(new ApiError(502, "Bad Gateway"))).toMatch(/^The server isn't responding right now/);
    expect(errorText(new ApiError(409, "answer the open question first"))).toBe("answer the open question first");
  });
});

describe("the event stream's connection", () => {
  class FakeEventSource {
    static all: FakeEventSource[] = [];
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSED = 2;
    readyState = 0;
    onerror: ((e: Event) => void) | null = null;
    onopen: ((e: Event) => void) | null = null;
    listeners: Record<string, ((e: Event) => void)[]> = {};
    constructor(public url: string) {
      FakeEventSource.all.push(this);
    }
    addEventListener(type: string, fn: (e: Event) => void) {
      (this.listeners[type] ??= []).push(fn);
    }
    emit(id: number, event: SessionEvent) {
      this.listeners[event.type]?.forEach((fn) => fn(new MessageEvent(event.type, { data: JSON.stringify(event), lastEventId: String(id) })));
    }
    close() {
      this.readyState = 2;
    }
  }

  it("reconnects after the last event seen, saying so, and shows a banner while the server is away", async () => {
    vi.useFakeTimers();
    FakeEventSource.all = [];
    vi.stubGlobal("EventSource", FakeEventSource);
    let up = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        if (!up) throw new TypeError("Failed to fetch");
        return new Response("{}", { status: 200 });
      }),
    );
    const events: SessionEvent[] = [];
    const statuses: string[] = [];
    subscribe("session-1", (e) => events.push(e), undefined, (s) => statuses.push(s));
    const first = FakeEventSource.all[0];
    first.onopen?.(new Event("open"));
    first.emit(7, start);
    first.emit(7, start); // a repeat after a reconnect: ignored
    expect(events).toHaveLength(1);

    first.readyState = FakeEventSource.CONNECTING; // the browser retries by itself
    first.onerror?.(new Event("error"));
    first.readyState = FakeEventSource.CLOSED; // and gives up: the server is down
    first.onerror?.(new Event("error"));
    await vi.advanceTimersByTimeAsync(0);
    expect(statuses).toEqual(["connected", "reconnecting", "offline"]);

    up = true;
    await vi.advanceTimersByTimeAsync(1000);
    const second = FakeEventSource.all[1];
    expect(second.url).toBe("/api/sessions/session-1/events?after=7");
    second.onopen?.(new Event("open"));
    expect(statuses.at(-1)).toBe("connected");
  });

  it("renders Reconnecting… and the offline banner with Try now", () => {
    const onRetryNow = vi.fn();
    const { rerender } = render(<ConnectionBanner status="reconnecting" onRetryNow={onRetryNow} />);
    expect(screen.getByTestId("connection-status").textContent).toBe("Reconnecting…");
    rerender(<ConnectionBanner status="offline" onRetryNow={onRetryNow} />);
    expect(screen.getByTestId("connection-status").textContent).toMatch(/Can't reach the server right now. Your work is saved/);
    fireEvent.click(screen.getByRole("button", { name: "Try now" }));
    expect(onRetryNow).toHaveBeenCalledTimes(1);
    rerender(<ConnectionBanner status="connected" onRetryNow={onRetryNow} />);
    expect(screen.queryByTestId("connection-status")).toBeNull();
  });
});

describe("the model picker", () => {
  it("offers only the models the server's API key can use, and moves off one that isn't", async () => {
    const gone = MORE_MODELS[0];
    const served = { default: MODELS[0].id, models: MODELS, more_models: MORE_MODELS.filter((m) => m.id !== gone.id) };
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(served), { status: 200 })));
    await loadModels(true);
    const onChange = vi.fn();
    render(<ModelPicker model={gone.id} effort="medium" onChange={onChange} />);
    await vi.waitFor(() => expect(onChange).toHaveBeenCalledWith({ model: served.default, effort: "medium" }));
    fireEvent.click(screen.getByRole("button", { expanded: false }));
    const more = screen.queryByRole("menuitem", { name: /More models/ });
    if (more) {
      fireEvent.click(more);
      const items = within(screen.getByRole("menu", { name: "More models" })).getAllByRole("menuitemradio");
      expect(items.map((i) => i.textContent)).not.toContain(`${gone.name}${gone.blurb}`);
    }
  });
});
