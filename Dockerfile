# --- Stage 1: build the Tailwind/TS bundle (needs app/templates for Tailwind's @source scan) ---
FROM node:24-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./frontend/
RUN cd frontend && npm install
COPY frontend/ ./frontend/
COPY app/templates/ ./app/templates/
RUN cd frontend && npm run build          # → /build/app/static/dist/{app.css,app.js}

# --- Stage 2: the Django app (Python 3.13 — Django 5.2's supported range) ---
FROM python:3.13-slim AS app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /uvx /bin/
WORKDIR /app
# Install prod deps only (no dev group) into /app/.venv from the lockfile, using the image's Python 3.13.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --python /usr/local/bin/python
ENV PATH="/app/.venv/bin:$PATH"
COPY app/ /app/
COPY --from=frontend /build/app/static/dist /app/static/dist
# Hash/compress static at build time (WhiteNoise ManifestStaticFilesStorage). No DB needed for this.
RUN DJANGO_DEBUG=0 DJANGO_SECRET_KEY=build DATABASE_URL=sqlite:///:memory: python manage.py collectstatic --noinput
EXPOSE 8000
# migrate is run as a release/deploy step (see DEPLOY.md), not here.
# `-t 60` is gunicorn's old `--timeout 60`, which this server swap would otherwise drop silently.
# It is not decoration: DEPLOY.md §4c and spec/features/arkiv.md both reason about what may and may
# not be served through the app on the basis that a request is cut off at sixty seconds, and
# photo_album defers video transcode to Celery for the same reason. Losing the bound would not
# reopen those decisions so much as quietly invalidate the argument for them.
#
# ONE PROCESS, unlike gunicorn's three. Daphne has no worker model — scaling it means running more
# of these behind the proxy. See DEPLOY.md §4 for what that costs and when to do it.
CMD ["daphne", "--bind", "0.0.0.0", "--port", "8000", "-t", "60", "config.asgi:application"]
