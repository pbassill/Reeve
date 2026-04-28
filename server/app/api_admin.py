import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .auth import require_admin
from .db import get_db
from .models import Admin, Agent, EnrollmentToken, Group, Task
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
}


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
