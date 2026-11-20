"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { Box, Image as ImageIcon, Maximize2, Minimize2, PanelLeft, X } from "lucide-react";
import { ArtifactsIcon } from "./ArtifactsIcon";
import { useShell } from "./AppShell";
import { CanvasSlot } from "./CanvasSlot";
import { demoHref, sessionHref } from "@/lib/session-api";
import { latestFound, openQuestion, pinnedQuestion, type Block, type Choices } from "./chat";
import { ChatMessages } from "./ChatMessages";
import { SafetyBadge, safetyNotices } from "./NoticeCard";
import { Composer } from "./Composer";
import { DEFAULT_CHOICES, EMPTY_HEADING } from "./data";
import { useChatSession } from "./useChatSession";
import { useStandardsSetName } from "./useStandardsSetName";
import { ConnectionBanner, RunControlsProvider } from "./RunNotice";
import { pickFiles, type FileKind, unsupportedNote } from "./files";
import { PhotoHint, photoPlaceholder } from "./PhotoHint";
import { QueuedMessages } from "./QueuedMessages";
import { chatQueue } from "./messageQueue";
import { ThreadsPanel } from "./ThreadsPanel";
import { SessionUnavailable, useSessionMissing } from "./SessionUnavailable";
import { ShareButton } from "./ShareButton";
import { ShareMenu } from "./ShareMenu";
import { SharedNotice } from "./SharedNotice";
import { SHARED_BLOCKED, shareToken } from "@/lib/share";
import { StandardsPicker } from "./StandardsPicker";
import { SessionStandards } from "./standards/SessionStandards";
import {
  FoundContext,
  SelectionContext,
  ShowLocationContext,
  useCanvasSelection,
  useSetCanvasSelection,
  type ChatSelection,
  type FoundRequest,
  type SetSelection,
  type ShowRequest,
} from "./canvas-selection";
import { HardwareCanvas } from "@/components/canvas/HardwareCanvas";
import { OpenChecksContext, useOpenChecksValue } from "@/components/canvas/openChecks";
import { ConstraintsBeforeBuild } from "@/components/canvas/ConstraintsPanel";
import { ConstraintsContext, type ConstraintsValue } from "@/components/canvas/constraintsContext";
import { useSessionBuilds } from "@/components/canvas/api";
import { reportAction } from "@/lib/session-api";

const START_OPTIONS: { kind: FileKind; label: string; icon: typeof Box }[] = [
  // A start is a STEP file or a photo.
  { kind: "cad", label: "Import STEP", icon: Box },
  { kind: "image", label: "Photo or scan", icon: ImageIcon },
];

type Props = {
  initialPrompt?: string;
  initialFiles?: string[];
  initialChoices?: Choices;
  /** The 3D canvas, rendered in the canvas slot. It can set the selection with useSetCanvasSelection. */
  canvas?: React.ReactNode;
  /**
   * An existing session on the session API: the chat loads its messages and the canvas its
   * builds. Without it, the first message creates a session and the URL becomes its page.
   */
  sessionId?: string;
};

export { useCanvasSelection, useSetCanvasSelection };

const headerBtn =
  "grid size-8 place-items-center rounded-lg text-[var(--cs-muted)] hover:bg-[var(--cs-hover)] hover:text-[var(--cs-text)]";

/** Chat on the left, the hardware canvas panel on the right (Main and New mockups). */
export function HardwareWorkspace({ initialPrompt, initialFiles = [], initialChoices = DEFAULT_CHOICES, canvas, sessionId: existing }: Props) {
  const { sidebarOpen, setSidebarOpen, refreshSessions, phone } = useShell();
  const router = useRouter();
  const [sessionId, setSessionId] = useState(existing);
  const { session, builds } = useSessionBuilds(sessionId);
  const missing = useSessionMissing(sessionId);
  const chat = useChatSession(existing, (id) => {
    // The workspace stays mounted, so the stream that is starting keeps going; only the URL
    // changes, replacing /new so Back does not return to an empty canvas and a reload reopens it.
    setSessionId(id);
    window.history.replaceState(null, "", sessionHref(id));
    refreshSessions();
  });
  const { messages, busy, waiting, blocked, answer, constraints, changeConstraint } = chat;
  // A read-only share link takes no messages or edits (`SharedNotice`).
  const shared = sessionId ? shareToken(sessionId) : null;
  const readOnly = !!shared;
  // A demo's original is never worked on: its page opens this browser's own copy instead.
  const original = session?.sample ? (session.demo ?? session.id) : null;
  useEffect(() => {
    if (original) router.replace(demoHref(original));
  }, [original, router]);
  // Why the constraints can't be edited now: a shared link, Claude replying, or a question waiting.
  const constraintsLocked = readOnly
    ? SHARED_BLOCKED
    : busy
      ? "Claude is still replying."
      : openQuestion(messages)
        ? "Answer Claude's question first."
        : null;
  // A flagged risk the chat asked the canvas to show; only a session's canvas can.
  const [showRequest, setShowRequest] = useState<ShowRequest | null>(null);
  const sessionCanvas = !canvas && !!sessionId && builds.length > 0;
  // Faces Claude found in chat: the canvas highlights the latest find as it arrives;
  // the card highlights it again or makes it the selection. A newer find wins over a click.
  const found = latestFound(messages);
  const [clicked, setClicked] = useState<(FoundRequest & { after?: string }) | null>(null);
  const showFound = useCallback(
    (block: Extract<Block, { kind: "found" }>, select: boolean, after?: string) => {
      setClicked((r) => ({ result: block.result, buildId: block.buildId, select, key: `click-${(r?.n ?? 0) + 1}`, n: (r?.n ?? 0) + 1, after }));
    },
    [],
  );
  const foundRequest: FoundRequest | null =
    clicked && clicked.after === found?.key
      ? clicked
      : found && found.block.result.matches.length
        ? { result: found.block.result, buildId: found.block.buildId, select: false, key: found.key, n: 0 }
        : null;
  const [choices, setChoices] = useState<Choices>(initialChoices);
  // Reopening a session: the picker shows the model and effort its last message used, unless
  // the user already picked something here.
  const choicePicked = useRef(false);
  const storedChoices = chat.storedChoices;
  useEffect(() => {
    if (storedChoices && !choicePicked.current) setChoices((c) => ({ ...c, ...storedChoices }));
  }, [storedChoices]);
  // Retry and Stop on a stopped, retrying or waiting reply (./RunNotice).
  const runControls = {
    onRetry: () => void chat.retry(choices),
    onStop: () => void chat.stop(),
    latestStop: chat.latestStop,
    acting: chat.acting,
  };
  const [selected, setSelected] = useState<ChatSelection | null>(null);
  const setSelection = useCallback<SetSelection>((s, label) => setSelected(s ? { selection: s, label } : null), []);
  const context = useMemo(() => ({ current: selected, set: setSelection }), [selected, setSelection]);
  const [staged, setStaged] = useState<string[]>([]);
  const [canvasOpen, setCanvasOpen] = useState(true);
  const [canvasMax, setCanvasMax] = useState(false);
  // The build the user picked (a build card). It stays shown until a newer
  // build arrives, which then shows instead.
  const latestBuild = builds.at(-1)?.id;
  const [picked, setPicked] = useState<{ id: string; latest: string | undefined } | null>(null);
  const shownBuild = picked && picked.latest === latestBuild ? picked.id : null;
  // null shows the latest build again (the canvas follows the rebuild after a hand edit).
  const showBuild = (id: string | null) => {
    setPicked(id ? { id, latest: latestBuild } : null);
    setCanvasOpen(true);
  };
  // On a phone the chat and the canvas take the screen one at a time, the chat first.
  const [pane, setPane] = useState<"chat" | "canvas">("chat");
  const title = session?.title ?? "Untitled hardware";
  // The first request names a new session: Recents shows the new name too.
  const sessionTitle = session?.title;
  useEffect(() => {
    if (sessionTitle) refreshSessions();
  }, [sessionTitle, refreshSessions]);
  // Why the session's constraint set can't be changed now.
  const loadReason = busy ? "Claude is working" : blocked;

  // Model and effort choices go to the session's telemetry log.
  const changeChoices = (next: Choices) => {
    if (sessionId) {
      for (const setting of ["model", "effort"] as const) {
        if (next[setting] !== choices[setting]) reportAction(sessionId, { kind: "choice", setting, value: next[setting] });
      }
    }
    choicePicked.current = true;
    setChoices(next);
  };

  // The team standards chosen on the empty chat go with the session's first message.
  const [standards, setStandards] = useState<string | null>(null);
  // The constraints name the session's constraint set as their source (null: the built-in set).
  const standardsName = useStandardsSetName(session?.standards ?? standards);
  // The design's constraints, for the canvas's Constraints panel.
  const constraintsValue = useMemo<ConstraintsValue>(
    () => ({ constraints, onChange: changeConstraint, disabledReason: constraintsLocked, setName: standardsName ?? null }),
    [constraints, changeConstraint, constraintsLocked, standardsName],
  );
  const send = (text: string, files: string[], chosen: Choices) => {
    const first = !sessionId && messages.length === 0;
    chat.send(text, files, chosen, selected?.selection ?? null, selected?.label, first && standards ? { standards } : {});
    setSelection(null);
  };

  // Follow the stream while the user is at the bottom; leave them be if they scrolled up to read.
  const scroller = useRef<HTMLDivElement>(null);
  const atBottom = useRef(true);
  useEffect(() => {
    const el = scroller.current;
    if (el && atBottom.current) el.scrollTop = el.scrollHeight;
  }, [messages]);

  // A prompt from the home page is the first message. The ref keeps Strict Mode from sending it twice.
  const sentInitial = useRef(false);
  useEffect(() => {
    if (sentInitial.current || !(initialPrompt || initialFiles.length)) return;
    sentInitial.current = true;
    chat.send(initialPrompt ?? "", initialFiles, initialChoices);
    // Only on mount: later prop changes remount the workspace (it is keyed).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const stage = (names: string[]) => {
    setFileNote(null);
    setStaged((s) => [...s, ...names.filter((n) => !s.includes(n))]);
  };
  // Files picked from the start buttons or the canvas that were left out (`unsupportedNote`).
  const [fileNote, setFileNote] = useState<string | null>(null);
  const refuse = (names: string[]) => setFileNote(unsupportedNote(names));
  const showChat = !(canvasOpen && canvasMax);
  const showCanvas = () => {
    setCanvasOpen(true);
    setPane("canvas");
  };
  // A check count in the chat opens the canvas (shown first if hidden) on its build's Constraints panel.
  const openChecks = useOpenChecksValue();

  return (
    <SelectionContext.Provider value={context}>
      <ShowLocationContext.Provider value={showRequest}>
      <FoundContext.Provider value={foundRequest}>
      <OpenChecksContext.Provider value={openChecks}>
      <div className="flex h-full min-h-0">
        {/* Hidden, not unmounted, while the canvas is expanded (or shown on a phone), so the draft and model choice survive. */}
        <section
          aria-label="Chat"
          className={`min-w-0 flex-col ${showChat ? "md:flex" : "md:hidden"} ${pane === "chat" ? "max-md:flex" : "max-md:hidden"} ${
            canvasOpen ? "md:w-[400px] md:shrink-0 md:border-r md:border-[var(--cs-border)] xl:w-[500px] max-md:flex-1" : "flex-1"
          }`}
        >
          <header className="flex h-12 shrink-0 items-center gap-1 border-b border-[var(--cs-border)] px-3">
            {!sidebarOpen && (
              <button type="button" aria-label="Show sidebar" onClick={() => setSidebarOpen(true)} className={headerBtn}>
                <PanelLeft className="size-[18px]" strokeWidth={1.6} />
              </button>
            )}
            <h1 className="ml-2 min-w-0 truncate text-[14px]" title={title}>
              {title}
            </h1>
            {sessionId && session && !missing && (
              <SessionStandards
                sessionId={sessionId}
                value={session.standards ?? null}
                disabledReason={loadReason ?? null}
                className="ml-auto"
              />
            )}
            {/* On a phone it switches to the canvas; Close canvas switches back. */}
            <button
              type="button"
              aria-label={canvasOpen && !phone ? "Hide canvas" : "Show canvas"}
              aria-pressed={phone ? undefined : canvasOpen}
              onClick={() => (phone || !canvasOpen ? showCanvas() : setCanvasOpen(false))}
              className={`${headerBtn} size-7 shrink-0`}
            >
              <ArtifactsIcon className="size-4" />
            </button>
          </header>

          <div
            ref={scroller}
            onScroll={(e) => {
              const el = e.currentTarget;
              atBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
            }}
            className="cs-scroll min-h-0 flex-1 overflow-y-auto px-5"
          >
            {missing ? (
              <SessionUnavailable />
            ) : chat.loading ? null : messages.length === 0 ? (
              <EmptyChat
                onPick={(kind) => pickFiles(kind, stage, refuse)}
                standards={standards}
                onStandardsChange={setStandards}
              />
            ) : (
              <RunControlsProvider value={runControls}>
              <ConnectionBanner status={chat.connection} onRetryNow={chat.reconnect} />
              <ChatMessages
                messages={messages}
                builds={builds}
                constraints={constraints}
                busy={busy}
                waiting={waiting}
                wide={!canvasOpen}
                onAnswer={(questionId, option) => answer(questionId, option, choices)}
                onReviewRisk={chat.reviewRisk}
                reviewingRisk={chat.reviewing}
                riskError={chat.riskError}
                onFound={
                  sessionCanvas
                    ? (block, select) => {
                        setCanvasOpen(true);
                        showFound(block, select, found?.key);
                      }
                    : undefined
                }
                onShowRisk={
                  sessionCanvas
                    ? (flag) => {
                        setCanvasOpen(true);
                        setShowRequest((r) => ({ location: flag.location, n: (r?.n ?? 0) + 1 }));
                      }
                    : undefined
                }
                onShowBuild={showBuild}
                onOpenChecks={
                  sessionCanvas
                    ? (buildId, status) => {
                        if (builds.some((b) => b.id === buildId)) showBuild(buildId);
                        showCanvas();
                        openChecks.open(status);
                      }
                    : undefined
                }
                onOpenSession={(id) => router.push(`/session/${encodeURIComponent(id)}`)}
                onStop={sessionId ? chat.stop : undefined}
                stopping={chat.stopping}
                readOnly={shared ? SHARED_BLOCKED : undefined}
              />
              </RunControlsProvider>
            )}
            {/* Messages sent while Claude works, under the running reply. */}
            {!missing && !chat.loading && (
              <QueuedMessages
                queue={chatQueue(chat.queue)}
                wide={!canvasOpen}
                onEdit={chat.editQueued}
                onMove={chat.moveQueued}
                onRemove={chat.removeQueued}
                onSendAnyway={chat.sendAnyway}
              />
            )}
          </div>

          <div className={`mx-auto w-full px-6 pb-3 ${canvasOpen ? "" : "max-w-[760px]"}`}>
            {/* The comment threads Claude is busy with: each its own conversation, taking turns. */}
            <ThreadsPanel
              conversations={chat.conversations}
              busy={busy}
              readOnly={readOnly}
              onAnswer={(questionId, optionId) => chat.answerInThread(questionId, optionId, choices)}
            />
            {/* A shared link takes no messages: it says so and offers its own actions instead. */}
            {!readOnly && <PhotoHint files={staged} />}
            {shared ? (
              <SharedNotice token={shared} />
            ) : (
              <Composer
                variant="reply"
                fileNote={fileNote}
                // Nothing to reply to before the first message: ask for the part instead.
                placeholder={photoPlaceholder(
                  staged,
                  chat.queues ? "Queue a message..." : messages.length === 0 ? "Describe a part or a change..." : "Reply...",
                )}
                files={staged}
                onFilesChange={setStaged}
                choices={choices}
                onChoicesChange={changeChoices}
                selection={selected?.selection}
                selectionLabel={selected?.label}
                onClearSelection={() => setSelection(null)}
                busy={busy && !chat.queues}
                queues={chat.queues}
                blocked={blocked}
                onSubmit={send}
              />
            )}
          </div>
        </section>

        {(canvasOpen || pane === "canvas") && (
          <section
            aria-label="Canvas"
            className={`min-w-0 flex-1 flex-col ${canvasOpen ? "md:flex" : "md:hidden"} ${pane === "canvas" ? "max-md:flex" : "max-md:hidden"}`}
          >
            <header className="flex h-12 shrink-0 items-center gap-1 border-b border-[var(--cs-border)] px-4">
              {!showChat && !sidebarOpen && (
                <button type="button" aria-label="Show sidebar" onClick={() => setSidebarOpen(true)} className={`${headerBtn} max-md:hidden`}>
                  <PanelLeft className="size-[18px]" strokeWidth={1.6} />
                </button>
              )}
              <h2 className="min-w-0 truncate text-[14px]" title={session?.title ?? "Untitled"}>
                {session?.title ?? "Untitled"}
              </h2>
              <SafetyBadge notices={safetyNotices(messages)} />
              <div className="ml-auto flex items-center gap-1">
                {/* A session of one's own is shared by link; a shared link is a page anyone can open. */}
                {readOnly ? <ShareButton /> : sessionId && session && !missing ? <ShareMenu sessionId={sessionId} busy={busy} /> : null}
                <button
                  type="button"
                  aria-label={canvasMax ? "Restore canvas" : "Expand canvas"}
                  aria-pressed={canvasMax}
                  onClick={() => setCanvasMax((v) => !v)}
                  // A phone shows the canvas on its own already.
                  className={`${headerBtn} max-md:hidden`}
                >
                  {canvasMax ? <Minimize2 className="size-4" /> : <Maximize2 className="size-4" />}
                </button>
                <button
                  type="button"
                  aria-label="Close canvas"
                  onClick={() => {
                    setPane("chat");
                    // On a phone it goes back to the chat; the canvas stays open for the desktop layout.
                    if (phone) return;
                    setCanvasOpen(false);
                    setCanvasMax(false);
                  }}
                  className={headerBtn}
                >
                  <X className="size-4" />
                </button>
              </div>
            </header>
            <ConstraintsContext.Provider value={constraintsValue}>
            <CanvasSlot
              onImport={() => pickFiles("any", stage, refuse)}
              // Before the first build the Constraints panel still lists them, to see and edit them.
              overlay={constraints.length > 0 && <ConstraintsBeforeBuild />}
            >
              {canvas ??
                (sessionId && builds.length > 0 && (
                  <HardwareCanvas
                    sessionId={sessionId}
                    builds={builds}
                    selectedId={shownBuild}
                    onSelect={showBuild}
                    fixDisabled={busy || readOnly}
                    fixBlocked={shared ? SHARED_BLOCKED : blocked}
                    readOnly={shared ? SHARED_BLOCKED : undefined}
                    onFixRequest={(fix) => {
                      chat.send(fix.text, [], choices, fix.selection);
                      // On a phone, show the chat where the fix streams in.
                      setPane("chat");
                    }}
                    question={pinnedQuestion(messages)}
                    onAnswer={(questionId, option) => answer(questionId, option, choices)}
                    onSendComments={(ids) => chat.sendComments(ids, choices)}
                    onAnswerInThread={(questionId, optionId) => chat.answerInThread(questionId, optionId, choices)}
                  />
                ))}
            </CanvasSlot>
            </ConstraintsContext.Provider>
          </section>
        )}
      </div>
      </OpenChecksContext.Provider>
      </FoundContext.Provider>
      </ShowLocationContext.Provider>
    </SelectionContext.Provider>
  );
}

function EmptyChat({
  onPick,
  standards,
  onStandardsChange,
}: {
  onPick: (kind: FileKind) => void;
  standards: string | null;
  onStandardsChange: (id: string) => void;
}) {
  return (
    <div className="flex h-full flex-col items-center justify-center pb-10 text-center">
      <EmptyBox />
      <p className="mt-4 max-w-[320px] text-[22px] font-[500] leading-[30px]">{EMPTY_HEADING}</p>
      <StandardsPicker value={standards} onChange={onStandardsChange} />
      <div className="mt-8 flex flex-wrap justify-center gap-1.5">
        {START_OPTIONS.map(({ kind, label, icon: Icon }) => (
          <button
            key={kind}
            type="button"
            onClick={() => onPick(kind)}
            className="flex h-8 items-center gap-1.5 rounded-lg border border-[var(--cs-border)] px-2.5 text-[13px] hover:bg-[var(--cs-hover)]"
          >
            <Icon className="size-3.5 text-[var(--cs-muted)]" />
            {label}
          </button>
        ))}
      </div>
    </div>
  );
}

/** The selected-box drawing from the New mockup. */
function EmptyBox() {
  const stroke = "#8b7cf6";
  const handles: [number, number][] = [
    [10, 30], [56, 4], [118, 40], [72, 66], [10, 64], [72, 102], [118, 74],
  ];
  return (
    <svg viewBox="0 0 140 124" className="h-[124px] w-[140px]" aria-hidden="true">
      <polygon points="10,30 72,66 72,102 10,64" fill="#3a3550" />
      <polygon points="72,66 118,40 118,74 72,102" fill="#2f2b44" />
      <polyline points="10,30 56,4 118,40" fill="none" stroke={stroke} strokeWidth="1.5" strokeDasharray="2 3" />
      <polyline points="10,30 72,66 118,40" fill="none" stroke={stroke} strokeWidth="1.5" />
      <polyline points="10,30 10,64 72,102 118,74 118,40" fill="none" stroke={stroke} strokeWidth="1.5" />
      <line x1="72" y1="66" x2="72" y2="102" stroke={stroke} strokeWidth="1.5" />
      {handles.map(([x, y]) => (
        <rect key={`${x}-${y}`} x={x - 3} y={y - 3} width="6" height="6" rx="1" fill="#1f1f1f" stroke={stroke} />
      ))}
      <path d="M92 104 L108 112 L100 114 L97 122 Z" fill={stroke} />
    </svg>
  );
}
