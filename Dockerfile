# --- Stage 1: build the Tailwind/TS bundle (needs app/templates for Tailwind's @source scan) ---
# On the builder's own platform: the bundle is the same CSS/JS for every platform, and esbuild
# crashes under emulation, so an amd64 image built on an arm64 Mac would otherwise fail here.
FROM --platform=$BUILDPLATFORM node:26-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./frontend/
RUN cd frontend && npm install
COPY frontend/ ./frontend/
COPY app/templates/ ./app/templates/
RUN cd frontend && npm run build          # → /build/app/static/dist/{app.css,app.js}

# --- What the build stages and the final image share (Python 3.13 — Django 5.2's supported range) ---
FROM python:3.13-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
# exiv2 (the photo album's metadata copying) links against the system's Expat XML parser, which the
# slim image leaves out.
RUN apt-get update && apt-get install -y --no-install-recommends libexpat1 && rm -rf /var/lib/apt/lists/*
WORKDIR /app

# --- Stage 2: the prod dependencies (no dev group) in /app/.venv, from the lockfile ---
# uv and its download cache stay in this stage. The cache alone was 430 MB of the image when the
# sync ran in the final one.
FROM base AS deps
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /bin/
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --python /usr/local/bin/python

# --- Stage 3: hash/compress static (WhiteNoise ManifestStaticFilesStorage). No DB needed. ---
# In /src, so the final image can take the app without this stage's copy of the venv. app/static
# is emptied afterwards: with DEBUG off WhiteNoise serves staticfiles/ only, and the sources
# (mostly the legacy images) would be a second copy of them. The empty directory stays because
# STATICFILES_DIRS names it, and Django warns on every command when it is missing.
FROM deps AS static
WORKDIR /src
COPY app/ ./
COPY --from=frontend /build/app/static/dist ./static/dist
RUN DJANGO_DEBUG=0 DJANGO_SECRET_KEY=build DATABASE_URL=sqlite:///:memory: python manage.py collectstatic --noinput \
    && rm -rf static && mkdir static

# --- The image: Python, the venv and the app. No uv, no cache, no Node. ---
FROM base AS app
# Two layers, dependencies first: a deploy that changes only code pulls the second one.
COPY --from=deps /app/.venv /app/.venv
COPY --from=static /src/ /app/
EXPOSE 8000
# migrate is run as a release/deploy step (see DEPLOY.md), not here.
# One process; scaling means --workers or more of these (DEPLOY.md §4). No lifespan: Channels'
# router has no handler for it, and uvicorn would log that on every start.
CMD ["uvicorn", "config.asgi:application", "--host", "0.0.0.0", "--port", "8000", "--lifespan", "off", "--timeout-keep-alive", "120"]
