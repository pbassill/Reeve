"""Alert rule engine + webhook delivery.

Periodic sweep checks each enabled rule against the current fleet state
(offline/disk/cpu) and creates AlertEvent rows. A delivery loop posts the
events to each rule's webhook URL (Slack-compatible JSON).
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from .config import settings
from .db import SessionLocal
from .models import Agent, AlertEvent, AlertRule, Task
from .utils import aware

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Rule evaluation
# ---------------------------------------------------------------------------


def _evaluate_offline(rule: AlertRule, db: Session) -> list[dict]:
    """Find agents that have been offline longer than threshold and aren't already alerting."""
    threshold = int(rule.params.get("minutes_offline", 60))
    now = datetime.now(timezone.utc)
    findings: list[dict] = []
    for a in db.query(Agent).all():
        last = aware(a.last_seen)
        if last is None:
            continue
        delta_min = (now - last).total_seconds() / 60.0
        if delta_min < threshold:
            continue
        if _has_open_event(db, rule.id, a.id):
            continue
        findings.append({
            "agent": a,
            "summary": f"{a.hostname} offline for {int(delta_min)}m",
            "details": {"hostname": a.hostname, "minutes_offline": int(delta_min), "ip": a.ip_address},
        })
    return findings


def _evaluate_disk_full(rule: AlertRule, db: Session) -> list[dict]:
    threshold = int(rule.params.get("disk_pct", 90))
    findings: list[dict] = []
    for a in db.query(Agent).all():
        if not a.disk_total_gb:
            continue
        pct = a.disk_used_gb / a.disk_total_gb * 100
        if pct < threshold:
            continue
        if _has_open_event(db, rule.id, a.id):
            continue
        findings.append({
            "agent": a,
            "summary": f"{a.hostname} disk {pct:.0f}% used ({a.disk_used_gb}/{a.disk_total_gb} GB)",
            "details": {"hostname": a.hostname, "disk_pct": round(pct, 1), "used_gb": a.disk_used_gb, "total_gb": a.disk_total_gb},
        })
    return findings


def _evaluate_cpu_high(rule: AlertRule, db: Session) -> list[dict]:
    threshold = float(rule.params.get("cpu_pct", 95))
    findings: list[dict] = []
    for a in db.query(Agent).all():
        if (a.cpu_percent or 0) < threshold:
            continue
        if _has_open_event(db, rule.id, a.id):
            continue
        findings.append({
            "agent": a,
            "summary": f"{a.hostname} CPU {a.cpu_percent:.0f}%",
            "details": {"hostname": a.hostname, "cpu_pct": round(a.cpu_percent, 1)},
        })
    return findings


def _evaluate_task_failed(rule: AlertRule, db: Session) -> list[dict]:
    """Fires once per failed task that hasn't been alerted yet."""
    last_id = int(rule.params.get("_last_seen_task_id", 0))
    findings: list[dict] = []
    new_last = last_id
    failed = (
        db.query(Task)
        .filter(Task.status == "failed", Task.id > last_id)
        .order_by(Task.id.asc())
        .limit(50)
        .all()
    )
    for t in failed:
        new_last = max(new_last, t.id)
        findings.append({
            "agent": t.agent,
            "summary": f"Task #{t.id} failed on {t.agent.hostname}: {t.title}",
            "details": {"task_id": t.id, "type": t.type, "exit_code": t.exit_code, "stderr_tail": (t.stderr or "")[-300:]},
        })
    if new_last != last_id:
        rule.params = {**(rule.params or {}), "_last_seen_task_id": new_last}
    return findings


def _has_open_event(db: Session, rule_id: int, agent_pk: int | None) -> bool:
    return (
        db.query(AlertEvent)
        .filter(AlertEvent.rule_id == rule_id, AlertEvent.agent_pk == agent_pk, AlertEvent.resolved_at.is_(None))
        .first()
        is not None
    )


_EVALUATORS = {
    "offline": _evaluate_offline,
    "disk_full": _evaluate_disk_full,
    "cpu_high": _evaluate_cpu_high,
    "task_failed": _evaluate_task_failed,
}


# ---------------------------------------------------------------------------
# Sweep + dispatch
# ---------------------------------------------------------------------------


def sweep(db: Session) -> int:
    fired = 0
    for rule in db.query(AlertRule).filter_by(enabled=True).all():
        eval_fn = _EVALUATORS.get(rule.kind)
        if not eval_fn:
            continue
        try:
            findings = eval_fn(rule, db)
        except Exception as e:
            log.exception("Alert rule %s eval failed: %s", rule.id, e)
            continue
        for f in findings:
            ev = AlertEvent(
                rule_id=rule.id,
                agent_pk=f["agent"].id if f["agent"] else None,
                summary=f["summary"],
                details=f["details"],
            )
            db.add(ev)
            fired += 1
    if fired:
        db.commit()

    # Auto-resolve offline events when an agent comes back.
    for ev in db.query(AlertEvent).filter(AlertEvent.resolved_at.is_(None)).all():
        if ev.rule.kind == "offline" and ev.agent and ev.agent.last_seen:
            last = aware(ev.agent.last_seen)
            if last and (datetime.now(timezone.utc) - last).total_seconds() < 120:
                ev.resolved_at = datetime.now(timezone.utc)
    db.commit()
    return fired


def deliver(db: Session) -> int:
    pending = (
        db.query(AlertEvent)
        .filter(AlertEvent.delivered.is_(False))
        .order_by(AlertEvent.fired_at.asc())
        .limit(20)
        .all()
    )
    delivered_count = 0
    for ev in pending:
        url = ev.rule.webhook_url
        if not url:
            ev.delivered = True
            delivered_count += 1
            continue
        body = {
            "text": f"[reevectl] {ev.summary}",
            "kind": ev.rule.kind,
            "rule": ev.rule.name,
            "fired_at": ev.fired_at.isoformat(),
            "details": ev.details,
            "public_url": settings.public_url,
        }
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode(),
                method="POST",
                headers={"Content-Type": "application/json", "User-Agent": "reevectl/alerts"},
            )
            urllib.request.urlopen(req, timeout=10).read()
            ev.delivered = True
            delivered_count += 1
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
            log.warning("Webhook delivery failed for event %s: %s", ev.id, e)
    if delivered_count:
        db.commit()
    return delivered_count


async def run_loop(stop: asyncio.Event, period_seconds: int = 60) -> None:
    log.info("Alert loop starting (period=%ss)", period_seconds)
    while not stop.is_set():
        try:
            with SessionLocal() as db:
                sweep(db)
                deliver(db)
        except Exception as e:
            log.exception("Alert sweep failed: %s", e)
        try:
            await asyncio.wait_for(stop.wait(), timeout=period_seconds)
        except asyncio.TimeoutError:
            pass
    log.info("Alert loop exiting")
