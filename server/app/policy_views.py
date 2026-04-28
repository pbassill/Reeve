"""Compliance policies UI: list, create, detail, fleet dashboard."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .auth import require_admin
from .db import get_db
from .models import Admin, Agent, ComplianceCheck, Group, Policy, Task
from .utils import relative_time
from .views import templates

router = APIRouter()

VALID_RULE_KINDS = {"package_installed", "package_absent", "service_running", "file_contains"}


def _validate_rules(raw_rules: list) -> list[dict]:
    cleaned: list[dict] = []
    for r in raw_rules:
        if not isinstance(r, dict):
            raise HTTPException(status_code=400, detail="rule must be an object")
        kind = r.get("kind")
        if kind not in VALID_RULE_KINDS:
            raise HTTPException(status_code=400, detail=f"unknown rule kind '{kind}'")
        params = r.get("params") or {}
        if kind in ("package_installed", "package_absent"):
            pkgs = params.get("packages")
            if not isinstance(pkgs, list) or not pkgs or not all(isinstance(p, str) for p in pkgs):
                raise HTTPException(status_code=400, detail=f"{kind} needs params.packages list")
        elif kind == "service_running":
            svcs = params.get("services")
            if not isinstance(svcs, list) or not svcs:
                raise HTTPException(status_code=400, detail="service_running needs params.services list")
        elif kind == "file_contains":
            if not isinstance(params.get("path"), str) or not params["path"].startswith("/"):
                raise HTTPException(status_code=400, detail="file_contains needs absolute params.path")
            if not isinstance(params.get("regex"), str) or not params["regex"]:
                raise HTTPException(status_code=400, detail="file_contains needs params.regex")
        cleaned.append({"id": r.get("id") or uuid.uuid4().hex[:8], "kind": kind, "params": params})
    return cleaned


@router.get("/policies", response_class=HTMLResponse)
def policies_page(
    request: Request,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    rows = db.query(Policy).order_by(Policy.name).all()
    rendered = []
    for p in rows:
        checks = db.query(ComplianceCheck).filter_by(policy_id=p.id).all()
        ok = sum(1 for c in checks if c.status == "ok")
        drift = sum(1 for c in checks if c.status == "drift")
        err = sum(1 for c in checks if c.status == "error")
        rendered.append({
            "row": p,
            "ok": ok,
            "drift": drift,
            "error": err,
            "total": len(checks),
        })
    return templates.TemplateResponse(
        request,
        "policies.html",
        {
            "active": "policies",
            "rows": rendered,
            "groups": db.query(Group).order_by(Group.name).all(),
            "flash": request.session.pop("flash", None),
        },
    )


@router.post("/policies/create")
async def policies_create(
    request: Request,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        request.session["flash"] = "Name required."
        return RedirectResponse("/policies", status_code=303)
    if db.query(Policy).filter_by(name=name).first():
        request.session["flash"] = f"Policy '{name}' already exists."
        return RedirectResponse("/policies", status_code=303)
    raw_rules_text = (form.get("rules_json") or "[]").strip()
    try:
        raw_rules = json.loads(raw_rules_text)
    except json.JSONDecodeError as e:
        request.session["flash"] = f"rules_json: {e}"
        return RedirectResponse("/policies", status_code=303)
    if not isinstance(raw_rules, list):
        request.session["flash"] = "rules_json must be a list."
        return RedirectResponse("/policies", status_code=303)
    try:
        rules = _validate_rules(raw_rules)
    except HTTPException as e:
        request.session["flash"] = f"Rule error: {e.detail}"
        return RedirectResponse("/policies", status_code=303)

    target_group_ids = [int(x) for x in form.getlist("target_group_ids") if x.isdigit()]
    p = Policy(
        name=name,
        description=form.get("description", ""),
        enabled=form.get("enabled") == "on",
        auto_remediate=form.get("auto_remediate") == "on",
        target_group_ids=target_group_ids,
        rules=rules,
    )
    db.add(p)
    db.commit()
    request.session["flash"] = f"Policy '{name}' created with {len(rules)} rule(s)."
    return RedirectResponse(f"/policies/{p.id}", status_code=303)


@router.get("/policies/{policy_id}", response_class=HTMLResponse)
def policy_detail(
    policy_id: int,
    request: Request,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    p = db.get(Policy, policy_id)
    if not p:
        raise HTTPException(status_code=404, detail="Policy not found")
    checks = (
        db.query(ComplianceCheck)
        .filter_by(policy_id=p.id)
        .order_by(ComplianceCheck.last_checked.desc().nullslast())
        .all()
    )
    return templates.TemplateResponse(
        request,
        "policy_detail.html",
        {
            "active": "policies",
            "policy": p,
            "rules_pretty": json.dumps(p.rules, indent=2),
            "checks": [
                {
                    "row": c,
                    "last": relative_time(c.last_checked) if c.last_checked else "never",
                }
                for c in checks
            ],
            "flash": request.session.pop("flash", None),
        },
    )


@router.post("/policies/{policy_id}/check-now")
def policy_check_now(
    policy_id: int,
    request: Request,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Queue a check_compliance task on every device in the policy's target groups."""
    p = db.get(Policy, policy_id)
    if not p:
        raise HTTPException(status_code=404, detail="Policy not found")
    targets: dict[int, Agent] = {}
    if p.target_group_ids:
        for g in db.query(Group).filter(Group.id.in_(p.target_group_ids)).all():
            for a in g.members:
                targets[a.id] = a
    else:
        for a in db.query(Agent).all():
            targets[a.id] = a
    batch_id = uuid.uuid4().hex
    for a in targets.values():
        db.add(Task(
            agent_pk=a.id,
            type="check_compliance",
            payload={"policy_id": p.id, "rules": p.rules},
            created_by=admin.username,
            batch_id=batch_id,
            title=f"compliance: {p.name}",
        ))
    db.commit()
    request.session["flash"] = f"Queued compliance check for {len(targets)} device(s)."
    return RedirectResponse(f"/policies/{p.id}", status_code=303)


@router.post("/policies/{policy_id}/toggle")
def policy_toggle(
    policy_id: int,
    request: Request,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    p = db.get(Policy, policy_id)
    if not p:
        raise HTTPException(status_code=404, detail="Policy not found")
    p.enabled = not p.enabled
    db.commit()
    return RedirectResponse(f"/policies/{p.id}", status_code=303)


@router.post("/policies/{policy_id}/delete")
def policy_delete(
    policy_id: int,
    request: Request,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    p = db.get(Policy, policy_id)
    if p:
        db.delete(p)
        db.commit()
        request.session["flash"] = f"Deleted policy '{p.name}'."
    return RedirectResponse("/policies", status_code=303)
