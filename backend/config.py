"""Configuration, entirely from environment variables.

None of this is persisted to the database: the container is replaceable, and its
only source of truth for credentials is the environment injected into it.
"""
import os
import shlex

# Where the SQLite file lives. In production this is a mounted volume.
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "ping_scheduler.db"
STATIC_DIR = Path(__file__).parent / "static"

# ---- Ping ----

# Configurable so the command can change without redeploying if the CLI's
# authentication mechanism changes. Parsed with shlex (shell quoting rules) but
# NOT executed in a shell: there are no pipes and no variable expansion.
PING_COMMAND = os.environ.get("PING_COMMAND", 'claude -p "ok"')

# A ping that hangs must not block the scheduler forever.
PING_TIMEOUT_SECONDS = int(os.environ.get("PING_TIMEOUT_SECONDS", "120"))

# How much stdout/stderr is kept per run. Enough to see whether the mechanism is
# still alive, not so much that SQLite grows without bound.
OUTPUT_EXCERPT_CHARS = int(os.environ.get("OUTPUT_EXCERPT_CHARS", "2000"))

# The subscription token. Read here to pass to the subprocess, and NEVER written
# to the database or logged in the clear: see runner.redact().
CLAUDE_CODE_OAUTH_TOKEN = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")

# ---- Scheduler ----

# The times you configure are local to this zone. UTC by default so a fresh
# deployment behaves predictably anywhere; set it to your own zone.
TIMEZONE = os.environ.get("SCHEDULER_TIMEZONE", "UTC")

# How many runs the history keeps. Older rows are pruned on every write, so the
# database stays bounded without a cleanup job.
HISTORY_LIMIT = int(os.environ.get("HISTORY_LIMIT", "200"))

# ---- Auth ----

AUTH_USER = os.environ.get("PING_AUTH_USER", "")
AUTH_PASSWORD = os.environ.get("PING_AUTH_PASSWORD", "")


def ping_argv() -> list[str]:
    """The ping command, already tokenised. Raises ValueError if malformed."""
    argv = shlex.split(PING_COMMAND)
    if not argv:
        raise ValueError("PING_COMMAND is empty")
    return argv
