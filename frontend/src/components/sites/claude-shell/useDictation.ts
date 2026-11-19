"use client";

import { type RefObject, useCallback, useEffect, useRef, useState } from "react";
import {
  dictationErrorMessage,
  dictationParts,
  type DictationParts,
  METER_BARS,
  partsCaret,
  partsText,
  SILENCE_MS,
  STOP_GRACE_MS,
} from "./dictation";

// Minimal typing for the Web Speech API, which is not in TypeScript's DOM lib everywhere.
type RecognitionResult = ArrayLike<{ transcript: string }> & { isFinal?: boolean };
type Recognition = {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  onresult: ((e: { results: ArrayLike<RecognitionResult> }) => void) | null;
  onend: (() => void) | null;
  onerror: ((e: { error?: string }) => void) | null;
  start: () => void;
  stop: () => void;
  abort?: () => void;
};
type RecognitionCtor = new () => Recognition;

function recognitionCtor(): RecognitionCtor | undefined {
  if (typeof window === "undefined") return undefined;
  const w = window as unknown as { SpeechRecognition?: RecognitionCtor; webkitSpeechRecognition?: RecognitionCtor };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition;
}

/** One dictation, from start until it is kept or cancelled. */
type Session = {
  rec: Recognition;
  before: string;
  after: string;
  final: string;
  interim: string;
  stopping: boolean;
  silence?: ReturnType<typeof setTimeout>;
  grace?: ReturnType<typeof setTimeout>;
  stream?: MediaStream;
  audio?: AudioContext;
  frame?: number;
};

const flat = () => Array<number>(METER_BARS).fill(0);

/**
 * Speech-to-text into the composer, like claude.ai's dictation.
 *
 * Starting snapshots the draft and the cursor. While listening the draft is shown with the
 * dictated words spliced in at the cursor (`parts`, interim words separately so they can be
 * dimmed); the draft itself is untouched until the words are kept, so cancelling removes only
 * what this dictation added. `stop` keeps the words (after the recognizer's last result),
 * `finish` keeps them at once and returns the new draft (for sending), `cancel` discards them.
 * Long silence or the recognizer ending on its own keeps them too.
 *
 * `supported` is false until mounted (so server and client render the same) and stays false in
 * browsers without speech recognition, in which case the mic button is not shown.
 */
export function useDictation(box: RefObject<HTMLTextAreaElement | null>, onKeep: (text: string, caret: number) => void) {
  const [supported, setSupported] = useState(false);
  const [parts, setParts] = useState<DictationParts | null>(null);
  const [startedAt, setStartedAt] = useState(0);
  const [now, setNow] = useState(0);
  const [levels, setLevels] = useState<number[]>(flat);
  const [metered, setMetered] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const session = useRef<Session | null>(null);
  const onKeepRef = useRef(onKeep);

  useEffect(() => {
    onKeepRef.current = onKeep;
  }, [onKeep]);

  const listening = parts !== null;

  /** Ends the session: detaches the recognizer and frees the microphone. Returns it, or null if none. */
  const end = useCallback((s: Session | null, abort: boolean) => {
    if (!s || session.current !== s) return null;
    session.current = null;
    clearTimeout(s.silence);
    clearTimeout(s.grace);
    s.rec.onresult = s.rec.onend = s.rec.onerror = null;
    if (abort) (s.rec.abort ?? s.rec.stop).call(s.rec);
    if (s.frame) cancelAnimationFrame(s.frame);
    s.stream?.getTracks().forEach((t) => t.stop());
    s.audio?.close().catch(() => {});
    setParts(null);
    setLevels(flat());
    setMetered(false);
    return s;
  }, []);

  /** Keeps the words heard so far (interim ones too) and returns the new draft. */
  const keep = useCallback(
    (abort: boolean) => {
      const s = end(session.current, abort);
      if (!s) return null;
      const p = dictationParts(s.before, `${s.final} ${s.interim}`, "", s.after);
      const text = partsText(p);
      onKeepRef.current(text, partsCaret(p));
      box.current?.focus();
      return text;
    },
    [end, box],
  );

  const cancel = useCallback(() => {
    if (end(session.current, true)) box.current?.focus();
  }, [end, box]);

  const stop = useCallback(() => {
    const s = session.current;
    if (!s || s.stopping) return;
    s.stopping = true;
    clearTimeout(s.silence);
    // The recognizer sends its last final result and then ends; don't wait forever for it.
    s.grace = setTimeout(() => keep(true), STOP_GRACE_MS);
    s.rec.stop();
  }, [keep]);

  const finish = useCallback(() => keep(true), [keep]);

  useEffect(() => {
    // Feature detection has to wait for the browser, so this runs after hydration.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setSupported(!!recognitionCtor());
    return () => {
      end(session.current, true);
    };
  }, [end]);

  // The elapsed timer.
  useEffect(() => {
    if (!listening) return;
    const id = setInterval(() => setNow(Date.now()), 250);
    return () => clearInterval(id);
  }, [listening]);

  /** Follows the microphone level for the meter. Blocked access ends dictation with a message. */
  const meter = useCallback(
    (s: Session) => {
      const media = typeof navigator !== "undefined" ? navigator.mediaDevices : undefined;
      if (!media?.getUserMedia) return;
      media.getUserMedia({ audio: true }).then(
        (stream) => {
          if (session.current !== s) return stream.getTracks().forEach((t) => t.stop());
          s.stream = stream;
          const Ctx = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
          if (!Ctx) return;
          const audio = new Ctx();
          s.audio = audio;
          const analyser = audio.createAnalyser();
          analyser.fftSize = 512;
          audio.createMediaStreamSource(stream).connect(analyser);
          const data = new Uint8Array(analyser.fftSize);
          let last = 0;
          const tick = (t: number) => {
            if (session.current !== s) return;
            s.frame = requestAnimationFrame(tick);
            if (t - last < 70) return;
            last = t;
            analyser.getByteTimeDomainData(data);
            let sum = 0;
            for (const v of data) sum += ((v - 128) / 128) ** 2;
            const level = Math.min(1, Math.sqrt(sum / data.length) * 4);
            setLevels((l) => [...l.slice(1), level]);
          };
          setMetered(true);
          s.frame = requestAnimationFrame(tick);
        },
        (err: { name?: string }) => {
          if (session.current !== s) return;
          if (err?.name === "NotAllowedError" || err?.name === "SecurityError") {
            end(s, true);
            setError(dictationErrorMessage("not-allowed"));
          }
          // Otherwise there is just no meter; recognition reports its own errors.
        },
      );
    },
    [end],
  );

  const start = useCallback(() => {
    const Ctor = recognitionCtor();
    const el = box.current;
    if (!Ctor || !el || session.current) return;
    const at = el.selectionEnd ?? el.value.length;
    const rec = new Ctor();
    const s: Session = { rec, before: el.value.slice(0, at), after: el.value.slice(at), final: "", interim: "", stopping: false };
    const show = () => setParts(dictationParts(s.before, s.final, s.interim, s.after));
    const quiet = () => {
      clearTimeout(s.silence);
      s.silence = setTimeout(stop, SILENCE_MS);
    };
    rec.continuous = true;
    rec.interimResults = true;
    rec.lang = navigator.language || "en-US";
    rec.onresult = (e) => {
      const fin: string[] = [];
      const mid: string[] = [];
      for (const r of Array.from(e.results)) (r.isFinal === false ? mid : fin).push(r[0]?.transcript ?? "");
      s.final = fin.join(" ");
      s.interim = mid.join(" ");
      show();
      if (!s.stopping) quiet();
    };
    rec.onerror = (e) => {
      const message = dictationErrorMessage(e?.error ?? "");
      if (message) setError(message);
      if (e?.error === "not-allowed" || e?.error === "service-not-allowed" || e?.error === "audio-capture") end(s, true);
    };
    rec.onend = () => keep(false);
    session.current = s;
    setError(null);
    setStartedAt(Date.now());
    setNow(Date.now());
    show();
    quiet();
    el.focus();
    try {
      rec.start();
    } catch {
      end(s, true);
      return;
    }
    meter(s);
  }, [box, end, keep, meter, stop]);

  const clearError = useCallback(() => setError(null), []);
  const toggle = useCallback(() => (session.current ? stop() : start()), [start, stop]);

  return {
    supported,
    listening,
    parts,
    elapsed: listening ? Math.max(0, now - startedAt) : 0,
    levels,
    metered,
    error,
    clearError,
    start,
    stop,
    finish,
    cancel,
    toggle,
  };
}
