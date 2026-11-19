import { describe, expect, it } from "vitest";
import { NextRequest } from "next/server";
import { OWNER_COOKIE, newOwner, proxy } from "@/proxy";

// A new browser gets its owner with its first page (backend app/owners.py), so the page's
// parallel first API requests share one owner instead of each getting its own.
describe("the owner cookie on a page request", () => {
  it("is set for a browser without one, as the API would set it", () => {
    const res = proxy(new NextRequest("http://localhost/new"));
    const cookie = res.cookies.get(OWNER_COOKIE);
    expect(cookie?.value).toMatch(/^[A-Za-z0-9_-]{16,64}$/);
    expect(cookie).toMatchObject({ httpOnly: true, sameSite: "lax", path: "/", secure: false });
    // The page's own render sees it too.
    expect(res.headers.get("x-middleware-request-cookie") ?? res.headers.get("x-middleware-override-headers")).toBeTruthy();
  });

  it("is Secure behind an HTTPS proxy", () => {
    const res = proxy(new NextRequest("http://localhost/new", { headers: { "x-forwarded-proto": "https" } }));
    expect(res.cookies.get(OWNER_COOKIE)?.secure).toBe(true);
  });

  it("is kept when the browser has one", () => {
    const owner = newOwner();
    const res = proxy(new NextRequest("http://localhost/new", { headers: { cookie: `${OWNER_COOKIE}=${owner}` } }));
    expect(res.cookies.get(OWNER_COOKIE)).toBeUndefined();
  });

  it("is replaced when the one it has isn't a token", () => {
    const res = proxy(new NextRequest("http://localhost/new", { headers: { cookie: `${OWNER_COOKIE}=x` } }));
    expect(res.cookies.get(OWNER_COOKIE)?.value).toMatch(/^[A-Za-z0-9_-]{16,64}$/);
  });

  it("makes a different token each time", () => {
    expect(new Set(Array.from({ length: 50 }, newOwner)).size).toBe(50);
  });
});
