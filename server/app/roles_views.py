"""HTML views for the /roles section: list, install, detail."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .api_admin import install_role
from .auth import require_admin
from .db import get_db
from .models import Admin, Agent, AgentRole
from .utils import relative_time
from .views import templates

router = APIRouter()


ROLE_LABELS = {
    "auth_server": "Authentication Server (Samba AD-DC)",
    "file_server": "File Server (Samba shares + homes)",
}


@router.get("/roles", response_class=HTMLResponse)
def roles_page(
    request: Request,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    rows = (
        db.query(AgentRole)
        .order_by(AgentRole.created_at.desc())
        .all()
    )
    enriched = [
        {
            "row": r,
            "label": ROLE_LABELS.get(r.role_type, r.role_type),
            "created": relative_time(r.created_at),
            "installed": relative_time(r.installed_at) if r.installed_at else "—",
        }
        for r in rows
    ]
    return templates.TemplateResponse(
        request,
        "roles.html",
        {
            "active": "roles",
            "rows": enriched,
            "agents": db.query(Agent).order_by(Agent.hostname).all(),
            "flash": request.session.pop("flash", None),
        },
    )


@router.post("/roles/install")
async def roles_install(
    request: Request,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    form = await request.form()
    role_type = form.get("role_type", "")
    target_agent_id = form.get("target_agent_id", "")

    if role_type == "auth_server":
        payload = {
            "realm": form.get("realm", ""),
            "domain": form.get("domain", ""),
            "admin_password": form.get("admin_password", ""),
            "dns_forwarder": form.get("dns_forwarder", "1.1.1.1") or "1.1.1.1",
        }
    elif role_type == "file_server":
        mode = form.get("mode", "standalone")
        payload = {
            "mode": mode,
            "departments": (form.get("departments") or "").split(),
            "shares_root": form.get("shares_root", "/srv/shares") or "/srv/shares",
            "homes_root": form.get("homes_root", "/srv/homes") or "/srv/homes",
        }
        if mode == "domain":
            payload.update(
                realm=form.get("realm", ""),
                domain=form.get("domain", ""),
                join_password=form.get("join_password", ""),
                dc_ip=form.get("dc_ip", ""),
            )
    else:
        request.session["flash"] = f"Unknown role type '{role_type}'."
        return RedirectResponse("/roles", status_code=303)

    try:
        role = install_role(
            role_type=role_type,
            payload=payload,
            target_agent_id=target_agent_id,
            admin_username=admin.username,
            db=db,
        )
    except HTTPException as e:
        request.session["flash"] = f"Could not install: {e.detail}"
        return RedirectResponse("/roles", status_code=303)

    request.session["flash"] = (
        f"Queued {ROLE_LABELS.get(role.role_type, role.role_type)} install on "
        f"{role.agent.hostname}. Watch progress on the role detail page."
    )
    return RedirectResponse(f"/roles/{role.id}", status_code=303)


@router.get("/roles/{role_id}", response_class=HTMLResponse)
def role_detail(
    role_id: int,
    request: Request,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    role = db.get(AgentRole, role_id)
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    return templates.TemplateResponse(
        request,
        "role_detail.html",
        {
            "active": "roles",
            "role": role,
            "label": ROLE_LABELS.get(role.role_type, role.role_type),
            "created": relative_time(role.created_at),
            "installed": relative_time(role.installed_at) if role.installed_at else "—",
            "flash": request.session.pop("flash", None),
        },
    )


@router.post("/roles/{role_id}/delete")
def role_delete(
    role_id: int,
    request: Request,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    role = db.get(AgentRole, role_id)
    if role:
        db.delete(role)
        db.commit()
        request.session["flash"] = "Role record removed (services on the host were NOT uninstalled)."
    return RedirectResponse("/roles", status_code=303)
