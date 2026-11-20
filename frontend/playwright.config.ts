import { defineConfig, devices } from "@playwright/test";

// Set E2E_BASE_URL to test an already running app (for example the Docker image);
// otherwise Playwright starts the mock session API (backend/app/mock_api.py, serving the
// contract fixtures) and the Next.js dev server proxying /api to it. See docs/architecture.md.
// The app uses localhost because next dev blocks its dev resources, and so hydration,
// for other origins such as 127.0.0.1.
const port = Number(process.env.E2E_PORT ?? 3100);
const mockPort = Number(process.env.E2E_MOCK_PORT ?? 8100);
const baseURL = process.env.E2E_BASE_URL ?? `http://localhost:${port}`;
const mockURL = `http://127.0.0.1:${mockPort}`;
// The real backend with a scripted Claude (backend/app/e2e_api.py), for the live-*.spec.ts tests.
// The dev server sends a request to it when it carries the cookie `e2e-backend=live`.
const livePort = Number(process.env.E2E_LIVE_PORT ?? 8200);
const liveURL = `http://127.0.0.1:${livePort}`;

// The screenshot comparisons (toHaveScreenshot) run only with E2E_VISUAL=1. Their baselines are
// Linux renders (e2e/*-snapshots/*-linux.png), so they can only be checked and refreshed on Linux:
// the "visual baselines" workflow (.github/workflows/visual-baselines.yml) regenerates them on
// ubuntu, and CI checks them when the repository variable E2E_VISUAL is 1. Without it, every other
// assertion in those tests still runs; only the pixel comparison is skipped.
const visual = process.env.E2E_VISUAL === "1";

export default defineConfig({
  testDir: "./e2e",
  ignoreSnapshots: !visual,
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: { baseURL, trace: "retain-on-failure" },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: process.env.E2E_BASE_URL
    ? undefined
    : [
        {
          // MOCK_API_SPEED scales the recorded delays: fast, but still streamed event by event.
          command: `cd ../backend && .venv/bin/uvicorn app.mock_api:app --host 127.0.0.1 --port ${mockPort} --timeout-keep-alive 75`,
          url: `${mockURL}/api/health`,
          env: { MOCK_API_SPEED: process.env.MOCK_API_SPEED ?? "10" },
          reuseExistingServer: !process.env.CI,
          timeout: 60_000,
        },
        {
          command: `cd ../backend && .venv/bin/uvicorn app.e2e_api:app --host 127.0.0.1 --port ${livePort} --timeout-keep-alive 75`,
          url: `${liveURL}/api/health`,
          // One run slot, so live-users.spec.ts sees a run wait in line (unset, it is sized
          // from the machine: docs/architecture.md).
          env: { MAX_AGENT_RUNS: "1" },
          reuseExistingServer: !process.env.CI,
          timeout: 60_000,
        },
        {
          command: `npx next dev --port ${port}`,
          url: baseURL,
          env: { BACKEND_URL: mockURL, E2E_LIVE_BACKEND_URL: liveURL },
          reuseExistingServer: !process.env.CI,
          timeout: 120_000,
        },
      ],
});
