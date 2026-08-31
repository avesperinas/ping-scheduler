"""Data model and SQLite access.

Four tables:
  - day        one fixed row per weekday (0=Monday .. 6=Sunday)
  - ping_time  the HH:MM times belonging to each day
  - ping_run   the run history
  - setting    key/value pairs, currently just the global switch

The schema is created at startup and the seven days are seeded if missing, so an
empty volume from a fresh deployment is usable without intervention.
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    delete,
    select,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)

from . import config

WEEKDAYS = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]

GLOBAL_ENABLED_KEY = "global_enabled"


class Base(DeclarativeBase):
    pass


class Day(Base):
    """A day of the week. There are always exactly seven rows."""

    __tablename__ = "day"

    weekday: Mapped[int] = mapped_column(Integer, primary_key=True)  # 0=Monday
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    times: Mapped[list[PingTime]] = relationship(
        back_populates="day",
        cascade="all, delete-orphan",
        order_by="PingTime.value",
    )


class PingTime(Base):
    """One "HH:MM" time within a day. Unique per (day, time)."""

    __tablename__ = "ping_time"
    __table_args__ = (UniqueConstraint("weekday", "value", name="uq_day_time"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    weekday: Mapped[int] = mapped_column(
        ForeignKey("day.weekday", ondelete="CASCADE"), nullable=False, index=True
    )
    value: Mapped[str] = mapped_column(String(5), nullable=False)  # "HH:MM"

    day: Mapped[Day] = relationship(back_populates="times")


class PingRun(Base):
    """One execution of the ping command, scheduled or manual.

    `output` holds an already-redacted excerpt of stdout/stderr. The token never
    reaches this table: runner.redact() strips it before the row is built.
    """

    __tablename__ = "ping_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Always UTC. Conversion to local time happens at presentation.
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, index=True)
    weekday: Mapped[int] = mapped_column(Integer, nullable=False)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)  # scheduled|manual
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # None when the process never started at all (timeout, missing binary):
    # there is no exit code to record.
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output: Mapped[str] = mapped_column(Text, nullable=False, default="")


class Setting(Base):
    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255), nullable=False)


_engine = None
_Session: sessionmaker | None = None


def init() -> None:
    """Create the file, the schema, and the seven day rows if absent."""
    global _engine, _Session

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    _engine = create_engine(
        f"sqlite:///{config.DB_PATH}",
        # The scheduler runs jobs on APScheduler threads, not the app's.
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(_engine)
    _Session = sessionmaker(bind=_engine, expire_on_commit=False, future=True)

    with session() as s:
        existing = {d.weekday for d in s.scalars(select(Day)).all()}
        for wd in range(7):
            if wd not in existing:
                s.add(Day(weekday=wd, enabled=True))
        if s.get(Setting, GLOBAL_ENABLED_KEY) is None:
            # Starts switched off on purpose: a freshly created volume must not
            # begin firing pings on a configuration nobody has reviewed yet.
            s.add(Setting(key=GLOBAL_ENABLED_KEY, value="0"))


@contextmanager
def session() -> Iterator:
    """A session that commits on exit and rolls back if anything raises."""
    if _Session is None:
        raise RuntimeError("db.init() has not been called")
    s = _Session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def is_ready() -> bool:
    """For the healthcheck: the database responds and holds all seven days."""
    if _Session is None:
        return False
    try:
        with session() as s:
            return s.query(Day).count() == 7
    except Exception:
        return False


# ---- Configuration reads and writes ----


def global_enabled() -> bool:
    with session() as s:
        row = s.get(Setting, GLOBAL_ENABLED_KEY)
        return bool(row and row.value == "1")


def set_global_enabled(value: bool) -> None:
    with session() as s:
        row = s.get(Setting, GLOBAL_ENABLED_KEY)
        if row is None:
            s.add(Setting(key=GLOBAL_ENABLED_KEY, value="1" if value else "0"))
        else:
            row.value = "1" if value else "0"


def schedule() -> list[dict]:
    """The whole week: seven days, each with its switch and sorted times."""
    with session() as s:
        days = s.scalars(select(Day).order_by(Day.weekday)).all()
        return [
            {
                "weekday": d.weekday,
                "name": WEEKDAYS[d.weekday],
                "enabled": d.enabled,
                "times": sorted(t.value for t in d.times),
            }
            for d in days
        ]


def set_day_enabled(weekday: int, enabled: bool) -> None:
    with session() as s:
        day = s.get(Day, weekday)
        if day is None:
            raise KeyError(weekday)
        day.enabled = enabled


def add_time(weekday: int, value: str) -> None:
    """Add a time. Idempotent: repeating one does not create a duplicate."""
    with session() as s:
        if s.get(Day, weekday) is None:
            raise KeyError(weekday)
        exists = s.scalar(
            select(PingTime).where(PingTime.weekday == weekday, PingTime.value == value)
        )
        if exists is None:
            s.add(PingTime(weekday=weekday, value=value))


def remove_time(weekday: int, value: str) -> None:
    with session() as s:
        s.execute(
            delete(PingTime).where(PingTime.weekday == weekday, PingTime.value == value)
        )


# ---- History ----


def record_run(
    *,
    started_at: dt.datetime,
    weekday: int,
    trigger: str,
    success: bool,
    exit_code: int | None,
    duration_ms: int,
    output: str,
) -> dict:
    """Store a run and prune the history to the configured limit."""
    with session() as s:
        run = PingRun(
            started_at=started_at,
            weekday=weekday,
            trigger=trigger,
            success=success,
            exit_code=exit_code,
            duration_ms=duration_ms,
            output=output,
        )
        s.add(run)
        s.flush()

        # Prune by id: it is monotonic, so this avoids a second pass by date.
        # The id at offset HISTORY_LIMIT-1 counting down from newest is the
        # oldest row worth keeping; everything below it goes.
        cutoff = s.scalar(
            select(PingRun.id)
            .order_by(PingRun.id.desc())
            .limit(1)
            .offset(config.HISTORY_LIMIT - 1)
        )
        if cutoff is not None:
            s.execute(delete(PingRun).where(PingRun.id < cutoff))

        return _run_as_dict(run)


def history(limit: int = 50) -> list[dict]:
    with session() as s:
        runs = s.scalars(
            select(PingRun).order_by(PingRun.id.desc()).limit(limit)
        ).all()
        return [_run_as_dict(r) for r in runs]


def _run_as_dict(run: PingRun) -> dict:
    return {
        "id": run.id,
        # Tagged as UTC explicitly: SQLite returns naive datetimes, and the
        # browser would read them as local time without a zone.
        "started_at": run.started_at.replace(tzinfo=dt.timezone.utc).isoformat(),
        "weekday": run.weekday,
        "day_name": WEEKDAYS[run.weekday],
        "trigger": run.trigger,
        "success": run.success,
        "exit_code": run.exit_code,
        "duration_ms": run.duration_ms,
        "output": run.output,
    }
