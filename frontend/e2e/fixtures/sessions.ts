import { expect, type APIRequestContext } from "@playwright/test";

// Sessions for the e2e tests. A new session starts empty; the board revision the demo
// walks through is one of the examples (`GET /api/examples`), so tests that need its board and
// design open the first example through the API instead of naming it. The app itself offers no
// examples: people open a demo (`openDemo`), which gives them their own copy of it.

export type Example = { id: string; title: string; description: string };
type BoardRef = { name: string; revision: string };
export type CreatedSession = {
  id: string;
  title: string;
  board: BoardRef | null;
  next_revision: BoardRef | null;
  status: string;
  latest_build_id: string | null;
};

/** The examples the API opens (`{example: id}`); there is at least one. */
export async function listExamples(request: APIRequestContext): Promise<Example[]> {
  const res = await request.get("/api/examples");
  expect(res.ok(), `GET /api/examples: ${res.status()}`).toBe(true);
  const examples = (await res.json()) as Example[];
  expect(examples.length).toBeGreaterThan(0);
  return examples;
}

/** Creates a session from the first example: its board and existing design. */
export async function openExample(request: APIRequestContext): Promise<CreatedSession> {
  const [example] = await listExamples(request);
  const res = await request.post("/api/sessions", { data: { example: example.id } });
  expect(res.status(), `POST /api/sessions {example: ${example.id}}`).toBe(201);
  return (await res.json()) as CreatedSession;
}

/** Opens a demo: this browser's own copy of it (made the first time), with the demo's design, board and builds. */
export async function openDemo(request: APIRequestContext, demoId: string): Promise<CreatedSession & { demo: string | null }> {
  const res = await request.post(`/api/demos/${demoId}/session`);
  expect(res.status(), `POST /api/demos/${demoId}/session`).toBe(200);
  return (await res.json()) as CreatedSession & { demo: string | null };
}

/** Resets a demo: this browser's copy is deleted, so the next `openDemo` makes a fresh one. */
export async function resetDemo(request: APIRequestContext, demoId: string): Promise<void> {
  const res = await request.delete(`/api/demos/${demoId}/session`);
  expect(res.status(), `DELETE /api/demos/${demoId}/session`).toBe(204);
}

/** Creates an empty session: no board, no builds. */
export async function emptySession(request: APIRequestContext): Promise<CreatedSession> {
  const res = await request.post("/api/sessions", { data: {} });
  expect(res.status()).toBe(201);
  return (await res.json()) as CreatedSession;
}
