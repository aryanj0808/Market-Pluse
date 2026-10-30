# Stage 1: build the React dashboard
FROM node:20.18.1-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# Stage 2: one Python image for every service; docker compose picks the entrypoint.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PYTHONUNBUFFERED=1
WORKDIR /app

# dependency layer (cached until a pyproject or the lockfile changes)
COPY pyproject.toml uv.lock .python-version ./
COPY packages/core/pyproject.toml packages/core/
COPY services/pyproject.toml services/
COPY mcp/pyproject.toml mcp/
RUN uv sync --frozen --no-dev --no-install-workspace

COPY packages/core/src packages/core/src
COPY services/src services/src
COPY mcp/src mcp/src
RUN uv sync --frozen --no-dev
COPY --from=web /web/dist /app/web/dist

ENV PATH=/app/.venv/bin:$PATH MP_WEB_DIR=/app/web/dist
RUN useradd --create-home --uid 10001 pulse
USER pulse
