"""ping-scheduler — schedules the pings that open your session window.

The app starts the database and the scheduler, serves a static page and exposes
a small API. Everything that changes the configuration calls scheduler.rebuild()
afterwards, which is what makes restarting the container unnecessary.
"""
from __future__ import annotations

import base64
import hmac
import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

from . import config, db, runner, scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("ping_scheduler")

TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    scheduler.start()
    log.info(
        "started | zone=%s | command=%s | token=%s",
        config.TIMEZONE,
        config.PING_COMMAND,
        # Only whether it is set. The value is never logged.
        "present" if config.CLAUDE_CODE_OAUTH_TOKEN else "MISSING",
    )
    yield
    scheduler.shutdown()


app = FastAPI(title="ping-scheduler", lifespan=lifespan)


# ---- Authentication ----
#
# Basic Auth over HTTPS: the minimum worth having in front of an app where
# whoever gets in can change when your session window is consumed and fire
# pings at will. Put it behind a real identity proxy if you have one.

# /api/health is queried by the container healthcheck without credentials. If it
# demanded authentication the deployment would be marked failed and a
# healthcheck-gated rollout would roll back in a loop.
PUBLIC_PATHS = frozenset({"/api/health"})


@app.middleware("http")
async def require_basic_auth(request: Request, call_next):
    """Require Basic Auth on everything except the healthcheck.

    Deliberately a middleware rather than a route dependency: this way it also
    covers the static frontend mounted at "/", which never reaches the router.
    """
    if request.url.path in PUBLIC_PATHS:
        return await call_next(request)

    # With no credentials configured, deny everything. Fail closed: if the
    # secret never reaches the container, a useless app beats an open one.
    if not config.AUTH_USER or not config.AUTH_PASSWORD:
        return Response(
            status_code=503,
            content="Authentication is not configured on the server.",
            media_type="text/plain",
        )

    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")

    if scheme.lower() == "basic":
        try:
            raw = base64.b64decode(token, validate=True)
        except Exception:
            raw = b""

        # RFC 7617 says UTF-8, but some clients still send latin-1, so both
        # readings are accepted.
        for encoding in ("utf-8", "latin-1"):
            try:
                user, _, password = raw.decode(encoding).partition(":")
            except UnicodeDecodeError:
                continue
            # compare_digest avoids leaking, through timing, how many characters
            # were guessed correctly. Compared as bytes: on str it raises
            # TypeError as soon as there is a non-ASCII character.
            if hmac.compare_digest(
                user.encode("utf-8"), config.AUTH_USER.encode("utf-8")
            ) and hmac.compare_digest(
                password.encode("utf-8"), config.AUTH_PASSWORD.encode("utf-8")
            ):
                return await call_next(request)

    return Response(
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="ping-scheduler", charset="UTF-8"'},
    )


# ---- Health ----


@app.get("/api/health")
def health() -> dict:
    """Returns 200 only if the app can actually do its job.

    Used by the container healthcheck: if this fails after a deployment, a
    healthcheck-gated rollout reverts to the previous version. That is why it
    checks the pieces it depends on (writable database, live scheduler) rather
    than merely that the process is up.
    """
    if not db.is_ready():
        raise HTTPException(503, "the database is not responding or is incomplete")
    if not scheduler.is_running():
        raise HTTPException(503, "the scheduler is not running")
    return {"status": "ok"}


# ---- Configuration ----


class TimePayload(BaseModel):
    time: str

    @field_validator("time")
    @classmethod
    def valid_time(cls, v: str) -> str:
        if not TIME_RE.match(v):
            raise ValueError("time must be in 24-hour HH:MM format")
        return v


class EnabledPayload(BaseModel):
    enabled: bool


@app.get("/api/config")
def get_config() -> dict:
    """Everything the page needs to render itself, in one call."""
    return {
        "global_enabled": db.global_enabled(),
        "days": db.schedule(),
        # The zone actually resolved, not the raw setting: if SCHEDULER_TIMEZONE
        # is unknown the scheduler falls back to UTC, and reporting the bad name
        # would both mislabel the times and hand the page an invalid zone.
        "timezone": str(scheduler.timezone()),
        "command": config.PING_COMMAND,
        # Whether the token is set is useful diagnostics; the value never leaves.
        "token_configured": bool(config.CLAUDE_CODE_OAUTH_TOKEN),
        "scheduled_jobs": scheduler.job_count(),
        "next_runs": scheduler.next_runs(),
    }


@app.put("/api/config/global")
def set_global(payload: EnabledPayload) -> dict:
    db.set_global_enabled(payload.enabled)
    scheduler.rebuild()
    return get_config()


@app.put("/api/config/days/{weekday}")
def set_day(weekday: int, payload: EnabledPayload) -> dict:
    _check_weekday(weekday)
    db.set_day_enabled(weekday, payload.enabled)
    scheduler.rebuild()
    return get_config()


@app.post("/api/config/days/{weekday}/times")
def add_day_time(weekday: int, payload: TimePayload) -> dict:
    _check_weekday(weekday)
    db.add_time(weekday, payload.time)
    scheduler.rebuild()
    return get_config()


@app.delete("/api/config/days/{weekday}/times/{value}")
def remove_day_time(weekday: int, value: str) -> dict:
    _check_weekday(weekday)
    if not TIME_RE.match(value):
        raise HTTPException(422, "time must be in 24-hour HH:MM format")
    db.remove_time(weekday, value)
    scheduler.rebuild()
    return get_config()


# ---- Pings ----


@app.post("/api/ping")
def manual_ping() -> dict:
    """Fire a ping right now, touching neither the schedule nor the switch.

    Works with the scheduler paused on purpose: this is the test button for
    checking that the mechanism is still alive.
    """
    row = runner.run_ping(trigger="manual")
    if row is None:
        raise HTTPException(409, "a ping is already running")
    return row


@app.get("/api/history")
def get_history(limit: int = 50) -> list[dict]:
    return db.history(limit=max(1, min(limit, config.HISTORY_LIMIT)))


def _check_weekday(weekday: int) -> None:
    if not 0 <= weekday <= 6:
        raise HTTPException(404, "day out of range (0=Monday .. 6=Sunday)")


# Serve frontend in production
if config.STATIC_DIR.exists():
    app.mount(
        "/", StaticFiles(directory=str(config.STATIC_DIR), html=True), name="static"
    )
