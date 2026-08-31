# syntax=docker/dockerfile:1

# ---------- node ----------
# The default ping runs the Claude Code CLI, which is an npm package requiring
# Node >= 22 (its `engines` field). Node is copied from the official image
# rather than installed via apt: bookworm ships Node 18, which is too old, and
# this way the version is pinned by that image's tag. Both bases are bookworm,
# so the glibc matches.
FROM node:22-bookworm-slim AS node


# ---------- runtime ----------
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm

# Pinned deliberately: a new CLI release should not enter the image without
# someone deciding so. Bumping it is a commit, not a silent rebuild.
ARG CLAUDE_CODE_VERSION=2.1.250
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && npm cache clean --force

# uv as the installer: resolves and installs in seconds. Versions stay pinned in
# requirements.txt, so the result is the same as with pip.
COPY --from=ghcr.io/astral-sh/uv:0.12.7 /uv /usr/local/bin/uv

WORKDIR /app

COPY requirements.txt .
RUN uv pip install --system --no-cache -r requirements.txt

# The app does not need root: it only writes its SQLite file and runs the CLI.
RUN useradd --create-home --uid 10001 appuser

COPY backend/ ./backend/
COPY frontend/ ./backend/static/

# In production a volume is mounted over backend/data. This chown covers running
# the image standalone and leaves the rest of the tree correctly owned.
RUN mkdir -p /app/backend/data && chown -R appuser:appuser /app

USER appuser

# The Claude Code CLI writes its state to $HOME. Without a writable HOME it
# fails at startup, and the data volume only covers backend/data.
ENV HOME=/home/appuser

EXPOSE 8000

# Checks that the app can do its job, not merely that the process is up:
# /api/health verifies the database is writable and the scheduler is alive.
# Written in Python because the slim image ships neither curl nor wget.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"]

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
