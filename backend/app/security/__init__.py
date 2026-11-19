"""Protections for a public demo that runs on the owner's API key (`docs/security.md`).

- `settings`: every limit's environment variable and default, and the production start check.
- `clients`: who is asking: the client's IP address (trusting `X-Forwarded-For` only from the
  configured proxies) and the owner cookie (`app.owners`).
- `limits`: per-owner and per-IP rate limits on sessions, messages and runs.
- `budget`: the app-wide daily spend cap, kept in the session store's database.
- `scrub`: secrets and email addresses taken out of what people send; the system prompt, keys
  and server details taken out of what Claude says; untrusted content marked as data.
- `web`: the HTTP layer: CSRF, security headers, body size and upload time limits, and the cap
  on open event streams.
- `quota`: disk use per owner, and old exports cleaned up.
"""
