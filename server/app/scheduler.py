"""Materialises Schedule rows into Tasks at the right times.

Run as a background asyncio task started in main.py's lifespan.
Wakes every 30s, advances any due schedule, respects maintenance windows.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, time, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from .db import SessionLocal
from .models import Agent, Group, Schedule, Task

log = logging.getLogger(__name__)


def _parse_hhmm(s: str) -> Optional[time]:
    try:
        h, m = s.split(":")
        return time(int(h), int(m))
    except (ValueError, AttributeError):
        return None


def _within_window(now: datetime, start: Optional[str], end: Optional[str]) -> bool:
    if not start or not end:
        return True
    s = _parse_hhmm(start)
    e = _parse_hhmm(end)
    if s is None or e is None:
        return True
    cur = now.time()
    if s <= e:
        return s <= cur < e
    # Crosses midnight (e.g. 22:00–04:00).
    return cur >= s or cur < e


def _compute_next_run(s: Schedule, after: datetime) -> Optional[datetime]:
    if s.cron_expr:
        try:
            from croniter import croniter
        except ImportError:
            log.error("Schedule %s uses cron but croniter is not installed", s.id)
            return None
        try:
            return croniter(s.cron_expr, after).get_next(datetime).astimezone(timezone.utc)
        except (ValueError, KeyError) as e:
            log.error("Invalid cron '%s' on schedule %s: %s", s.cron_expr, s.id, e)
            return None
    if s.interval_seconds and s.interval_seconds > 0:
        return after + timedelta(seconds=s.interval_seconds)
    return None


def compute_initial_next_run(s: Schedule, now: Optional[datetime] = None) -> Optional[datetime]:
    """Public helper for views.py when creating/editing a schedule."""
    return _compute_next_run(s, now or datetime.now(timezone.utc))


def _resolve_targets(s: Schedule, db: Session) -> list[Agent]:
    if s.target_all:
        return db.query(Agent).all()
    out: dict[int, Agent] = {}
    if s.target_agent_ids:
        for a in db.query(Agent).filter(Agent.agent_id.in_(s.target_agent_ids)).all():
            out[a.id] = a
    if s.target_group_ids:
        for g in db.query(Group).filter(Group.id.in_(s.target_group_ids)).all():
            for a in g.members:
                out[a.id] = a
    return list(out.values())


def _materialize(s: Schedule, db: Session) -> int:
    targets = _resolve_targets(s, db)
    if not targets:
        return 0
    batch_id = uuid.uuid4().hex
    title = f"[scheduled:{s.name}] {s.task_type}"
    for a in targets:
        # Per-agent maintenance window override: skip this agent if outside its window.
        if a.maintenance_window_start and a.maintenance_window_end:
            if not _within_window(datetime.now(timezone.utc), a.maintenance_window_start, a.maintenance_window_end):
                continue
        db.add(Task(
            agent_pk=a.id,
            type=s.task_type,
            payload=s.task_payload or {},
            created_by=f"schedule#{s.id}",
            batch_id=batch_id,
            title=title,
        ))
    return len(targets)


def tick(db: Session) -> int:
    """One scheduler iteration. Returns the number of schedules fired."""
    now = datetime.now(timezone.utc)
    fired = 0
    due = (
        db.query(Schedule)
        .filter(Schedule.enabled.is_(True))
        .filter(Schedule.next_run_at.isnot(None))
        .filter(Schedule.next_run_at <= now)
        .all()
    )
    for s in due:
        if not _within_window(now, s.window_start, s.window_end):
            # Defer to the next acceptable minute. Re-eval on the next tick.
            continue
        try:
            count = _materialize(s, db)
        except Exception as e:
            log.exception("Materialising schedule %s failed: %s", s.id, e)
            count = 0
        s.last_run_at = now
        s.next_run_at = _compute_next_run(s, now)
        fired += 1
        log.info("Schedule %s fired -> %d tasks; next at %s", s.id, count, s.next_run_at)
    if fired:
        db.commit()
    return fired


async def run_loop(stop: asyncio.Event, period_seconds: int = 30) -> None:
    log.info("Scheduler loop starting (period=%ss)", period_seconds)
    while not stop.is_set():
        try:
            with SessionLocal() as db:
                tick(db)
        except Exception as e:
            log.exception("Scheduler tick failed: %s", e)
        try:
            await asyncio.wait_for(stop.wait(), timeout=period_seconds)
        except asyncio.TimeoutError:
            pass
    log.info("Scheduler loop exiting")
