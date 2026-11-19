import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { Composer } from "@/components/sites/claude-shell/Composer";
import { DEFAULT_CHOICES } from "@/components/sites/claude-shell/data";
import { ADD_FOLDER, BOARD_FILES, EXPLODE_TOOL, FIND, MEASURE_TOOL, LOAD_REVISION, HISTORY, MANUAL_MODE, MODELING_TOOLS, SELECT_FILTERS, SKETCH } from "@/lib/flags";
import { lockedMode } from "@/lib/session-api";

afterEach(cleanup);

describe("feature flags, off by default", () => {
  it("are all off unless set", () => {
    expect([ADD_FOLDER, MANUAL_MODE, SKETCH, HISTORY, MODELING_TOOLS, SELECT_FILTERS, FIND, LOAD_REVISION, EXPLODE_TOOL, BOARD_FILES, MEASURE_TOOL]).toEqual(Array(11).fill(false));
  });

  it("shows no Manual/Auto toggle in the composer", () => {
    render(
      <Composer
        placeholder="Reply..."
        onSubmit={() => {}}
        choices={DEFAULT_CHOICES}
        onChoicesChange={() => {}}
        files={[]}
        onFilesChange={() => {}}
      />,
    );
    expect(screen.queryByRole("radiogroup", { name: "Mode" })).toBeNull();
    expect(screen.queryByRole("radio", { name: "Manual" })).toBeNull();
  });

  it("sends every message in Auto mode, whatever was chosen before", () => {
    expect(lockedMode({ text: "Hi", mode: "manual" })).toMatchObject({ mode: "auto" });
    expect(lockedMode({ text: "Hi", mode: "auto" })).toMatchObject({ mode: "auto" });
    expect(lockedMode({ text: "Hi", mode: "manual" }, true)).toMatchObject({ mode: "manual" });
  });
});
