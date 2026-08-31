"""The in-process scheduler.

Triggers are rebuilt wholesale whenever the configuration changes: `rebuild()`
drops every job and recreates them from the database. That is simpler than
synchronising additions and removals one by one, and with seven days of a few
times each the cost is irrelevant. The container never needs restarting for a
change to take effect.
"""
from __future__ import annotations

import logging
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import config, db, runner

log = logging.getLogger("ping_scheduler.scheduler")

# APScheduler follows the cron convention for weekdays. We pass names rather
# than numbers so the result does not depend on where the numbering starts.
_CRON_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

_scheduler: BackgroundScheduler | None = None


def timezone():
    try:
        return ZoneInfo(config.TIMEZONE)
    except Exception:
        log.warning("unknown time zone %r; falling back to UTC", config.TIMEZONE)
        return ZoneInfo("UTC")


def start() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(timezone=timezone())
    _scheduler.start()
    rebuild()


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def is_running() -> bool:
    return _scheduler is not None and _scheduler.running


def rebuild() -> int:
    """Recreate every job from the configuration. Returns how many there are.

    When the global switch is off no jobs are created at all: it is the most
    direct way to guarantee that "off" means off, with no flag to consult from
    inside each run.
    """
    if _scheduler is None:
        return 0

    _scheduler.remove_all_jobs()

    if not db.global_enabled():
        log.info("global switch off: 0 jobs")
        return 0

    count = 0
    for day in db.schedule():
        if not day["enabled"]:
            continue
        for value in day["times"]:
            hour, minute = value.split(":")
            _scheduler.add_job(
                runner.run_ping,
                trigger=CronTrigger(
                    day_of_week=_CRON_DAYS[day["weekday"]],
                    hour=int(hour),
                    minute=int(minute),
                    timezone=timezone(),
                ),
                kwargs={"trigger": "scheduled"},
                id=f"ping-{day['weekday']}-{value}",
                # If the container was down at the scheduled time, do not fire
                # on startup: the window for that time has already passed.
                misfire_grace_time=300,
                coalesce=True,
                max_instances=1,
                replace_existing=True,
            )
            count += 1

    log.info("scheduler rebuilt: %d jobs", count)
    return count


def job_count() -> int:
    return 0 if _scheduler is None else len(_scheduler.get_jobs())


def next_runs(limit: int = 5) -> list[dict]:
    """The next scheduled runs, for display in the interface."""
    if _scheduler is None:
        return []
    jobs = [j for j in _scheduler.get_jobs() if j.next_run_time is not None]
    jobs.sort(key=lambda j: j.next_run_time)
    return [
        {"at": j.next_run_time.isoformat(), "id": j.id} for j in jobs[:limit]
    ]
