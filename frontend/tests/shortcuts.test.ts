import { describe, expect, it } from "vitest";
import {
  bindingKeys,
  findShortcut,
  isTypingTarget,
  matchesBinding,
  SHORTCUTS,
  type ShortcutAction,
} from "@/components/canvas/shortcuts";

const key = (init: KeyboardEventInit) => new KeyboardEvent("keydown", init);
const all = () => true;
const action = (e: KeyboardEvent, available: (a: ShortcutAction) => boolean = all) =>
  findShortcut(e, available)?.action ?? null;

describe("canvas keymap", () => {
  it("binds every listed shortcut, each action once and no key twice", () => {
    const actions = SHORTCUTS.map((s) => s.action);
    expect(new Set(actions).size).toBe(actions.length);
    for (const a of [
      "view-front", "view-top", "view-right", "view-iso", "fit",
      "tool-select", "tool-measure", "tool-section", "tool-comment",
      "hide", "isolate", "show-all", "undo", "redo", "help",
    ] as ShortcutAction[]) {
      expect(actions).toContain(a);
    }
    const combos = SHORTCUTS.flatMap((s) => s.bindings.map((b) => JSON.stringify(b)));
    expect(new Set(combos).size).toBe(combos.length);
  });

  it("maps keys to actions", () => {
    expect(action(key({ key: "1", code: "Digit1" }))).toBe("view-front");
    expect(action(key({ key: "2", code: "Digit2" }))).toBe("view-top");
    expect(action(key({ key: "3", code: "Digit3" }))).toBe("view-right");
    expect(action(key({ key: "0", code: "Digit0" }))).toBe("view-iso");
    expect(action(key({ key: "f", code: "KeyF" }))).toBe("fit");
    expect(action(key({ key: "?", code: "Slash", shiftKey: true }))).toBe("help");
    expect(action(key({ key: "h", code: "KeyH" }))).toBe("hide");
    expect(action(key({ key: "˙", code: "KeyH", altKey: true }))).toBe("show-all");
    expect(action(key({ key: "z", code: "KeyZ", ctrlKey: true }))).toBe("undo");
    expect(action(key({ key: "z", code: "KeyZ", metaKey: true }))).toBe("undo");
    expect(action(key({ key: "Z", code: "KeyZ", metaKey: true, shiftKey: true }))).toBe("redo");
    expect(action(key({ key: "y", code: "KeyY", ctrlKey: true }))).toBe("redo");
  });

  it("needs the exact modifiers", () => {
    expect(action(key({ key: "f", code: "KeyF", ctrlKey: true }))).toBeNull();
    expect(action(key({ key: "F", code: "KeyF", shiftKey: true }))).toBeNull();
    expect(action(key({ key: "1", code: "Digit1", metaKey: true }))).toBeNull();
    expect(matchesBinding(key({ key: "z", code: "KeyZ" }), { code: "KeyZ", mod: true })).toBe(false);
  });

  it("only fires shortcuts that have an action", () => {
    const viewsOnly = (a: ShortcutAction) => a.startsWith("view-");
    expect(action(key({ key: "1", code: "Digit1" }), viewsOnly)).toBe("view-front");
    expect(action(key({ key: "f", code: "KeyF" }), viewsOnly)).toBeNull();
    expect(action(key({ key: "z", code: "KeyZ", ctrlKey: true }), viewsOnly)).toBeNull();
  });

  it("shows bindings with platform modifiers", () => {
    const redo = SHORTCUTS.find((s) => s.action === "redo")!.bindings[0];
    expect(bindingKeys(redo, false)).toEqual(["Ctrl", "Shift", "Z"]);
    expect(bindingKeys(redo, true)).toEqual(["⌘", "⇧", "Z"]);
    expect(bindingKeys({ key: "?" }, false)).toEqual(["?"]);
  });
});

describe("typing targets", () => {
  it("treats inputs, textareas, selects and editable content as typing", () => {
    document.body.innerHTML = `
      <input id="i" /><textarea id="t"></textarea><select id="s"></select>
      <div id="ce" contenteditable="true"><span id="inner">x</span></div>
      <div id="rt" role="textbox" tabindex="0"></div>
      <button id="b">b</button><div id="d" tabindex="0"></div>
      <div id="off" contenteditable="false"></div>`;
    for (const id of ["i", "t", "s", "ce", "inner", "rt"]) {
      expect(isTypingTarget(document.getElementById(id)), id).toBe(true);
    }
    for (const id of ["b", "d", "off"]) {
      expect(isTypingTarget(document.getElementById(id)), id).toBe(false);
    }
    expect(isTypingTarget(document.body)).toBe(false);
    expect(isTypingTarget(window)).toBe(false);
    expect(isTypingTarget(null)).toBe(false);
  });
});
