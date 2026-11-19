import { afterEach, describe, expect, it, vi } from "vitest";
import { sendFiles } from "@/components/sites/claude-shell/useChatSession";
import { ImportError, isStepFile } from "@/lib/session-api";

// Importing STEP files: a picked STEP file is imported as a build; other files are attached.

const picked = (name: string) => ({ name, file: new File(["x"], name) });

function stubFetch(respond: (url: string) => Response) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      calls.push(url);
      return respond(url);
    }),
  );
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

describe("importing STEP files", () => {
  it("knows STEP files by their extension", () => {
    expect(["a.step", "B.STP", "c.Step"].map(isStepFile)).toEqual([true, true, true]);
    expect(["a.stl", "step.txt", "a.step.zip"].map(isStepFile)).toEqual([false, false, false]);
  });

  it("imports STEP files and attaches the others", async () => {
    const calls = stubFetch((url) =>
      url.endsWith("/imports")
        ? new Response(JSON.stringify({ id: "build-2" }), { status: 201 })
        : new Response(JSON.stringify({ name: "notes.txt", media_type: "text/plain", url: `${url}/attachment-1` }), { status: 201 }),
    );
    const attachments = await sendFiles("s-1", [picked("case.step"), picked("notes.txt")]);
    expect(calls).toEqual(["/api/sessions/s-1/imports", "/api/sessions/s-1/attachments"]);
    expect(attachments.map((a) => a.name)).toEqual(["notes.txt"]);
  });

  it("a failed import says which file and why", async () => {
    const detail = "case.step is larger than 25 MB, the most a STEP import takes.";
    stubFetch(() => new Response(JSON.stringify({ detail }), { status: 413 }));
    const sent = sendFiles("s-1", [picked("case.step")]);
    await expect(sent).rejects.toBeInstanceOf(ImportError);
    // The server's words already name the file: said as they are (friendly limits).
    await expect(sendFiles("s-1", [picked("case.step")])).rejects.toThrow(new RegExp(`^${detail.replace(/[.]/g, "\\.")}$`));
  });

  it("names the file when the server's words don't", async () => {
    stubFetch(() => new Response(JSON.stringify({ detail: "The file can't be read." }), { status: 422 }));
    await expect(sendFiles("s-1", [picked("case.step")])).rejects.toThrow("Could not import case.step: The file can't be read.");
  });
});
