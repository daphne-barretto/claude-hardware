# One image serving both apps: Next.js (standalone) on $PORT, FastAPI on 127.0.0.1:8000.
# Next.js proxies /api/* to the backend, so only $PORT is exposed.

ARG NODE_VERSION=24-slim
ARG PYTHON_VERSION=3.12-slim

FROM node:${NODE_VERSION} AS node

FROM node AS frontend
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
ENV NEXT_TELEMETRY_DISABLED=1 \
    BACKEND_URL=http://127.0.0.1:8000
# public/ holds optional static assets and may not exist in a checkout.
RUN mkdir -p public && npm run build

FROM python:${PYTHON_VERSION} AS runtime
# OpenCascade (build123d, CadQuery) needs a few system GL/X libraries at import time.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libxrender1 libxext6 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node

WORKDIR /app/backend
COPY backend/pyproject.toml ./
COPY backend/app ./app
COPY backend/geometry ./geometry
RUN pip install --no-cache-dir .
COPY standards.yaml /app/standards.yaml
ENV STANDARDS_PATH=/app/standards.yaml
# The demos: the example board (its revisions and the existing case, which the enclosure
# generator reads at run time), the recorded demo sessions and their requests.
COPY demos /app/demos
ENV DEMOS_DIR=/app/demos
# The session API serves the fixture artifacts for builds the build store doesn't hold.
COPY fixtures /app/fixtures
ENV FIXTURES_DIR=/app/fixtures

WORKDIR /app/frontend
COPY --from=frontend /app/.next/standalone ./
COPY --from=frontend /app/.next/static ./.next/static
COPY --from=frontend /app/public ./public

COPY scripts/start.sh /app/start.sh
RUN useradd --create-home app && chown -R app /app
USER app

# DATA_DIR may start empty: Render's free plan wipes the filesystem on spin-down.
ENV PORT=3000 \
    HOSTNAME=0.0.0.0 \
    DATA_DIR=/app/data \
    NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1
EXPOSE 3000
CMD ["/app/start.sh"]
