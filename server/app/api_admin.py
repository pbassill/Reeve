import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .auth import require_admin
from .db import get_db
from .models import Admin, Agent, AgentRole, EnrollmentToken, Group, Task
from .schemas import (
    CreateEnrollmentTokenRequest,
    CreateGroupRequest,
    CreateTaskRequest,
    UpdateGroupMembersRequest,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])

VALID_TASK_TYPES = {
    "shell",
    "apt_install",
    "apt_remove",
    "apt_upgrade",
    "snap_install",
    "snap_remove",
    "snap_refresh",
    "flatpak_install",
    "flatpak_remove",
    "flatpak_update",
    "push_file",
    "reboot",
    "shutdown",
    "set_wallpaper",
    "user_shell",
    "user_gsettings",
    "set_wallpaper_user",
    "set_dock_favorites",
    "self_update",
    "install_auth_server",
    "install_file_server",
}

# Task payload keys that are secrets — scrubbed from the DB row right after
# the agent picks the task up on its first check-in, so the password never
# sits at rest on the manage server.
TASK_PAYLOAD_SECRETS = {"admin_password", "join_password"}


def _validate_payload(task_type: str, payload: dict[str, Any]) -> None:
    if task_type == "shell":
        if not isinstance(payload.get("command"), str) or not payload["command"].strip():
            raise HTTPException(status_code=400, detail="shell task requires non-empty 'command'")
    elif task_type in ("apt_install", "apt_remove", "snap_install", "snap_remove"):
        pkgs = payload.get("packages")
        if not isinstance(pkgs, list) or not pkgs or not all(isinstance(p, str) and p for p in pkgs):
            raise HTTPException(status_code=400, detail=f"{task_type} requires 'packages' list")
    elif task_type in ("flatpak_install", "flatpak_remove"):
        ids = payload.get("app_ids")
        if not isinstance(ids, list) or not ids or not all(isinstance(p, str) and p for p in ids):
            raise HTTPException(status_code=400, detail=f"{task_type} requires 'app_ids' list")
    elif task_type == "push_file":
        if not isinstance(payload.get("path"), str) or not payload["path"].startswith("/"):
            raise HTTPException(status_code=400, detail="push_file requires absolute 'path'")
        if not isinstance(payload.get("content_b64"), str):
            raise HTTPException(status_code=400, detail="push_file requires 'content_b64'")
    elif task_type in ("set_wallpaper", "set_wallpaper_user"):
        if not isinstance(payload.get("url"), str) or not payload["url"]:
            raise HTTPException(status_code=400, detail=f"{task_type} requires 'url'")
    elif task_type == "user_shell":
        if not isinstance(payload.get("command"), str) or not payload["command"].strip():
            raise HTTPException(status_code=400, detail="user_shell requires 'command'")
    elif task_type == "user_gsettings":
        for k in ("schema", "key", "value"):
            if not isinstance(payload.get(k), str) or not payload[k]:
                raise HTTPException(status_code=400, detail=f"user_gsettings requires '{k}'")
    elif task_type == "set_dock_favorites":
        favs = payload.get("favorites")
        if not isinstance(favs, list) or not all(isinstance(f, str) and f for f in favs):
            raise HTTPException(status_code=400, detail="set_dock_favorites requires 'favorites' list")
    elif task_type == "install_auth_server":
        _validate_role_auth_server(payload)
    elif task_type == "install_file_server":
        _validate_role_file_server(payload)


_REALM_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]*(\.[A-Z0-9][A-Z0-9-]*)+$")
_NETBIOS_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{0,14}$")
_DEPT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_IP_RE = re.compile(r"^(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)$")


def _validate_role_auth_server(p: dict[str, Any]) -> None:
    realm = (p.get("realm") or "").strip().upper()
    domain = (p.get("domain") or "").strip().upper()
    pwd = p.get("admin_password") or ""
    fwd = (p.get("dns_forwarder") or "1.1.1.1").strip()
    if not _REALM_RE.match(realm):
        raise HTTPException(status_code=400, detail="realm must look like EXAMPLE.LOCAL")
    if not _NETBIOS_RE.match(domain):
        raise HTTPException(status_code=400, detail="domain must be a NetBIOS name (1-15 chars, alphanumeric)")
    if len(pwd) < 8:
        raise HTTPException(status_code=400, detail="admin_password must be at least 8 characters")
    if not _IP_RE.match(fwd):
        raise HTTPException(status_code=400, detail="dns_forwarder must be a valid IPv4 address")
    p["realm"] = realm
    p["domain"] = domain
    p["dns_forwarder"] = fwd


def _validate_role_file_server(p: dict[str, Any]) -> None:
    mode = (p.get("mode") or "standalone").strip().lower()
    if mode not in ("standalone", "domain"):
        raise HTTPException(status_code=400, detail="mode must be 'standalone' or 'domain'")
    departments = p.get("departments") or []
    if not isinstance(departments, list) or not all(_DEPT_RE.match(d or "") for d in departments):
        raise HTTPException(
            status_code=400,
            detail="departments must be a list of lowercase names (a-z 0-9 -, max 32 chars)",
        )
    homes_root = p.get("homes_root") or "/srv/homes"
    shares_root = p.get("shares_root") or "/srv/shares"
    if not isinstance(homes_root, str) or not homes_root.startswith("/"):
        raise HTTPException(status_code=400, detail="homes_root must be an absolute path")
    if not isinstance(shares_root, str) or not shares_root.startswith("/"):
        raise HTTPException(status_code=400, detail="shares_root must be an absolute path")
    p["mode"] = mode
    p["homes_root"] = homes_root
    p["shares_root"] = shares_root
    p["departments"] = list(dict.fromkeys(departments))  # dedupe, preserve order
    if mode == "domain":
        realm = (p.get("realm") or "").strip().upper()
        domain = (p.get("domain") or "").strip().upper()
        if not _REALM_RE.match(realm):
            raise HTTPException(status_code=400, detail="realm required for domain mode (EXAMPLE.LOCAL)")
        if not _NETBIOS_RE.match(domain):
            raise HTTPException(status_code=400, detail="domain (NetBIOS name) required for domain mode")
        if len(p.get("join_password") or "") < 1:
            raise HTTPException(status_code=400, detail="join_password required for domain mode")
        dc_ip = (p.get("dc_ip") or "").strip()
        if dc_ip and not _IP_RE.match(dc_ip):
            raise HTTPException(status_code=400, detail="dc_ip must be a valid IPv4 address")
        p["realm"] = realm
        p["domain"] = domain
        p["dc_ip"] = dc_ip


def _resolve_targets(req: CreateTaskRequest, db: Session) -> list[Agent]:
    if req.target_all:
        return db.query(Agent).all()
    agents: dict[int, Agent] = {}
    if req.target_agent_ids:
        for a in db.query(Agent).filter(Agent.agent_id.in_(req.target_agent_ids)).all():
            agents[a.id] = a
    if req.target_group_ids:
        groups = db.query(Group).filter(Group.id.in_(req.target_group_ids)).all()
        for g in groups:
            for a in g.members:
                agents[a.id] = a
    return list(agents.values())


@router.post("/tasks")
def create_task(
    req: CreateTaskRequest,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    if req.type not in VALID_TASK_TYPES:
        raise HTTPException(status_code=400, detail=f"Unknown task type '{req.type}'")
    _validate_payload(req.type, req.payload)
    targets = _resolve_targets(req, db)
    if not targets:
        raise HTTPException(status_code=400, detail="No target agents resolved")

    batch_id = uuid.uuid4().hex
    title = req.title or _default_title(req.type, req.payload)
    created_ids: list[int] = []
    for agent in targets:
        task = Task(
            agent_pk=agent.id,
            type=req.type,
            payload=req.payload,
            created_by=admin.username,
            batch_id=batch_id,
            title=title,
        )
        db.add(task)
        db.flush()
        created_ids.append(task.id)
    db.commit()
    return {"batch_id": batch_id, "task_ids": created_ids, "count": len(created_ids)}


def _default_title(task_type: str, payload: dict[str, Any]) -> str:
    if task_type in ("shell", "user_shell"):
        return f"{task_type}: {(payload.get('command') or '')[:60]}"
    if task_type in ("apt_install", "apt_remove", "snap_install", "snap_remove"):
        return f"{task_type}: {' '.join(payload.get('packages', []))[:60]}"
    if task_type in ("flatpak_install", "flatpak_remove"):
        return f"{task_type}: {' '.join(payload.get('app_ids', []))[:60]}"
    if task_type in ("apt_upgrade", "snap_refresh", "flatpak_update", "self_update"):
        return task_type.replace("_", " ")
    if task_type == "push_file":
        return f"push: {payload.get('path', '')}"
    if task_type in ("set_wallpaper", "set_wallpaper_user"):
        return f"wallpaper: {payload.get('url', '')[:60]}"
    if task_type == "user_gsettings":
        return f"gsettings {payload.get('schema', '')} {payload.get('key', '')}"
    if task_type == "set_dock_favorites":
        return f"dock favorites ({len(payload.get('favorites', []))})"
    if task_type == "install_auth_server":
        return f"install auth server (realm={payload.get('realm', '')})"
    if task_type == "install_file_server":
        mode = payload.get("mode", "standalone")
        n = len(payload.get("departments") or [])
        return f"install file server ({mode}, {n} dept(s))"
    return task_type


@router.post("/groups")
def create_group(
    req: CreateGroupRequest,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="Name required")
    if db.query(Group).filter_by(name=req.name).first():
        raise HTTPException(status_code=409, detail="Group already exists")
    group = Group(name=req.name.strip(), description=req.description)
    db.add(group)
    db.commit()
    return {"id": group.id, "name": group.name}


@router.delete("/groups/{group_id}")
def delete_group(
    group_id: int,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    g = db.get(Group, group_id)
    if not g:
        raise HTTPException(status_code=404, detail="Group not found")
    db.delete(g)
    db.commit()
    return {"ok": True}


@router.put("/groups/{group_id}/members")
def update_group_members(
    group_id: int,
    req: UpdateGroupMembersRequest,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    group = db.get(Group, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    agents = db.query(Agent).filter(Agent.agent_id.in_(req.agent_ids)).all()
    group.members = agents
    db.commit()
    return {"ok": True, "count": len(agents)}


def install_role(
    *,
    role_type: str,
    payload: dict[str, Any],
    target_agent_id: str,
    admin_username: str,
    db: Session,
) -> AgentRole:
    """Shared helper: validate, create the install task, and the AgentRole row.

    Used by both the JSON API (`POST /api/admin/roles`) and the form view (`POST /roles/install`).
    """
    if role_type == "auth_server":
        task_type = "install_auth_server"
    elif role_type == "file_server":
        task_type = "install_file_server"
    else:
        raise HTTPException(status_code=400, detail=f"unknown role_type {role_type}")
    _validate_payload(task_type, payload)

    agent = db.query(Agent).filter_by(agent_id=target_agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Target agent not found")

    title = _default_title(task_type, payload)
    task = Task(
        agent_pk=agent.id,
        type=task_type,
        payload=payload,
        created_by=admin_username,
        title=title,
    )
    db.add(task)
    db.flush()

    # Persist a sanitized copy on the AgentRole — never store passwords here.
    safe_config = {k: v for k, v in payload.items() if k not in TASK_PAYLOAD_SECRETS}
    role = AgentRole(
        agent_pk=agent.id,
        role_type=role_type,
        config=safe_config,
        status="installing",
        install_task_id=task.id,
    )
    db.add(role)
    db.commit()
    return role


@router.post("/roles")
def api_install_role(
    body: dict,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    role = install_role(
        role_type=body.get("role_type", ""),
        payload=body.get("payload", {}) or {},
        target_agent_id=body.get("target_agent_id", ""),
        admin_username=admin.username,
        db=db,
    )
    return {"id": role.id, "task_id": role.install_task_id, "status": role.status}


@router.delete("/roles/{role_id}")
def delete_role_record(
    role_id: int,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Removes the role record only — does NOT uninstall services on the host.

    To actually uninstall, send a `shell` task with the appropriate apt purge.
    """
    role = db.get(AgentRole, role_id)
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    db.delete(role)
    db.commit()
    return {"ok": True}


@router.delete("/agents/{agent_id}")
def delete_agent(
    agent_id: str,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    a = db.query(Agent).filter_by(agent_id=agent_id).first()
    if not a:
        raise HTTPException(status_code=404, detail="Agent not found")
    db.delete(a)
    db.commit()
    return {"ok": True}


@router.post("/enrollment-tokens")
def create_enrollment_token(
    req: CreateEnrollmentTokenRequest,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    if req.default_group_id is not None and not db.get(Group, req.default_group_id):
        raise HTTPException(status_code=400, detail="default_group_id not found")
    expires_at = None
    if req.expires_in_hours and req.expires_in_hours > 0:
        expires_at = datetime.now(timezone.utc) + timedelta(hours=req.expires_in_hours)
    token = EnrollmentToken(
        token=secrets.token_urlsafe(24),
        label=req.label,
        default_group_id=req.default_group_id,
        expires_at=expires_at,
    )
    db.add(token)
    db.commit()
    return {"id": token.id, "token": token.token}


@router.delete("/enrollment-tokens/{token_id}")
def revoke_enrollment_token(
    token_id: int,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    t = db.get(EnrollmentToken, token_id)
    if not t:
        raise HTTPException(status_code=404, detail="Not found")
    t.revoked = True
    db.commit()
    return {"ok": True}
