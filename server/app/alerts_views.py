"""/alerts page: rule list + add form + recent events."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .alerts import _EVALUATORS
from .auth import require_admin
from .db import get_db
from .models import Admin, AlertEvent, AlertRule
from .utils import relative_time
from .views import templates

router = APIRouter()

KIND_LABELS = {
    "offline": "Device offline > N minutes",
    "disk_full": "Disk usage above %",
    "cpu_high": "CPU usage above %",
    "task_failed": "Any task fails",
}


@router.get("/alerts", response_class=HTMLResponse)
def alerts_page(
    request: Request,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    rules = db.query(AlertRule).order_by(AlertRule.id).all()
    events = (
        db.query(AlertEvent)
        .order_by(AlertEvent.fired_at.desc())
        .limit(100)
        .all()
    )
    return templates.TemplateResponse(
        request,
        "alerts.html",
        {
            "active": "alerts",
            "rules": rules,
            "events": [
                {
                    "row": e,
                    "fired": relative_time(e.fired_at),
                    "resolved": relative_time(e.resolved_at) if e.resolved_at else None,
                }
                for e in events
            ],
            "kind_labels": KIND_LABELS,
            "flash": request.session.pop("flash", None),
        },
    )


@router.post("/alerts/create")
def alerts_create(
    request: Request,
    name: str = Form(...),
    kind: str = Form(...),
    webhook_url: str = Form(""),
    threshold: str = Form(""),
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    if kind not in _EVALUATORS:
        request.session["flash"] = f"Unknown kind '{kind}'."
        return RedirectResponse("/alerts", status_code=303)
    name = name.strip()
    if not name:
        request.session["flash"] = "Name required."
        return RedirectResponse("/alerts", status_code=303)
    params: dict = {}
    if kind == "offline":
        params["minutes_offline"] = int(threshold) if threshold.isdigit() else 60
    elif kind == "disk_full":
        params["disk_pct"] = int(threshold) if threshold.isdigit() else 90
    elif kind == "cpu_high":
        params["cpu_pct"] = int(threshold) if threshold.isdigit() else 95
    db.add(AlertRule(name=name, kind=kind, params=params, webhook_url=webhook_url.strip()))
    db.commit()
    request.session["flash"] = f"Alert rule '{name}' created."
    return RedirectResponse("/alerts", status_code=303)


@router.post("/alerts/{rule_id}/toggle")
def alerts_toggle(
    rule_id: int,
    request: Request,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    r = db.get(AlertRule, rule_id)
    if not r:
        raise HTTPException(status_code=404, detail="Not found")
    r.enabled = not r.enabled
    db.commit()
    return RedirectResponse("/alerts", status_code=303)


@router.post("/alerts/{rule_id}/delete")
def alerts_delete(
    rule_id: int,
    request: Request,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    r = db.get(AlertRule, rule_id)
    if r:
        db.delete(r)
        db.commit()
    return RedirectResponse("/alerts", status_code=303)


@router.post("/alerts/events/{event_id}/resolve")
def alerts_resolve(
    event_id: int,
    request: Request,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    from datetime import datetime, timezone
    e = db.get(AlertEvent, event_id)
    if e and e.resolved_at is None:
        e.resolved_at = datetime.now(timezone.utc)
        db.commit()
    return RedirectResponse("/alerts", status_code=303)
