import { expect, test } from "@playwright/test";

// /health needs the backend, so this runs against a full app (the Docker image or
// staging, via E2E_BASE_URL), not the frontend-only dev server.
test.skip(!process.env.E2E_BASE_URL, "needs a running app with the backend (set E2E_BASE_URL)");

test("/health reports version, commit and key presence without the key", async ({ request }) => {
  const response = await request.get("/health");
  expect(response.ok()).toBe(true);

  const body = await response.json();
  expect(body.status).toBe("ok");
  expect(typeof body.version).toBe("string");
  expect(typeof body.commit).toBe("string");
  expect(typeof body.api_key_present).toBe("boolean");
  expect(JSON.stringify(body)).not.toMatch(/sk-ant-/);
  // The deployed app serves the real session API; only the mock reports `mock`.
  expect(body.mock).toBeUndefined();

  const expected = process.env.E2E_EXPECT_COMMIT;
  if (expected) expect(body.commit).toBe(expected);
});
