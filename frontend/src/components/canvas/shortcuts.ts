/**
 * The canvas keymap: CAD-style shortcuts for views, tools, visibility and history.
 *
 * Every binding lives here, including those whose feature isn't built yet. The canvas
 * passes a handler per action it supports; a binding without a handler never fires and
 * stays off the shortcut sheet, so a feature turns its shortcut on by adding its handler.
 */

export type ShortcutAction =
  | "view-front"
  | "view-top"
  | "view-right"
  | "view-iso"
  | "fit"
  | "tool-select"
  | "tool-measure"
  | "tool-section"
  | "tool-comment"
  | "hide"
  | "isolate"
  | "show-all"
  | "undo"
  | "redo"
  | "help";

export type ShortcutGroup = "Views" | "Tools" | "Visibility" | "History" | "Help";

/**
 * A key combination. `code` matches the physical key (letters, so Alt and layouts don't
 * change it); `key` matches the produced character (`?`, digits), with Shift not checked.
 * `mod` is Ctrl, or Cmd on macOS.
 */
export type KeyBinding = { code?: string; key?: string; mod?: boolean; shift?: boolean; alt?: boolean };

export type Shortcut = {
  action: ShortcutAction;
  label: string;
  group: ShortcutGroup;
  bindings: KeyBinding[];
};

const letter = (c: string, extra: Omit<KeyBinding, "code" | "key"> = {}): KeyBinding => ({
  code: `Key${c}`,
  ...extra,
});

export const SHORTCUTS: Shortcut[] = [
  { action: "view-front", label: "Front view", group: "Views", bindings: [{ key: "1" }] },
  { action: "view-top", label: "Top view", group: "Views", bindings: [{ key: "2" }] },
  { action: "view-right", label: "Right view", group: "Views", bindings: [{ key: "3" }] },
  { action: "view-iso", label: "Isometric view", group: "Views", bindings: [{ key: "0" }] },
  { action: "fit", label: "Zoom to fit", group: "Views", bindings: [letter("F")] },
  { action: "tool-select", label: "Select", group: "Tools", bindings: [letter("S")] },
  { action: "tool-measure", label: "Measure", group: "Tools", bindings: [letter("M")] },
  { action: "tool-section", label: "Section", group: "Tools", bindings: [letter("X")] },
  { action: "tool-comment", label: "Comment", group: "Tools", bindings: [letter("C")] },
  { action: "hide", label: "Hide selection", group: "Visibility", bindings: [letter("H")] },
  { action: "isolate", label: "Isolate selection", group: "Visibility", bindings: [letter("I")] },
  { action: "show-all", label: "Show all", group: "Visibility", bindings: [letter("H", { alt: true })] },
  { action: "undo", label: "Undo", group: "History", bindings: [letter("Z", { mod: true })] },
  {
    action: "redo",
    label: "Redo",
    group: "History",
    bindings: [letter("Z", { mod: true, shift: true }), letter("Y", { mod: true })],
  },
  { action: "help", label: "Keyboard shortcuts", group: "Help", bindings: [{ key: "?" }] },
];

export const SHORTCUT_GROUPS: ShortcutGroup[] = ["Views", "Tools", "Visibility", "History", "Help"];

type KeyEventLike = Pick<KeyboardEvent, "key" | "code" | "ctrlKey" | "metaKey" | "shiftKey" | "altKey">;

export function matchesBinding(e: KeyEventLike, b: KeyBinding): boolean {
  if (!!b.mod !== (e.ctrlKey || e.metaKey)) return false;
  if (!!b.alt !== e.altKey) return false;
  if (b.code !== undefined) return e.code === b.code && !!b.shift === e.shiftKey;
  return e.key === b.key;
}

/** The shortcut a key event triggers, among those with a handler. */
export function findShortcut(e: KeyEventLike, available: (action: ShortcutAction) => boolean): Shortcut | null {
  return SHORTCUTS.find((s) => available(s.action) && s.bindings.some((b) => matchesBinding(e, b))) ?? null;
}

/** True while the user is typing: focus in an input, textarea, select or editable content. */
export function isTypingTarget(target: EventTarget | null): boolean {
  if (!target || typeof (target as Element).closest !== "function") return false;
  const el = target as HTMLElement;
  if (el.isContentEditable) return true;
  return !!el.closest('input, textarea, select, [contenteditable]:not([contenteditable="false"]), [role="textbox"]');
}

/** A binding as shown on the sheet, e.g. ["Ctrl", "Shift", "Z"] (⌘ on macOS). */
export function bindingKeys(b: KeyBinding, mac: boolean): string[] {
  const keys: string[] = [];
  if (b.mod) keys.push(mac ? "⌘" : "Ctrl");
  if (b.alt) keys.push(mac ? "⌥" : "Alt");
  if (b.shift) keys.push(mac ? "⇧" : "Shift");
  keys.push(b.code ? b.code.replace(/^Key/, "") : b.key!);
  return keys;
}
