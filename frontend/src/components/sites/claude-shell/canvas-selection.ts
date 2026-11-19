"use client";

import { createContext, useContext } from "react";
import type { FindResult } from "@/components/canvas/api";
import type { RiskLocation, Selection } from "@/lib/session-api";

/** The canvas selection the next message carries, and the chip text for it (a feature name, say). */
export type ChatSelection = { selection: Selection; label?: string };
export type SetSelection = (selection: Selection | null, label?: string) => void;

/** The canvas selection: the canvas sets it, and the chat sends it with the next message. */
export const SelectionContext = createContext<{ current: ChatSelection | null; set: SetSelection }>({
  current: null,
  set: () => {},
});

/**
 * A place the chat asks the canvas to show, such as a flagged risk's location: the canvas
 * selects its faces (or its feature) and centres the view on its point. `n` counts the
 * requests, so asking for the same place again shows it again.
 */
export type ShowRequest = { location: RiskLocation; n: number };
export const ShowLocationContext = createContext<ShowRequest | null>(null);

/** For the canvas: the latest place the chat asked it to show. */
export function useShowRequest(): ShowRequest | null {
  return useContext(ShowLocationContext);
}

/** For the canvas inside the workspace: set (or clear) the selection that goes with the next message. */
export function useSetCanvasSelection(): SetSelection {
  return useContext(SelectionContext).set;
}

/** For the canvas: the selection the next message will carry; null once it is sent or cleared. */
export function useCanvasSelection(): Selection | null {
  return useContext(SelectionContext).current?.selection ?? null;
}

/**
 * Faces Claude found in chat for the canvas: it highlights them, or, with `select`, makes
 * them the selection the next message carries. Each request has its own `key`, so the canvas
 * applies it once.
 */
export type FoundRequest = { result: FindResult; buildId: string; select: boolean; key: string; n: number };
export const FoundContext = createContext<FoundRequest | null>(null);

/** For the canvas: the latest faces the chat asked it to highlight or select. */
export function useFoundRequest(): FoundRequest | null {
  return useContext(FoundContext);
}
