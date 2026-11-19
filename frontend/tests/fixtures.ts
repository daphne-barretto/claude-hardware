// Reads the generated contract files and fixtures (see docs/architecture.md, "Mock API").
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import type { SessionEvent } from "@/lib/session-api";

const root = resolve(__dirname, "../..");

export const readRepoFile = (path: string) => readFileSync(resolve(root, path), "utf8");

/** The scripted session the mock API plays. */
export const recordedEvents: SessionEvent[] = readRepoFile("fixtures/session-events.jsonl")
  .split("\n")
  .filter(Boolean)
  .map((line) => JSON.parse(line).event);

export const sessionOpenApi = JSON.parse(readRepoFile("contracts/session-api.openapi.json"));

/** The scripted can't-satisfy run the mock API plays for `conflict_prompt`. */
export const conflictEvents: SessionEvent[] = readRepoFile("fixtures/conflict-events.jsonl")
  .split("\n")
  .filter(Boolean)
  .map((line) => JSON.parse(line).event);

/** The fixture session the mock API creates (title and board). */
export const sessionFixture = JSON.parse(readRepoFile("fixtures/session.json")) as {
  title: string;
  board: { name: string; revision: string };
  prompt: string;
};

/** The fixture build. */
export const buildFixture = JSON.parse(readRepoFile("fixtures/build.json"));
