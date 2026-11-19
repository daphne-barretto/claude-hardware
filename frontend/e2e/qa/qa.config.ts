import { resolve } from "node:path";
import { defineConfig, devices } from "@playwright/test";

// The QA harness (`make qa`): every state and control of the app on the real
// backend with a scripted Claude (backend/app/e2e_api.py). It never calls the real model.
// Ports: QA_PORT (frontend, 3250), QA_LIVE_PORT (real backend, 8250), QA_MOCK_PORT (mock API,
// 8251, which the dev server needs as its default backend). QA_BASE_URL tests a running app.
const port = Number(process.env.QA_PORT ?? 3250);
const livePort = Number(process.env.QA_LIVE_PORT ?? 8250);
const mockPort = Number(process.env.QA_MOCK_PORT ?? 8251);
const baseURL = process.env.QA_BASE_URL ?? `http://localhost:${port}`;
const out = resolve(__dirname, "../../test-results/qa");

export default defineConfig({
  testDir: ".",
  testMatch: /.*\.qa\.ts$/,
  outputDir: resolve(out, "artifacts"),
  fullyParallel: true,
  workers: Number(process.env.QA_WORKERS ?? 4),
  timeout: 180_000,
  expect: { timeout: 10_000 },
  reporter: [["list"], ["json", { outputFile: resolve(out, `results${process.env.QA_PART ? `-${process.env.QA_PART}` : ""}.json`) }]],
  use: { ...devices["Desktop Chrome"], baseURL, viewport: { width: 1280, height: 800 }, trace: "off", actionTimeout: 15_000 },
  webServer: process.env.QA_BASE_URL
    ? undefined
    : [
        {
          command: `cd ../../../backend && .venv/bin/python -m uvicorn app.mock_api:app --host 127.0.0.1 --port ${mockPort} --timeout-keep-alive 75`,
          url: `http://127.0.0.1:${mockPort}/api/health`,
          env: { MOCK_API_SPEED: "10" },
          reuseExistingServer: true,
          timeout: 60_000,
        },
        {
          // Real builds for designs without a board; the sandbox's Linux limits are optional on macOS.
          // Both backends keep idle sockets longer than the dev server's proxy, as the image and
          // `make dev` do (docs/architecture.md, "Keep-alive").
          command: `cd ../../../backend && .venv/bin/python -m uvicorn app.e2e_api:app --host 127.0.0.1 --port ${livePort} --timeout-keep-alive 75`,
          url: `http://127.0.0.1:${livePort}/api/health`,
          env: {
            GEOMETRY_SANDBOX_KERNEL_LIMITS: "optional",
            DATA_DIR: resolve(out, "data"),
            // Every QA browser comes from 127.0.0.1, one IP for hundreds of sessions: lift the
            // per-IP and per-owner limits (docs/security.md) that would otherwise stop the sweep.
            // `make edge` tests the limits themselves.
            SESSIONS_PER_DAY: "100000",
            RUNS_PER_HOUR: "100000",
            MESSAGES_PER_MINUTE: "100000",
            WRITES_PER_MINUTE: "100000",
            MAX_EVENT_STREAMS_PER_OWNER: "100",
            MAX_EVENT_STREAMS: "2000",
            OWNER_DISK_QUOTA_MB: "100000",
          },
          reuseExistingServer: true,
          timeout: 60_000,
        },
        {
          command: `cd ../.. && npx next dev --port ${port}`,
          url: baseURL,
          env: { BACKEND_URL: `http://127.0.0.1:${mockPort}`, E2E_LIVE_BACKEND_URL: `http://127.0.0.1:${livePort}` },
          reuseExistingServer: true,
          timeout: 120_000,
        },
      ],
});
