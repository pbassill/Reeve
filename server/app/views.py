import base64
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .auth import require_admin
from .config import settings
from .db import get_db
from .models import Admin, Agent, EnrollmentToken, Group, Task
from .utils import aware, format_uptime, relative_time, status_label

import pathlib

TEMPLATE_DIR = pathlib.Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

router = APIRouter()


def _flash(request: Request) -> str | None:
    msg = request.session.pop("flash", None)
    return msg


# --- auth pages ---


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    if request.session.get("user"):
        return RedirectResponse("/devices", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"flash": _flash(request), "error": None})


@router.post("/login", response_class=HTMLResponse)
def do_login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    from .auth import verify_password
    from . import ldap_auth

    admin = db.query(Admin).filter_by(username=username).first()
    authenticated = False
    if admin and admin.auth_source == "local" and admin.password_hash:
        authenticated = verify_password(password, admin.password_hash)
    if not authenticated and ldap_auth.is_configured():
        ldap_result = ldap_auth.authenticate(username, password)
        if ldap_result:
            if not admin:
                admin = Admin(
                    username=username,
                    password_hash=None,
                    auth_source="ldap",
                    display_name=ldap_result.get("display_name", ""),
                )
                db.add(admin)
            elif admin.auth_source == "ldap":
                admin.display_name = ldap_result.get("display_name", admin.display_name)
            else:
                admin = None  # local user trying to log in via LDAP — refuse
            authenticated = admin is not None

    if not authenticated or admin is None:
        return templates.TemplateResponse(
            request, "login.html", {"flash": None, "error": "Invalid credentials"}, status_code=401
        )
    admin.last_login = datetime.now(timezone.utc)
    db.commit()
    request.session["user"] = admin.username
    return RedirectResponse("/devices", status_code=303)


@router.get("/logout")
def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# --- index ---


@router.get("/", include_in_schema=False)
def index(request: Request) -> RedirectResponse:
    if not request.session.get("user"):
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse("/devices", status_code=303)


# --- devices ---


@router.get("/devices", response_class=HTMLResponse)
def devices_page(
    request: Request,
    group: int | None = None,
    q: str | None = None,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    query = db.query(Agent)
    if group:
        query = query.join(Agent.groups).filter(Group.id == group)
    if q:
        like = f"%{q}%"
        query = query.filter((Agent.hostname.ilike(like)) | (Agent.ip_address.ilike(like)))
    agents = query.order_by(Agent.hostname.asc()).all()

    rows = []
    for a in agents:
        mem_pct = int(a.mem_used_mb / a.mem_total_mb * 100) if a.mem_total_mb else 0
        disk_pct = int(a.disk_used_gb / a.disk_total_gb * 100) if a.disk_total_gb else 0
        rows.append({
            "agent": a,
            "status": status_label(a.last_seen),
            "mem_pct": mem_pct,
            "disk_pct": disk_pct,
            "uptime": format_uptime(a.uptime_seconds),
            "last_seen": relative_time(a.last_seen),
        })

    return templates.TemplateResponse(
        request,
        "devices.html",
        {
            "active": "devices",
            "rows": rows,
            "groups": db.query(Group).order_by(Group.name).all(),
            "selected_group_id": group,
            "q": q,
            "flash": _flash(request),
        },
    )


@router.get("/devices/{agent_id}", response_class=HTMLResponse)
def device_detail(
    agent_id: str,
    request: Request,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    a = db.query(Agent).filter_by(agent_id=agent_id).first()
    if not a:
        raise HTTPException(status_code=404, detail="Device not found")
    tasks = (
        db.query(Task)
        .filter(Task.agent_pk == a.id)
        .order_by(Task.id.desc())
        .limit(25)
        .all()
    )
    return templates.TemplateResponse(
        request,
        "device_detail.html",
        {
            "active": "devices",
            "agent": a,
            "status": status_label(a.last_seen),
            "uptime": format_uptime(a.uptime_seconds),
            "last_seen": relative_time(a.last_seen),
            "enrolled": relative_time(a.enrolled_at),
            "tasks": [{"task": t, "created": relative_time(t.created_at)} for t in tasks],
            "flash": _flash(request),
        },
    )


@router.post("/devices/{agent_id}/delete")
def delete_device(
    agent_id: str,
    request: Request,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    a = db.query(Agent).filter_by(agent_id=agent_id).first()
    if a:
        db.delete(a)
        db.commit()
        request.session["flash"] = f"Removed {a.hostname}."
    return RedirectResponse("/devices", status_code=303)


# --- groups ---


@router.get("/groups", response_class=HTMLResponse)
def groups_page(
    request: Request,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "groups.html",
        {
            "active": "groups",
            "groups": db.query(Group).order_by(Group.name).all(),
            "all_agents": db.query(Agent).order_by(Agent.hostname).all(),
            "flash": _flash(request),
        },
    )


@router.post("/groups/create")
def groups_create(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    name = name.strip()
    if not name:
        request.session["flash"] = "Name required."
        return RedirectResponse("/groups", status_code=303)
    if db.query(Group).filter_by(name=name).first():
        request.session["flash"] = f"Group '{name}' already exists."
        return RedirectResponse("/groups", status_code=303)
    db.add(Group(name=name, description=description))
    db.commit()
    request.session["flash"] = f"Created group {name}."
    return RedirectResponse("/groups", status_code=303)


@router.post("/groups/{group_id}/delete")
def groups_delete(
    group_id: int,
    request: Request,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    g = db.get(Group, group_id)
    if g:
        db.delete(g)
        db.commit()
        request.session["flash"] = f"Deleted group {g.name}."
    return RedirectResponse("/groups", status_code=303)


@router.post("/groups/{group_id}/members")
async def groups_set_members(
    group_id: int,
    request: Request,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    form = await request.form()
    selected = form.getlist("agent_ids")
    g = db.get(Group, group_id)
    if not g:
        raise HTTPException(status_code=404, detail="Group not found")
    g.members = db.query(Agent).filter(Agent.agent_id.in_(selected)).all()
    db.commit()
    request.session["flash"] = f"Updated {g.name}: {len(g.members)} member(s)."
    return RedirectResponse("/groups", status_code=303)


# --- tasks ---


def _build_payload(form: dict) -> tuple[str, dict]:
    task_type = form.get("type", "shell")
    if task_type in ("shell", "user_shell"):
        return task_type, {"command": form.get("command", "").strip()}
    if task_type == "apt_install":
        return task_type, {"packages": (form.get("packages") or "").split()}
    if task_type == "apt_remove":
        return task_type, {"packages": (form.get("packages_remove") or form.get("packages") or "").split()}
    if task_type == "snap_install":
        return task_type, {"packages": (form.get("snap_packages") or "").split()}
    if task_type == "snap_remove":
        return task_type, {"packages": (form.get("snap_packages_remove") or form.get("snap_packages") or "").split()}
    if task_type == "flatpak_install":
        return task_type, {
            "app_ids": (form.get("flatpak_apps") or "").split(),
            "remote": (form.get("flatpak_remote") or "flathub").strip(),
        }
    if task_type == "flatpak_remove":
        return task_type, {"app_ids": (form.get("flatpak_apps_remove") or form.get("flatpak_apps") or "").split()}
    if task_type in ("apt_upgrade", "snap_refresh", "flatpak_update", "reboot", "shutdown", "self_update"):
        return task_type, {}
    if task_type == "push_file":
        content = form.get("content", "")
        return task_type, {
            "path": form.get("path", "").strip(),
            "mode": form.get("mode", "0644").strip() or "0644",
            "content_b64": base64.b64encode(content.encode()).decode(),
        }
    if task_type in ("set_wallpaper", "set_wallpaper_user"):
        return task_type, {"url": form.get("wallpaper_url", "").strip()}
    if task_type == "user_gsettings":
        return task_type, {
            "schema": form.get("gs_schema", "").strip(),
            "key": form.get("gs_key", "").strip(),
            "value": form.get("gs_value", "").strip(),
        }
    if task_type == "set_dock_favorites":
        return task_type, {"favorites": (form.get("dock_favorites") or "").split()}
    raise HTTPException(status_code=400, detail=f"Unknown task type {task_type}")


@router.post("/tasks/create")
async def tasks_create_view(
    request: Request,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    form = await request.form()
    form_dict = {k: form.get(k) for k in form.keys()}
    task_type, payload = _build_payload(form_dict)

    target_agent_id = form.get("target_agent_id")
    target_group_id = form.get("target_group_id")
    target_all = form.get("target_all") == "1"

    targets: list[Agent] = []
    if target_all:
        targets = db.query(Agent).all()
    elif target_group_id:
        g = db.get(Group, int(target_group_id))
        if g:
            targets = list(g.members)
    elif target_agent_id:
        a = db.query(Agent).filter_by(agent_id=target_agent_id).first()
        if a:
            targets = [a]

    if not targets:
        request.session["flash"] = "No matching devices to send the task to."
        return RedirectResponse(request.headers.get("referer", "/tasks"), status_code=303)

    from .api_admin import _default_title, VALID_TASK_TYPES, _validate_payload
    import uuid

    if task_type not in VALID_TASK_TYPES:
        raise HTTPException(status_code=400, detail="Bad task type")
    _validate_payload(task_type, payload)
    batch_id = uuid.uuid4().hex
    title = _default_title(task_type, payload)
    for a in targets:
        db.add(Task(
            agent_pk=a.id,
            type=task_type,
            payload=payload,
            created_by=admin.username,
            batch_id=batch_id,
            title=title,
        ))
    db.commit()
    request.session["flash"] = f"Queued '{title}' for {len(targets)} device(s)."
    if target_agent_id and len(targets) == 1:
        return RedirectResponse(f"/devices/{target_agent_id}", status_code=303)
    return RedirectResponse("/tasks", status_code=303)


@router.get("/tasks", response_class=HTMLResponse)
def tasks_page(
    request: Request,
    status: str | None = None,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    query = db.query(Task)
    if status:
        query = query.filter(Task.status == status)
    tasks = query.order_by(Task.id.desc()).limit(200).all()
    return templates.TemplateResponse(
        request,
        "tasks.html",
        {
            "active": "tasks",
            "rows": [{"task": t, "created": relative_time(t.created_at)} for t in tasks],
            "status_filter": status,
            "flash": _flash(request),
        },
    )


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
def task_detail(
    task_id: int,
    request: Request,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    t = db.get(Task, task_id)
    if not t:
        raise HTTPException(status_code=404, detail="Task not found")
    payload_json = json.dumps(t.payload or {}, indent=2)
    return templates.TemplateResponse(
        request,
        "task_detail.html",
        {
            "active": "tasks",
            "task": t,
            "payload_json": payload_json,
            "created": relative_time(t.created_at),
            "dispatched": relative_time(t.dispatched_at) if t.dispatched_at else None,
            "completed": relative_time(t.completed_at) if t.completed_at else None,
            "flash": _flash(request),
        },
    )


@router.post("/tasks/{task_id}/cancel")
def task_cancel(
    task_id: int,
    request: Request,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    t = db.get(Task, task_id)
    if t and t.status == "pending":
        t.status = "failed"
        t.stderr = "Cancelled by admin"
        t.completed_at = datetime.now(timezone.utc)
        db.commit()
        request.session["flash"] = "Task cancelled."
    return RedirectResponse(request.headers.get("referer", "/tasks"), status_code=303)


# --- enrollment ---


@router.get("/enrollment", response_class=HTMLResponse)
def enrollment_page(
    request: Request,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    new_token = request.session.pop("new_token", None)
    tokens_raw = db.query(EnrollmentToken).order_by(EnrollmentToken.id.desc()).all()
    tokens = []
    now = datetime.now(timezone.utc)
    for t in tokens_raw:
        exp = aware(t.expires_at)
        tokens.append({
            "token": t,
            "default_group": t.default_group,
            "created_rel": relative_time(t.created_at),
            "expires_rel": relative_time(t.expires_at) if t.expires_at else "never",
            "expired": bool(exp and exp < now),
        })
    return templates.TemplateResponse(
        request,
        "enrollment.html",
        {
            "active": "enrollment",
            "tokens": tokens,
            "groups": db.query(Group).order_by(Group.name).all(),
            "new_token": new_token,
            "public_url": settings.public_url or str(request.base_url).rstrip("/"),
            "flash": _flash(request),
        },
    )


@router.post("/enrollment/create")
def enrollment_create(
    request: Request,
    label: str = Form(""),
    default_group_id: str = Form(""),
    expires_in_hours: str = Form(""),
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    import secrets
    from datetime import timedelta

    group_id = int(default_group_id) if default_group_id.strip() else None
    if group_id is not None and not db.get(Group, group_id):
        request.session["flash"] = "Invalid default group."
        return RedirectResponse("/enrollment", status_code=303)
    expires_at = None
    if expires_in_hours.strip():
        try:
            hours = int(expires_in_hours)
            if hours > 0:
                expires_at = datetime.now(timezone.utc) + timedelta(hours=hours)
        except ValueError:
            pass
    token_value = secrets.token_urlsafe(24)
    db.add(EnrollmentToken(
        token=token_value,
        label=label.strip(),
        default_group_id=group_id,
        expires_at=expires_at,
    ))
    db.commit()
    request.session["new_token"] = token_value
    return RedirectResponse("/enrollment", status_code=303)


@router.post("/enrollment/{token_id}/revoke")
def enrollment_revoke(
    token_id: int,
    request: Request,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    t = db.get(EnrollmentToken, token_id)
    if t:
        t.revoked = True
        db.commit()
        request.session["flash"] = "Token revoked."
    return RedirectResponse("/enrollment", status_code=303)
