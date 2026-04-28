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
    "install_print_server",
    "install_dhcp_dns",
    "install_security_server",
    "check_compliance",
    "inventory_refresh",
    "open_terminal",
    "set_apt_mirror",
    "set_clamav_mirror",
    "set_dns_servers",
    "set_log_forwarding",
    "set_proxy",
}

# Task payload keys that are secrets — scrubbed from the DB row right after
# the agent picks the task up on its first check-in, so the password never
# sits at rest on the reevectl server.
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
    elif task_type == "install_print_server":
        _validate_role_print_server(payload)
    elif task_type == "install_dhcp_dns":
        _validate_role_dhcp_dns(payload)
    elif task_type == "install_security_server":
        _validate_role_security_server(payload)
    elif task_type == "set_apt_mirror":
        _validate_set_apt_mirror(payload)
    elif task_type == "set_clamav_mirror":
        _validate_set_clamav_mirror(payload)
    elif task_type == "set_dns_servers":
        _validate_set_dns_servers(payload)
    elif task_type == "set_log_forwarding":
        _validate_set_log_forwarding(payload)
    elif task_type == "set_proxy":
        _validate_set_proxy(payload)


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


def _validate_role_security_server(p: dict[str, Any]) -> None:
    # All the DHCP/DNS validation, plus feature toggles + sources.
    _validate_role_dhcp_dns(p)
    p["enable_blocklist"] = bool(p.get("enable_blocklist", True))
    p["enable_squid"] = bool(p.get("enable_squid", True))
    p["enable_apt_mirror"] = bool(p.get("enable_apt_mirror", True))
    p["enable_clamav_mirror"] = bool(p.get("enable_clamav_mirror", True))
    p["enable_log_server"] = bool(p.get("enable_log_server", True))

    blocklist_urls = p.get("blocklist_urls") or [
        "https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts"
    ]
    if isinstance(blocklist_urls, str):
        blocklist_urls = [u.strip() for u in blocklist_urls.splitlines() if u.strip()]
    if not isinstance(blocklist_urls, list) or not all(isinstance(u, str) for u in blocklist_urls):
        raise HTTPException(status_code=400, detail="blocklist_urls must be a list of URLs")
    for u in blocklist_urls:
        if not (u.startswith("http://") or u.startswith("https://")):
            raise HTTPException(status_code=400, detail=f"blocklist URL must start with http(s): {u}")
    p["blocklist_urls"] = blocklist_urls

    squid_block = p.get("squid_block_domains") or []
    if isinstance(squid_block, str):
        squid_block = [d.strip() for d in squid_block.replace(",", " ").split() if d.strip()]
    if not isinstance(squid_block, list) or not all(isinstance(d, str) for d in squid_block):
        raise HTTPException(status_code=400, detail="squid_block_domains must be a list of domains")
    p["squid_block_domains"] = squid_block

    p["squid_port"] = int(p.get("squid_port") or 3128)
    if not (1 <= p["squid_port"] <= 65535):
        raise HTTPException(status_code=400, detail="squid_port out of range")

    codename = (p.get("ubuntu_codename") or "").strip().lower()
    if codename and not codename.isalpha():
        raise HTTPException(status_code=400, detail="ubuntu_codename must be alphabetic (e.g. noble)")
    p["ubuntu_codename"] = codename or "noble"
    p["mirror_components"] = (p.get("mirror_components") or "main restricted universe multiverse").strip()
    p["log_retention_days"] = int(p.get("log_retention_days") or 30)


def _validate_set_apt_mirror(p: dict[str, Any]) -> None:
    url = (p.get("url") or "").strip()
    codename = (p.get("codename") or "").strip().lower()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail="set_apt_mirror requires http(s) url")
    if codename and not codename.isalpha():
        raise HTTPException(status_code=400, detail="codename must be alphabetic")
    components = (p.get("components") or "main restricted universe multiverse").strip()
    p.update(url=url, codename=codename, components=components)


def _validate_set_clamav_mirror(p: dict[str, Any]) -> None:
    url = (p.get("url") or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail="set_clamav_mirror requires http(s) url")
    p["url"] = url


def _validate_set_dns_servers(p: dict[str, Any]) -> None:
    servers = p.get("servers") or []
    if not isinstance(servers, list) or not servers:
        raise HTTPException(status_code=400, detail="servers must be a non-empty list of IPs")
    for s in servers:
        if not isinstance(s, str) or not _IP_RE.match(s):
            raise HTTPException(status_code=400, detail=f"invalid IP {s!r}")
    p["search_domain"] = (p.get("search_domain") or "").strip()


def _validate_set_log_forwarding(p: dict[str, Any]) -> None:
    server = (p.get("server") or "").strip()
    if not _IP_RE.match(server) and "." not in server:
        raise HTTPException(status_code=400, detail="server must be an IP or hostname")
    proto = (p.get("protocol") or "udp").lower()
    if proto not in ("udp", "tcp"):
        raise HTTPException(status_code=400, detail="protocol must be udp or tcp")
    port = int(p.get("port") or 514)
    p.update(server=server, protocol=proto, port=port)


def _validate_set_proxy(p: dict[str, Any]) -> None:
    url = (p.get("proxy_url") or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail="proxy_url must start with http(s)")
    no_proxy = p.get("no_proxy") or "localhost,127.0.0.1,::1"
    if not isinstance(no_proxy, str):
        raise HTTPException(status_code=400, detail="no_proxy must be a string")
    p.update(proxy_url=url, no_proxy=no_proxy)


def _validate_role_print_server(p: dict[str, Any]) -> None:
    p["allow_remote_admin"] = bool(p.get("allow_remote_admin", False))
    p["admin_username"] = (p.get("admin_username") or "").strip()
    if p["allow_remote_admin"] and not p["admin_username"]:
        raise HTTPException(status_code=400, detail="admin_username required when allow_remote_admin=true")


def _validate_role_dhcp_dns(p: dict[str, Any]) -> None:
    iface = (p.get("interface") or "").strip()
    if not iface or not iface.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(status_code=400, detail="interface required (e.g. eth0)")
    subnet = (p.get("subnet") or "").strip()
    range_start = (p.get("range_start") or "").strip()
    range_end = (p.get("range_end") or "").strip()
    gateway = (p.get("gateway") or "").strip()
    netmask = (p.get("netmask") or "255.255.255.0").strip()
    upstream_dns = (p.get("upstream_dns") or "1.1.1.1").strip()
    domain = (p.get("domain") or "lan").strip()
    for label, value in [("subnet", subnet), ("range_start", range_start),
                          ("range_end", range_end), ("gateway", gateway),
                          ("netmask", netmask), ("upstream_dns", upstream_dns)]:
        if not _IP_RE.match(value):
            raise HTTPException(status_code=400, detail=f"{label} must be a valid IPv4 address")
    p.update(interface=iface, subnet=subnet, range_start=range_start, range_end=range_end,
             gateway=gateway, netmask=netmask, upstream_dns=upstream_dns, domain=domain)


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
    if task_type == "install_print_server":
        return "install print server (CUPS)"
    if task_type == "install_dhcp_dns":
        return f"install DHCP+DNS ({payload.get('interface', 'iface')}, subnet={payload.get('subnet', '')})"
    if task_type == "install_security_server":
        return f"install Security Server ({payload.get('interface', 'iface')})"
    if task_type == "set_apt_mirror":
        return f"point apt at {payload.get('url', '')}"
    if task_type == "set_clamav_mirror":
        return f"point clamav at {payload.get('url', '')}"
    if task_type == "set_dns_servers":
        return f"set DNS servers: {' '.join(payload.get('servers', []))}"
    if task_type == "set_log_forwarding":
        return f"forward logs to {payload.get('server', '')}:{payload.get('port', 514)}"
    if task_type == "set_proxy":
        return f"set http(s) proxy: {payload.get('proxy_url', '')}"
    if task_type == "check_compliance":
        return f"compliance check (policy {payload.get('policy_id', '?')})"
    if task_type == "inventory_refresh":
        return "inventory refresh"
    if task_type == "open_terminal":
        return f"terminal session {payload.get('session_id', '')[:8]}"
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
    role_to_task = {
        "auth_server": "install_auth_server",
        "file_server": "install_file_server",
        "print_server": "install_print_server",
        "security_server": "install_security_server",
    }
    task_type = role_to_task.get(role_type)
    if not task_type:
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
