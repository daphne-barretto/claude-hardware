#!/bin/sh
# Start the backend and the frontend; if either exits, stop the container.
set -e
cd /app/backend
# Keep idle sockets longer than the Next.js proxy's agent (5 s), so the proxy never reuses one
# uvicorn has just closed (ECONNRESET, a 500 in the browser; docs/architecture.md).
uvicorn app.main:app --host 127.0.0.1 --port 8000 --timeout-keep-alive 75 &
backend=$!
cd /app/frontend
node server.js &
frontend=$!
trap 'kill $backend $frontend 2>/dev/null' INT TERM
while kill -0 $backend 2>/dev/null && kill -0 $frontend 2>/dev/null; do sleep 2; done
kill $backend $frontend 2>/dev/null || true
exit 1
