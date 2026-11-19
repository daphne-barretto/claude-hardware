"""Telemetry: one append-only event log per session.

- `app.contracts.telemetry`: the event schema (`contracts/telemetry-event.schema.json`).
- `app.telemetry.log`: where logs go, retention and redaction.
- `app.telemetry.session`: what the session API records.
- `app.telemetry.report`: the product metrics over all recorded sessions.

`docs/architecture.md` describes it all.
"""
