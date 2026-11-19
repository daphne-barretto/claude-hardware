import type { NextConfig } from "next";

// The FastAPI backend serves everything under /api. In development and in the
// single Docker image it listens on BACKEND_URL (default localhost:8000), and
// Next.js proxies /api/* to it so the browser only ever talks to one origin.
// /health is the public health check (Render, QA) and maps to /api/health.
const backendUrl = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";
// Playwright only: requests carrying the cookie `e2e-backend=live` go to the real backend
// (backend/app/e2e_api.py) instead of the mock API, so one dev server serves both.
const liveBackendUrl = process.env.E2E_LIVE_BACKEND_URL;

// Security headers for every page (docs/security.md). Next.js inlines its bootstrap scripts, so
// scripts need 'unsafe-inline'; the dev server also needs eval and its websocket. three.js loads
// meshes from data: and blob: URLs and may decode in a worker. Nothing loads from another origin,
// and no other site may frame the app.
const dev = process.env.NODE_ENV !== "production";
export const contentSecurityPolicy = [
  "default-src 'self'",
  `script-src 'self' 'unsafe-inline'${dev ? " 'unsafe-eval'" : ""}`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob:",
  "font-src 'self' data:",
  `connect-src 'self' data: blob:${dev ? " ws: wss:" : ""}`,
  "worker-src 'self' blob:",
  "media-src 'self' blob:",
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "frame-ancestors 'none'",
].join("; ");
export const securityHeaders = [
  { key: "Content-Security-Policy", value: contentSecurityPolicy },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "same-origin" },
  { key: "Permissions-Policy", value: "camera=(), geolocation=(), microphone=(self)" },
];

const nextConfig: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
  experimental: {
    // A STEP import (POST /api/sessions/{id}/imports) answers once the file is read, meshed and
    // checked, which may take up to the backend's import limit (backend/app/imports.py, 600 s);
    // the proxy's default would cut it off after 30 s.
    proxyTimeout: 11 * 60 * 1000,
  },
  async rewrites() {
    const live = liveBackendUrl
      ? [
          {
            source: "/api/:path*",
            has: [{ type: "cookie" as const, key: "e2e-backend", value: "live" }],
            destination: `${liveBackendUrl}/api/:path*`,
          },
        ]
      : [];
    return [
      ...live,
      { source: "/health", destination: `${backendUrl}/api/health` },
      { source: "/api/:path*", destination: `${backendUrl}/api/:path*` },
    ];
  },
};

export default nextConfig;
