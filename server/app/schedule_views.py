"""HTML + form views for /schedules."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .api_admin import VALID_TASK_TYPES, _validate_payload
from .auth import require_admin
from .db import get_db
from .models import Admin, Agent, Group, Schedule
from .scheduler import compute_initial_next_run, tick
from .utils import relative_time
from .views import templates

router = APIRouter()

_HHMM_OK = lambda s: not s or (len(s) == 5 and s[2] == ":" and s[:2].isdigit() and s[3:].isdigit())


def _parse_payload(form: dict) -> dict:
    raw = form.get("task_payload_json", "").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"task_payload_json must be valid JSON: {e}")


@router.get("/schedules", response_class=HTMLResponse)
def schedules_page(
    request: Request,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    rows = db.query(Schedule).order_by(Schedule.name).all()
    return templates.TemplateResponse(
        request,
        "schedules.html",
        {
            "active": "schedules",
            "rows": [
                {
                    "row": s,
                    "last_run": relative_time(s.last_run_at) if s.last_run_at else "—",
                    "next_run": relative_time(s.next_run_at) if s.next_run_at else "—",
                }
                for s in rows
            ],
            "agents": db.query(Agent).order_by(Agent.hostname).all(),
            "groups": db.query(Group).order_by(Group.name).all(),
            "task_types": sorted(VALID_TASK_TYPES),
            "flash": request.session.pop("flash", None),
        },
    )


@router.post("/schedules/create")
async def schedules_create(
    request: Request,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        request.session["flash"] = "Name required."
        return RedirectResponse("/schedules", status_code=303)
    cron_expr = (form.get("cron_expr") or "").strip() or None
    interval_raw = (form.get("interval_seconds") or "").strip()
    interval_seconds = int(interval_raw) if interval_raw.isdigit() else None
    if not cron_expr and not interval_seconds:
        request.session["flash"] = "Provide either a cron expression or an interval (seconds)."
        return RedirectResponse("/schedules", status_code=303)
    if cron_expr and interval_seconds:
        request.session["flash"] = "Use cron OR interval, not both."
        return RedirectResponse("/schedules", status_code=303)

    window_start = (form.get("window_start") or "").strip() or None
    window_end = (form.get("window_end") or "").strip() or None
    if not _HHMM_OK(window_start) or not _HHMM_OK(window_end):
        request.session["flash"] = "Window must be HH:MM."
        return RedirectResponse("/schedules", status_code=303)

    task_type = form.get("task_type", "")
    if task_type not in VALID_TASK_TYPES:
        request.session["flash"] = f"Unknown task type {task_type}."
        return RedirectResponse("/schedules", status_code=303)
    try:
        payload = _parse_payload(dict(form))
        _validate_payload(task_type, payload)
    except HTTPException as e:
        request.session["flash"] = f"Payload invalid: {e.detail}"
        return RedirectResponse("/schedules", status_code=303)

    target_all = form.get("target_all") == "on"
    target_agent_ids = form.getlist("target_agent_ids")
    target_group_ids_raw = form.getlist("target_group_ids")
    target_group_ids = [int(x) for x in target_group_ids_raw if x.isdigit()]
    if not target_all and not target_agent_ids and not target_group_ids:
        request.session["flash"] = "Pick at least one target (or 'all devices')."
        return RedirectResponse("/schedules", status_code=303)

    s = Schedule(
        name=name,
        description=form.get("description", ""),
        cron_expr=cron_expr,
        interval_seconds=interval_seconds,
        window_start=window_start,
        window_end=window_end,
        task_type=task_type,
        task_payload=payload,
        target_all=target_all,
        target_agent_ids=target_agent_ids,
        target_group_ids=target_group_ids,
        created_by=admin.username,
    )
    s.next_run_at = compute_initial_next_run(s)
    db.add(s)
    db.commit()
    request.session["flash"] = f"Schedule '{name}' created. Next run: {s.next_run_at}."
    return RedirectResponse("/schedules", status_code=303)


@router.post("/schedules/{schedule_id}/toggle")
def schedules_toggle(
    schedule_id: int,
    request: Request,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    s = db.get(Schedule, schedule_id)
    if not s:
        raise HTTPException(status_code=404, detail="Schedule not found")
    s.enabled = not s.enabled
    if s.enabled and s.next_run_at is None:
        s.next_run_at = compute_initial_next_run(s)
    db.commit()
    request.session["flash"] = f"Schedule '{s.name}' {'enabled' if s.enabled else 'disabled'}."
    return RedirectResponse("/schedules", status_code=303)


@router.post("/schedules/{schedule_id}/run-now")
def schedules_run_now(
    schedule_id: int,
    request: Request,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    s = db.get(Schedule, schedule_id)
    if not s:
        raise HTTPException(status_code=404, detail="Schedule not found")
    s.next_run_at = datetime.now(timezone.utc)
    db.commit()
    tick(db)  # fire immediately so admin sees the result
    request.session["flash"] = f"Schedule '{s.name}' fired now."
    return RedirectResponse("/schedules", status_code=303)


@router.post("/schedules/{schedule_id}/delete")
def schedules_delete(
    schedule_id: int,
    request: Request,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    s = db.get(Schedule, schedule_id)
    if s:
        name = s.name
        db.delete(s)
        db.commit()
        request.session["flash"] = f"Deleted schedule '{name}'."
    return RedirectResponse("/schedules", status_code=303)
