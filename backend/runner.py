"""Runs the ping command and records the result.

The point of a ping is not the content of the reply: it is to open the session
window at a time you chose. So it is enough to store whether the command exited
zero plus an excerpt of its output — enough to notice when the authentication
mechanism has stopped working.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import subprocess
import threading
import time

from . import config, db

log = logging.getLogger("ping_scheduler.runner")

# One ping at a time. If the previous one is still running (long timeout, slow
# network), the new one is dropped rather than overlapping and consuming twice.
_lock = threading.Lock()


def redact(text: str) -> str:
    """Strip the token from a text before it is stored or logged.

    The CLI should never print it, but an error message in a future version
    might, and from there it would land in SQLite and on screen.
    """
    token = config.CLAUDE_CODE_OAUTH_TOKEN
    # Very short (or empty) tokens are left alone: three- or four-character
    # strings would match all over the place and wreck the output.
    if token and len(token) >= 8:
        text = text.replace(token, "***")
    return text


def _child_env() -> dict[str, str]:
    """The subprocess environment: the container's, plus the token."""
    env = os.environ.copy()
    if config.CLAUDE_CODE_OAUTH_TOKEN:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = config.CLAUDE_CODE_OAUTH_TOKEN
    return env


def run_ping(trigger: str = "scheduled") -> dict | None:
    """Run the ping and return the stored row. None if one was already running.

    Raises nothing: any failure is recorded as a failed run, because the history
    is precisely what tells you something broke.
    """
    if not _lock.acquire(blocking=False):
        log.warning("a ping is already running; dropping this one (%s)", trigger)
        return None

    try:
        started = dt.datetime.now(dt.timezone.utc)
        # The day is taken in local time: a ping at 00:30 on Monday belongs to
        # Monday even when it is still Sunday in UTC.
        local_now = dt.datetime.now(_tz())
        # Monotonic clock for the duration: immune to system clock jumps.
        t0 = time.monotonic()

        exit_code: int | None = None
        success = False

        try:
            argv = config.ping_argv()
        except ValueError as e:
            output = f"PING_COMMAND is misconfigured: {e}"
        else:
            try:
                proc = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=config.PING_TIMEOUT_SECONDS,
                    env=_child_env(),
                    # No shell, deliberately: the command is tokenised with
                    # shlex, so there are no pipes or expansions to interpret.
                    shell=False,
                    check=False,
                    # Never inherit the server's stdin: a CLI that prompts for
                    # input should see EOF and give up, not hang waiting.
                    stdin=subprocess.DEVNULL,
                )
            except FileNotFoundError:
                output = f"executable not found: {argv[0]}"
            except subprocess.TimeoutExpired:
                output = f"the command did not finish within {config.PING_TIMEOUT_SECONDS}s"
            except Exception as e:  # noqa: BLE001 - any failure is a failed ping
                output = f"failed to execute: {e}"
            else:
                exit_code = proc.returncode
                success = proc.returncode == 0
                output = _excerpt(proc.stdout, proc.stderr)

        duration_ms = int((time.monotonic() - t0) * 1000)

        row = db.record_run(
            started_at=started.replace(tzinfo=None),
            weekday=local_now.weekday(),
            trigger=trigger,
            success=success,
            exit_code=exit_code,
            duration_ms=duration_ms,
            output=redact(output),
        )
        log.info(
            "ping %s: %s (exit %s, %sms)",
            trigger,
            "ok" if success else "FAILED",
            exit_code,
            duration_ms,
        )
        return row
    finally:
        _lock.release()


def _tz():
    from zoneinfo import ZoneInfo

    try:
        return ZoneInfo(config.TIMEZONE)
    except Exception:
        log.warning("unknown time zone %r; falling back to UTC", config.TIMEZONE)
        return dt.timezone.utc


def _excerpt(stdout: str, stderr: str) -> str:
    """A readable excerpt of the output, trimmed to the configured limit."""
    parts = []
    if stdout.strip():
        parts.append(stdout.strip())
    if stderr.strip():
        parts.append(f"[stderr] {stderr.strip()}")
    text = "\n".join(parts) or "(no output)"

    limit = config.OUTPUT_EXCERPT_CHARS
    if len(text) > limit:
        text = text[:limit] + f"\n... (truncated, {len(text)} characters in total)"
    return text
