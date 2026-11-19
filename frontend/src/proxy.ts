import { NextResponse, type NextRequest } from "next/server";

// The browser's owner cookie (backend/app/owners.py), set with the page itself.
//
// The backend gives a request without the cookie a new owner, and sets it on the response. A page
// makes several API requests as it loads; without a cookie each of them got a different owner,
// and the browser kept whichever came back last. A session created before the last of them
// returned belonged to an owner the browser no longer had, so its first message got a 404 (QA
// sweep). Setting the cookie on the page response means every API request the
// page makes carries the same owner.

/** Must match `OWNER_COOKIE` and its attributes in backend/app/owners.py. */
export const OWNER_COOKIE = "hardware_owner";
const MAX_AGE = 400 * 24 * 3600;
const TOKEN = /^[A-Za-z0-9_-]{16,64}$/;

/** A random token the backend accepts (`_TOKEN` in backend/app/owners.py). */
export function newOwner(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(18));
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_");
}

export function proxy(request: NextRequest) {
  const current = request.cookies.get(OWNER_COOKIE)?.value;
  if (current && TOKEN.test(current)) return NextResponse.next();
  const owner = newOwner();
  // The page's own server render sees the cookie too.
  request.cookies.set(OWNER_COOKIE, owner);
  const response = NextResponse.next({ request });
  const https = request.nextUrl.protocol === "https:" || request.headers.get("x-forwarded-proto") === "https";
  response.cookies.set(OWNER_COOKIE, owner, { maxAge: MAX_AGE, path: "/", httpOnly: true, sameSite: "lax", secure: https });
  return response;
}

export const config = {
  // Pages only: the API sets its own cookie for clients that call it directly, and static files
  // need none.
  matcher: ["/((?!api/|health|_next/|favicon|.*\\.[a-z0-9]+$).*)"],
};
