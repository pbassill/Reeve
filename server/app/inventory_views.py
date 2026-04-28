"""Inventory: hardware view + software search across the fleet."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .auth import require_admin, require_agent
from .db import get_db
from .models import Admin, Agent, Package, Task
from .utils import relative_time
from .views import templates

router = APIRouter()


# ---------------------------------------------------------------------------
# Admin-facing UI
# ---------------------------------------------------------------------------


@router.get("/inventory", response_class=HTMLResponse)
def inventory_page(
    request: Request,
    q: str = "",
    source: str = "",
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    matches: list[dict] = []
    if q:
        like = f"%{q}%"
        query = db.query(Package).filter(Package.name.ilike(like))
        if source:
            query = query.filter(Package.source == source)
        for pkg in query.order_by(Package.name, Package.version).limit(500).all():
            matches.append({"pkg": pkg, "agent": pkg_agent(db, pkg)})
    agents = db.query(Agent).order_by(Agent.hostname).all()
    return templates.TemplateResponse(
        request,
        "inventory.html",
        {
            "active": "inventory",
            "agents": agents,
            "q": q,
            "source": source,
            "matches": matches,
            "flash": request.session.pop("flash", None),
        },
    )


def pkg_agent(db: Session, pkg: Package) -> Agent | None:
    return db.get(Agent, pkg.agent_pk)


@router.post("/inventory/{agent_id}/refresh")
def inventory_refresh(
    agent_id: str,
    request: Request,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    a = db.query(Agent).filter_by(agent_id=agent_id).first()
    if not a:
        raise HTTPException(status_code=404, detail="Agent not found")
    db.add(Task(
        agent_pk=a.id,
        type="inventory_refresh",
        payload={},
        created_by=admin.username,
        batch_id=uuid.uuid4().hex,
        title="inventory refresh",
    ))
    db.commit()
    request.session["flash"] = f"Queued inventory refresh on {a.hostname}."
    return RedirectResponse("/inventory", status_code=303)


@router.get("/inventory/{agent_id}", response_class=HTMLResponse)
def agent_inventory(
    agent_id: str,
    request: Request,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    a = db.query(Agent).filter_by(agent_id=agent_id).first()
    if not a:
        raise HTTPException(status_code=404, detail="Agent not found")
    pkgs = db.query(Package).filter_by(agent_pk=a.id).order_by(Package.source, Package.name).all()
    return templates.TemplateResponse(
        request,
        "inventory_agent.html",
        {
            "active": "inventory",
            "agent": a,
            "packages": pkgs,
            "packages_updated": relative_time(a.packages_updated_at) if a.packages_updated_at else "never",
            "flash": request.session.pop("flash", None),
        },
    )


# ---------------------------------------------------------------------------
# Agent-facing API: package list upload (called when agent's packages_hash differs)
# ---------------------------------------------------------------------------


@router.post("/api/agents/inventory/packages")
def upload_packages(
    body: dict = Body(...),
    agent: Agent = Depends(require_agent),
    db: Session = Depends(get_db),
) -> dict:
    """Body: {"hash": "...", "packages": [{"source": "apt", "name": "...", "version": "..."}, ...]}"""
    new_hash = body.get("hash") or ""
    incoming = body.get("packages") or []
    if not isinstance(incoming, list):
        raise HTTPException(status_code=400, detail="packages must be a list")

    db.query(Package).filter(Package.agent_pk == agent.id).delete()
    now = datetime.now(timezone.utc)
    for p in incoming:
        if not isinstance(p, dict):
            continue
        name = (p.get("name") or "").strip()
        if not name:
            continue
        db.add(Package(
            agent_pk=agent.id,
            source=(p.get("source") or "apt")[:16],
            name=name[:255],
            version=(p.get("version") or "")[:128],
            last_seen=now,
        ))
    agent.packages_hash = new_hash
    agent.packages_updated_at = now
    db.commit()
    return {"ok": True, "stored": len(incoming)}
