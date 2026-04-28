"""Admin account management UI."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from . import ldap_auth
from .auth import hash_password, require_admin
from .db import get_db
from .models import Admin
from .utils import relative_time
from .views import templates

router = APIRouter()


@router.get("/admins", response_class=HTMLResponse)
def admins_page(
    request: Request,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    rows = db.query(Admin).order_by(Admin.username).all()
    return templates.TemplateResponse(
        request,
        "admins.html",
        {
            "active": "admins",
            "admins": [
                {
                    "row": a,
                    "last_login": relative_time(a.last_login),
                    "created": relative_time(a.created_at),
                    "is_self": a.username == admin.username,
                }
                for a in rows
            ],
            "ldap_configured": ldap_auth.is_configured(),
            "current_user": admin.username,
            "flash": request.session.pop("flash", None),
        },
    )


@router.post("/admins/create")
def admin_create(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    display_name: str = Form(""),
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    username = username.strip()
    if not username or not password:
        request.session["flash"] = "Username and password required."
        return RedirectResponse("/admins", status_code=303)
    if db.query(Admin).filter_by(username=username).first():
        request.session["flash"] = f"User '{username}' already exists."
        return RedirectResponse("/admins", status_code=303)
    db.add(Admin(
        username=username,
        password_hash=hash_password(password),
        auth_source="local",
        display_name=display_name.strip(),
    ))
    db.commit()
    request.session["flash"] = f"Created local admin '{username}'."
    return RedirectResponse("/admins", status_code=303)


@router.post("/admins/{admin_id}/password")
def admin_set_password(
    admin_id: int,
    request: Request,
    password: str = Form(...),
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    target = db.get(Admin, admin_id)
    if not target:
        raise HTTPException(status_code=404, detail="Admin not found")
    if target.auth_source != "local":
        request.session["flash"] = "Cannot set a password on an LDAP-sourced account."
        return RedirectResponse("/admins", status_code=303)
    if not password.strip():
        request.session["flash"] = "Password required."
        return RedirectResponse("/admins", status_code=303)
    target.password_hash = hash_password(password)
    db.commit()
    request.session["flash"] = f"Password updated for '{target.username}'."
    return RedirectResponse("/admins", status_code=303)


@router.post("/admins/{admin_id}/delete")
def admin_delete(
    admin_id: int,
    request: Request,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    target = db.get(Admin, admin_id)
    if not target:
        raise HTTPException(status_code=404, detail="Admin not found")
    if target.username == admin.username:
        request.session["flash"] = "Cannot delete your own account."
        return RedirectResponse("/admins", status_code=303)
    if db.query(Admin).count() <= 1:
        request.session["flash"] = "Cannot delete the last admin."
        return RedirectResponse("/admins", status_code=303)
    name = target.username
    db.delete(target)
    db.commit()
    request.session["flash"] = f"Removed admin '{name}'."
    return RedirectResponse("/admins", status_code=303)
