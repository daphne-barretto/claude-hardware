/**
 * Pure helpers for dictation: where spoken text goes in the draft, and what the
 * recording bar shows. The hook that drives speech recognition is `useDictation.ts`.
 */

/** Listening stops by itself after this long without new speech, keeping the text. */
export const SILENCE_MS = 8000;

/** After stop, how long to wait for the recognizer's last final result before keeping what is shown. */
export const STOP_GRACE_MS = 1500;

/** Bars in the level meter. */
export const METER_BARS = 24;

/** The draft while dictating: the text before the cursor, the dictated words, and the text after it. */
export type DictationParts = {
  /** Existing text before the cursor, plus a space if one is needed. */
  head: string;
  /** Final (recognized) words, capitalized if they start a sentence. */
  final: string;
  /** Words still being recognized, shown dimmed. Starts with a space when it follows final words. */
  interim: string;
  /** A space if one is needed, then the existing text after the cursor. */
  tail: string;
};

const clean = (s: string) => s.replace(/\s+/g, " ").trim();

/**
 * Splices dictated words into the draft at the cursor. Adds a space on either side
 * where the words would otherwise touch existing text, and capitalizes the first word
 * at the start of the draft or after a sentence end. Existing text is never changed.
 */
export function dictationParts(before: string, final: string, interim: string, after: string): DictationParts {
  let f = clean(final);
  let i = clean(interim);
  const spoken = f || i;
  if (!spoken) return { head: before, final: "", interim: "", tail: after };

  const startsSentence = /^\s*$/.test(before) || /[.!?\n]\s*$/.test(before);
  const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);
  if (startsSentence) {
    if (f) f = cap(f);
    else i = cap(i);
  }
  if (f && i) i = ` ${i}`;

  const head = before && !/\s$/.test(before) ? `${before} ` : before;
  const tail = after && !/^[\s.,!?;:)\]}]/.test(after) ? ` ${after}` : after;
  return { head, final: f, interim: i, tail };
}

/** The draft as plain text. */
export const partsText = (p: DictationParts) => p.head + p.final + p.interim + p.tail;

/** Where the cursor goes after the dictated words are kept. */
export const partsCaret = (p: DictationParts) => p.head.length + p.final.length + p.interim.length;

/** "0:07", "1:23", "12:00". */
export function formatElapsed(ms: number) {
  const s = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** The inline message for a speech recognition error, or null for errors that just end dictation. */
export function dictationErrorMessage(error: string): string | null {
  switch (error) {
    case "not-allowed":
    case "service-not-allowed":
      return "Microphone access is blocked. To dictate, allow the microphone for this site in your browser's address bar or site settings, then try again.";
    case "audio-capture":
      return "No microphone was found. Connect one, then try again.";
    case "network":
      return "Dictation needs a network connection. Check your connection, then try again.";
    default:
      return null;
  }
}
