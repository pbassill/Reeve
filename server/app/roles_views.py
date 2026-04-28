"""HTML views for the /roles section: list, install, detail, and the
'configure clients to use a security server' bulk-queue helper."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .api_admin import _validate_payload, install_role
from .auth import require_admin
from .db import get_db
from .models import Admin, Agent, AgentRole, Group, Task
from .utils import relative_time
from .views import templates

router = APIRouter()


ROLE_LABELS = {
    "auth_server": "Authentication Server (Samba AD-DC)",
    "file_server": "File Server (Samba shares + homes)",
    "print_server": "Print Server (CUPS)",
    "security_server": "Security Server (DHCP+DNS, ad/malware blocklists, web proxy, apt + clamav mirror, central logs)",
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
    elif role_type == "print_server":
        payload = {
            "allow_remote_admin": form.get("allow_remote_admin") == "on",
            "admin_username": (form.get("admin_username") or "").strip(),
        }
    elif role_type == "security_server":
        payload = {
            "interface": (form.get("interface") or "").strip(),
            "subnet": (form.get("subnet") or "").strip(),
            "range_start": (form.get("range_start") or "").strip(),
            "range_end": (form.get("range_end") or "").strip(),
            "gateway": (form.get("gateway") or "").strip(),
            "netmask": (form.get("netmask") or "255.255.255.0").strip(),
            "upstream_dns": (form.get("upstream_dns") or "1.1.1.1").strip(),
            "domain": (form.get("domain") or "lan").strip(),
            "enable_blocklist": form.get("enable_blocklist") == "on",
            "enable_squid": form.get("enable_squid") == "on",
            "enable_apt_mirror": form.get("enable_apt_mirror") == "on",
            "enable_clamav_mirror": form.get("enable_clamav_mirror") == "on",
            "enable_log_server": form.get("enable_log_server") == "on",
            "blocklist_urls": form.get("blocklist_urls") or
                "https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts",
            "squid_block_domains": form.get("squid_block_domains") or "",
            "squid_port": int(form.get("squid_port") or 3128),
            "ubuntu_codename": (form.get("ubuntu_codename") or "noble").strip(),
            "mirror_components": (form.get("mirror_components") or "main restricted universe multiverse").strip(),
            "log_retention_days": int(form.get("log_retention_days") or 30),
        }
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


@router.post("/roles/{role_id}/configure-clients")
async def role_configure_clients(
    role_id: int,
    request: Request,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """For a Security Server role: queue tasks across the chosen target so every
    client uses this server for apt, clamav, dns, syslog, and the web proxy."""
    role = db.get(AgentRole, role_id)
    if not role or role.role_type != "security_server":
        raise HTTPException(status_code=400, detail="Only valid for an installed Security Server role")

    form = await request.form()
    target_kind = form.get("target_kind", "all")
    target_id = form.get("target_id", "")

    targets: dict[int, Agent] = {}
    if target_kind == "group" and target_id.isdigit():
        g = db.get(Group, int(target_id))
        if g:
            for a in g.members:
                targets[a.id] = a
    elif target_kind == "agent" and target_id:
        a = db.query(Agent).filter_by(agent_id=target_id).first()
        if a:
            targets[a.id] = a
    else:  # all
        for a in db.query(Agent).all():
            if a.id == role.agent_pk:
                continue  # don't reconfigure the security server to point at itself
            targets[a.id] = a

    if not targets:
        request.session["flash"] = "No targets resolved (skipped the Security Server itself)."
        return RedirectResponse(f"/roles/{role.id}", status_code=303)

    server_ip = role.agent.ip_address or role.agent.hostname
    cfg = role.config or {}
    codename = cfg.get("ubuntu_codename") or "noble"
    components = cfg.get("mirror_components") or "main restricted universe multiverse"

    # Build the five canonical client-config tasks. Skip ones whose feature
    # was disabled on this Security Server install.
    task_specs: list[tuple[str, dict]] = []
    if cfg.get("enable_apt_mirror", True):
        task_specs.append(("set_apt_mirror", {
            "url": f"http://{server_ip}/ubuntu/",
            "codename": codename,
            "components": components,
        }))
    if cfg.get("enable_clamav_mirror", True):
        task_specs.append(("set_clamav_mirror", {"url": f"http://{server_ip}/clamav/"}))
    task_specs.append(("set_dns_servers", {"servers": [server_ip], "search_domain": cfg.get("domain", "lan")}))
    if cfg.get("enable_log_server", True):
        task_specs.append(("set_log_forwarding", {"server": server_ip, "protocol": "udp", "port": 514}))
    if cfg.get("enable_squid", True):
        task_specs.append(("set_proxy", {
            "proxy_url": f"http://{server_ip}:{cfg.get('squid_port', 3128)}",
            "no_proxy": f"localhost,127.0.0.1,::1,{server_ip}",
        }))

    # Validate every payload before persisting anything.
    for ttype, payload in task_specs:
        _validate_payload(ttype, payload)

    batch_id = uuid.uuid4().hex
    queued = 0
    for a in targets.values():
        for ttype, payload in task_specs:
            db.add(Task(
                agent_pk=a.id,
                type=ttype,
                payload=payload,
                created_by=admin.username,
                batch_id=batch_id,
                title=f"configure {a.hostname} → security server #{role.id} ({ttype})",
            ))
            queued += 1
    db.commit()
    request.session["flash"] = (
        f"Queued {queued} client-config task(s) across {len(targets)} device(s)."
    )
    return RedirectResponse(f"/roles/{role.id}", status_code=303)


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
