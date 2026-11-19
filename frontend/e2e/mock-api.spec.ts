import { expect, test } from "@playwright/test";
import { openExample } from "./fixtures/sessions";

// The mock session API (docs/architecture.md) through the Next.js /api proxy: the recorded run
// streams live to an EventSource in the browser and ends with a build and a question.
test.skip(!!process.env.E2E_BASE_URL, "the mock API runs only with the local dev servers");

type StreamedEvent = { id: string; type: string; data: Record<string, unknown> };

test("the mock API streams the recorded run and serves its build", async ({ page, request }) => {
  const session = await openExample(request);

  await page.goto("/");
  const streamed = page.evaluate(
    (id) =>
      new Promise<StreamedEvent[]>((resolve, reject) => {
        const events: StreamedEvent[] = [];
        const source = new EventSource(`/api/sessions/${id}/events`);
        const types = [
          "user_message",
          "message_start",
          "text",
          "progress",
          "question",
          "build_ready",
          "constraints",
          "plan",
          "message_end",
          "error",
        ];
        for (const type of types) {
          source.addEventListener(type, (e) => {
            const message = e as MessageEvent;
            events.push({ id: message.lastEventId, type, data: JSON.parse(message.data) });
            if (type === "message_end") {
              source.close();
              resolve(events);
            }
          });
        }
        setTimeout(() => {
          source.close();
          reject(new Error(`timed out after ${events.length} events`));
        }, 30_000);
      }),
    session.id,
  );

  const sent = await request.post(`/api/sessions/${session.id}/messages`, {
    data: { text: "Check the current enclosure", effort: "high", mode: "manual" },
  });
  expect(sent.status()).toBe(202);

  const events = await streamed;
  const types = events.map((e) => e.type);
  // The message itself and its constraint chips come first, then the reply, which opens
  // with the plan.
  expect(types.slice(0, 4)).toEqual(["user_message", "constraints", "message_start", "plan"]);
  expect(types).toContain("progress");
  expect(types).toContain("text");
  expect(types.slice(-2)).toEqual(["question", "message_end"]);
  expect(types.indexOf("build_ready")).toBeGreaterThan(0);
  expect(types.indexOf("build_ready")).toBeLessThan(types.indexOf("question"));
  // Streamed live, in order: event IDs increase one by one.
  expect(events.map((e) => Number(e.id))).toEqual(events.map((_, i) => i + 1));

  const progress = events.find((e) => e.type === "progress")!.data;
  expect(progress.text).toMatch(/round 1 of \d+/);
  const question = events.find((e) => e.type === "question")!.data as {
    question_id: string;
    options: { id: string }[];
  };
  expect(question.options.length).toBeGreaterThanOrEqual(2);

  const build = (events.find((e) => e.type === "build_ready")!.data as { build: { mesh_url: string; checks_url: string } }).build;
  const mesh = await (await request.get(build.mesh_url)).json();
  expect(mesh.asset.extras).toMatchObject({ contract: "mesh/1", units: "mm" });
  const checks = await (await request.get(build.checks_url)).json();
  expect(checks.contract).toBe("check-results/1");

  const answered = await request.post(`/api/sessions/${session.id}/messages`, {
    data: { answer: { question_id: question.question_id, option_id: question.options[0].id } },
  });
  expect(answered.status()).toBe(202);
});
