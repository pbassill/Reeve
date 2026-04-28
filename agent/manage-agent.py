#!/usr/bin/env python3
"""Manage agent — runs as a systemd service on each Ubuntu host.

Polls the configured server, executes queued tasks, reports results.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import platform
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

AGENT_VERSION = "0.4.0"
CONFIG_PATH = Path(os.environ.get("MANAGE_CONFIG", "/etc/manage-agent/config.json"))
DEFAULT_INTERVAL = 30

log = logging.getLogger("manage-agent")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


# --- HTTP helpers (stdlib only — no extra deps required) ---


def _http(method: str, url: str, headers: dict[str, str] | None = None, body: dict | None = None, timeout: int = 30) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", f"manage-agent/{AGENT_VERSION}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode() or "{}"
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace") if e.fp else ""
        raise RuntimeError(f"HTTP {e.code} {url}: {body_text}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error contacting {url}: {e}") from e


# --- system info ---


def _read_proc(path: str) -> str:
    try:
        return Path(path).read_text()
    except OSError:
        return ""


def _cpu_model() -> str:
    for line in _read_proc("/proc/cpuinfo").splitlines():
        if line.startswith("model name"):
            return line.split(":", 1)[1].strip()
    return platform.processor() or "unknown"


def _cpu_cores() -> int:
    return os.cpu_count() or 0


def _meminfo() -> tuple[int, int]:
    fields = {}
    for line in _read_proc("/proc/meminfo").splitlines():
        k, _, v = line.partition(":")
        fields[k.strip()] = v.strip()
    def kb(name: str) -> int:
        v = fields.get(name, "0 kB").split()
        try:
            return int(v[0])
        except (ValueError, IndexError):
            return 0
    total_kb = kb("MemTotal")
    avail_kb = kb("MemAvailable")
    used_kb = max(0, total_kb - avail_kb)
    return used_kb // 1024, total_kb // 1024


def _diskinfo() -> tuple[int, int]:
    try:
        s = os.statvfs("/")
    except OSError:
        return 0, 0
    total = s.f_blocks * s.f_frsize
    free = s.f_bavail * s.f_frsize
    used = total - free
    return used // (1024**3), total // (1024**3)


def _uptime() -> int:
    try:
        return int(float(_read_proc("/proc/uptime").split()[0]))
    except (IndexError, ValueError):
        return 0


def _cpu_percent(sample_seconds: float = 0.5) -> float:
    def snap() -> tuple[int, int]:
        line = _read_proc("/proc/stat").splitlines()[0]
        parts = [int(x) for x in line.split()[1:]]
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
        total = sum(parts)
        return idle, total
    try:
        i1, t1 = snap()
        time.sleep(sample_seconds)
        i2, t2 = snap()
        dt = t2 - t1
        di = i2 - i1
        if dt <= 0:
            return 0.0
        return max(0.0, min(100.0, (1.0 - di / dt) * 100.0))
    except Exception:
        return 0.0


def _os_release() -> tuple[str, str]:
    info: dict[str, str] = {}
    for line in _read_proc("/etc/os-release").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info[k] = v.strip().strip('"')
    return info.get("NAME", "Linux"), info.get("VERSION", "")


def _primary_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("1.1.1.1", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return ""


def _logged_in_user() -> str:
    try:
        out = subprocess.run(
            ["loginctl", "list-sessions", "--no-legend"],
            capture_output=True, text=True, timeout=5,
        )
        for line in out.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[3] not in ("root", "gdm", "lightdm"):
                return parts[2]
    except (FileNotFoundError, subprocess.SubprocessError):
        pass
    try:
        out = subprocess.run(["who"], capture_output=True, text=True, timeout=5)
        line = out.stdout.splitlines()[0] if out.stdout else ""
        return line.split()[0] if line else ""
    except (FileNotFoundError, subprocess.SubprocessError, IndexError):
        return ""


def collect_system_info() -> dict[str, Any]:
    os_name, os_version = _os_release()
    mem_used, mem_total = _meminfo()
    disk_used, disk_total = _diskinfo()
    hw = collect_hardware_info()
    return {
        "hostname": socket.gethostname(),
        "os_name": os_name,
        "os_version": os_version,
        "kernel": platform.release(),
        "arch": platform.machine(),
        "cpu_model": _cpu_model(),
        "cpu_cores": _cpu_cores(),
        "cpu_percent": _cpu_percent(),
        "mem_total_mb": mem_total,
        "mem_used_mb": mem_used,
        "disk_total_gb": disk_total,
        "disk_used_gb": disk_used,
        "uptime_seconds": _uptime(),
        "ip_address": _primary_ip(),
        "agent_version": AGENT_VERSION,
        "logged_in_user": _logged_in_user(),
        "manufacturer": hw.get("manufacturer", ""),
        "product_name": hw.get("product_name", ""),
        "serial_number": hw.get("serial_number", ""),
        "bios_version": hw.get("bios_version", ""),
        "gpu_model": hw.get("gpu_model", ""),
        "mac_address": hw.get("mac_address", ""),
    }


# --- config ---


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        log.error("No config at %s — running enrollment first.", CONFIG_PATH)
        sys.exit(1)
    return json.loads(CONFIG_PATH.read_text())


def save_config(cfg: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


def enroll(server: str, enrollment_token: str) -> dict[str, Any]:
    payload = {"enrollment_token": enrollment_token, "system_info": collect_system_info()}
    resp = _http("POST", f"{server}/api/agents/enroll", body=payload)
    cfg = {
        "server": server.rstrip("/"),
        "agent_id": resp["agent_id"],
        "token": resp["token"],
        "checkin_interval_seconds": resp.get("checkin_interval_seconds", DEFAULT_INTERVAL),
    }
    save_config(cfg)
    log.info("Enrolled as agent %s", cfg["agent_id"])
    return cfg


# --- task execution ---


def _run(cmd: list[str] | str, *, shell: bool = False, timeout: int = 600, env: dict | None = None) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            shell=shell,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as e:
        return 124, e.stdout or "", (e.stderr or "") + f"\n[timeout after {timeout}s]"
    except Exception as e:
        return 1, "", f"agent error: {e}"


def execute_task(task: dict[str, Any], agent_state: dict[str, Any]) -> tuple[int, str, str]:
    t = task["type"]
    p = task.get("payload") or {}
    log.info("Executing task #%s (%s)", task["id"], t)

    if t == "shell":
        return _run(p["command"], shell=True, timeout=int(p.get("timeout", 600)))
    if t == "apt_install":
        pkgs = list(p.get("packages") or [])
        if not pkgs:
            return 1, "", "no packages"
        env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
        rc1, o1, e1 = _run(["apt-get", "update"], env=env, timeout=300)
        rc2, o2, e2 = _run(["apt-get", "install", "-y", *pkgs], env=env, timeout=900)
        return (rc1 or rc2), o1 + o2, e1 + e2
    if t == "apt_remove":
        pkgs = list(p.get("packages") or [])
        if not pkgs:
            return 1, "", "no packages"
        env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
        return _run(["apt-get", "remove", "-y", *pkgs], env=env, timeout=600)
    if t == "apt_upgrade":
        env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
        rc1, o1, e1 = _run(["apt-get", "update"], env=env, timeout=300)
        rc2, o2, e2 = _run(["apt-get", "upgrade", "-y"], env=env, timeout=1800)
        return (rc1 or rc2), o1 + o2, e1 + e2
    if t == "snap_install":
        pkgs = list(p.get("packages") or [])
        if not pkgs:
            return 1, "", "no packages"
        return _run(["snap", "install", *pkgs], timeout=900)
    if t == "snap_remove":
        pkgs = list(p.get("packages") or [])
        return _run(["snap", "remove", *pkgs], timeout=300)
    if t == "snap_refresh":
        return _run(["snap", "refresh"], timeout=1800)
    if t == "flatpak_install":
        ensure_rc, ensure_out, ensure_err = _ensure_flatpak()
        if ensure_rc != 0:
            return ensure_rc, ensure_out, ensure_err
        remote = (p.get("remote") or "flathub").strip()
        ids = list(p.get("app_ids") or [])
        rc, o, e = _run(
            ["flatpak", "install", "-y", "--noninteractive", remote, *ids],
            timeout=1800,
        )
        return rc, ensure_out + o, ensure_err + e
    if t == "flatpak_remove":
        return _run(
            ["flatpak", "uninstall", "-y", "--noninteractive", *list(p.get("app_ids") or [])],
            timeout=600,
        )
    if t == "flatpak_update":
        ensure_rc, ensure_out, ensure_err = _ensure_flatpak()
        if ensure_rc != 0:
            return ensure_rc, ensure_out, ensure_err
        rc, o, e = _run(["flatpak", "update", "-y", "--noninteractive"], timeout=1800)
        return rc, ensure_out + o, ensure_err + e
    if t == "push_file":
        path = Path(p["path"])
        content = base64.b64decode(p["content_b64"])
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            mode_str = p.get("mode", "0644")
            os.chmod(path, int(mode_str, 8))
            return 0, f"wrote {len(content)} bytes to {path} (mode {mode_str})", ""
        except OSError as e:
            return 1, "", f"push_file error: {e}"
    if t == "reboot":
        subprocess.Popen(["shutdown", "-r", "+1", "Reboot scheduled by Manage"])
        return 0, "reboot scheduled in 60s", ""
    if t == "shutdown":
        subprocess.Popen(["shutdown", "-h", "+1", "Shutdown scheduled by Manage"])
        return 0, "shutdown scheduled in 60s", ""
    if t == "set_wallpaper":
        return _set_wallpaper_system(p.get("url", ""))
    if t == "user_shell":
        return _run_as_desktop_user(p["command"], shell=True)
    if t == "user_gsettings":
        return _run_as_desktop_user(
            ["gsettings", "set", p["schema"], p["key"], p["value"]],
        )
    if t == "set_wallpaper_user":
        return _set_wallpaper_user(p.get("url", ""))
    if t == "set_dock_favorites":
        favs = p.get("favorites") or []
        gv = "[" + ", ".join(f"'{f}'" for f in favs) + "]"
        return _run_as_desktop_user(
            ["gsettings", "set", "org.gnome.shell", "favorite-apps", gv],
        )
    if t == "self_update":
        agent_state["force_self_update"] = True
        return 0, "self-update will run after this checkin completes", ""
    if t == "install_auth_server":
        return _install_auth_server(p)
    if t == "install_file_server":
        return _install_file_server(p)
    if t == "install_print_server":
        return _install_print_server(p)
    if t == "install_dhcp_dns":
        return _install_dhcp_dns(p)
    if t == "check_compliance":
        return _check_compliance(p)
    if t == "inventory_refresh":
        return _inventory_refresh(agent_state)
    if t == "open_terminal":
        return _open_terminal(p, agent_state)
    return 1, "", f"unknown task type {t}"


# --- Server roles: one-click installs --------------------------------------

_AUTH_SERVER_SCRIPT = r"""
set -euo pipefail
: "${REALM:?REALM required}"
: "${DOMAIN:?DOMAIN required}"
: "${ADMIN_PASSWORD:?ADMIN_PASSWORD required}"
: "${DNS_FORWARDER:=1.1.1.1}"

LOWER_REALM="${REALM,,}"

# Idempotency: if Samba is already provisioned, just make sure it's running.
if [ -f /var/lib/samba/private/sam.ldb ]; then
  echo "[role:auth_server] Already provisioned (sam.ldb present)."
  systemctl unmask samba-ad-dc 2>/dev/null || true
  systemctl enable --now samba-ad-dc
  systemctl is-active --quiet samba-ad-dc && echo "[role:auth_server] samba-ad-dc is active"
  samba-tool domain info 127.0.0.1 || true
  exit 0
fi

export DEBIAN_FRONTEND=noninteractive

# Avoid interactive krb5 prompt during apt install.
{
  echo "krb5-config krb5-config/default_realm string ${REALM}"
  echo "krb5-config krb5-config/admin_server string ${REALM}"
  echo "krb5-config krb5-config/kerberos_servers string ${REALM}"
} | debconf-set-selections

apt-get update -qq
apt-get install -y --no-install-recommends \
    samba samba-dsdb-modules samba-vfs-modules \
    winbind libnss-winbind libpam-winbind \
    krb5-user krb5-config \
    dnsutils chrony

# AD-DC must run alone — disable conflicting services. systemd-resolved
# stays disabled because the AD-DC is the DNS server on 53.
for svc in smbd nmbd winbind systemd-resolved; do
  systemctl disable --now "$svc" 2>/dev/null || true
done

# Stash existing smb.conf so the provisioner writes a fresh one.
[ -f /etc/samba/smb.conf ] && \
  mv /etc/samba/smb.conf "/etc/samba/smb.conf.bak.$(date +%s)" || true

samba-tool domain provision \
  --use-rfc2307 \
  --realm="${REALM}" \
  --domain="${DOMAIN}" \
  --server-role=dc \
  --dns-backend=SAMBA_INTERNAL \
  --adminpass="${ADMIN_PASSWORD}"

# Adopt the provisioner's krb5.conf system-wide.
install -m 0644 /var/lib/samba/private/krb5.conf /etc/krb5.conf

# Configure the upstream DNS forwarder.
if grep -qE '^[[:space:]]*dns forwarder' /etc/samba/smb.conf; then
  sed -i "s|^[[:space:]]*dns forwarder.*|        dns forwarder = ${DNS_FORWARDER}|" /etc/samba/smb.conf
else
  sed -i "/^\[global\]/a\\        dns forwarder = ${DNS_FORWARDER}" /etc/samba/smb.conf
fi

# Point this host's resolver at itself so domain joins succeed.
rm -f /etc/resolv.conf
cat > /etc/resolv.conf <<EOF
nameserver 127.0.0.1
search ${LOWER_REALM}
EOF
chattr +i /etc/resolv.conf 2>/dev/null || true

systemctl unmask samba-ad-dc 2>/dev/null || true
systemctl enable --now samba-ad-dc

# Wait briefly for the AD services to come up.
for _ in $(seq 1 20); do
  if samba-tool domain info 127.0.0.1 >/dev/null 2>&1; then break; fi
  sleep 1
done
samba-tool domain info 127.0.0.1
echo "[role:auth_server] Provisioned realm ${REALM} (NetBIOS ${DOMAIN})"
"""


_FILE_SERVER_SCRIPT = r"""
set -euo pipefail
: "${MODE:=standalone}"
: "${SHARES_ROOT:=/srv/shares}"
: "${HOMES_ROOT:=/srv/homes}"
DEPARTMENTS="${DEPARTMENTS:-}"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq

if [ "${MODE}" = "domain" ]; then
  : "${REALM:?REALM required for domain mode}"
  : "${DOMAIN:?DOMAIN required for domain mode}"
  : "${JOIN_PASSWORD:?JOIN_PASSWORD required for domain mode}"
  apt-get install -y --no-install-recommends \
      samba winbind libnss-winbind libpam-winbind \
      krb5-user krb5-config acl
else
  apt-get install -y --no-install-recommends samba acl
fi

mkdir -p "${SHARES_ROOT}" "${HOMES_ROOT}"
chmod 0755 "${SHARES_ROOT}" "${HOMES_ROOT}"

# Per-department directory + group.
for dept in ${DEPARTMENTS}; do
  mkdir -p "${SHARES_ROOT}/${dept}"
  groupadd -f "dept-${dept}"
  chgrp "dept-${dept}" "${SHARES_ROOT}/${dept}"
  chmod 2770 "${SHARES_ROOT}/${dept}"  # setgid: inherit group on new files
done

SHARES_BLOCK=""
for dept in ${DEPARTMENTS}; do
  SHARES_BLOCK+="
[${dept}]
   comment = ${dept} share
   path = ${SHARES_ROOT}/${dept}
   browseable = yes
   read only = no
   create mask = 0660
   directory mask = 0770
   force group = dept-${dept}
   valid users = @\"dept-${dept}\"
"
done

if [ "${MODE}" = "domain" ]; then
  cat > /etc/krb5.conf <<EOF
[libdefaults]
  default_realm = ${REALM}
  dns_lookup_realm = false
  dns_lookup_kdc = true
EOF

  cat > /etc/samba/smb.conf <<EOF
[global]
   workgroup = ${DOMAIN}
   realm = ${REALM}
   security = ADS
   server string = Manage File Server
   log file = /var/log/samba/log.%m
   max log size = 1000

   template homedir = ${HOMES_ROOT}/%U
   template shell = /bin/bash

   winbind use default domain = yes
   winbind enum users = yes
   winbind enum groups = yes
   winbind refresh tickets = yes

   idmap config * : backend = tdb
   idmap config * : range = 3000-7999
   idmap config ${DOMAIN} : backend = rid
   idmap config ${DOMAIN} : range = 10000-99999

   vfs objects = acl_xattr
   map acl inherit = yes
   store dos attributes = yes

[homes]
   comment = User Home
   browseable = no
   read only = no
   create mask = 0700
   directory mask = 0700
   valid users = %D\\%S

${SHARES_BLOCK}
EOF

  # nsswitch: resolve AD users via winbind.
  sed -i 's/^passwd:.*/passwd:         files systemd winbind/' /etc/nsswitch.conf
  sed -i 's/^group:.*/group:          files systemd winbind/' /etc/nsswitch.conf

  if [ -n "${DC_IP:-}" ]; then
    rm -f /etc/resolv.conf
    cat > /etc/resolv.conf <<EOF
nameserver ${DC_IP}
search ${REALM,,}
EOF
  fi

  if ! net ads testjoin 2>/dev/null | grep -q OK; then
    echo "${JOIN_PASSWORD}" | net ads join -U "administrator%${JOIN_PASSWORD}" || true
  fi

  systemctl enable --now smbd nmbd winbind
  systemctl restart smbd nmbd winbind
  echo "[role:file_server] Domain-joined Samba file server up (realm=${REALM})"
else
  cat > /etc/samba/smb.conf <<EOF
[global]
   workgroup = WORKGROUP
   server string = Manage File Server
   server role = standalone server
   security = user
   passdb backend = tdbsam
   map to guest = Bad User
   log file = /var/log/samba/log.%m
   max log size = 1000

[homes]
   comment = Home Directories
   browseable = no
   read only = no
   create mask = 0700
   directory mask = 0700
   valid users = %S

${SHARES_BLOCK}
EOF

  systemctl enable --now smbd nmbd
  systemctl restart smbd nmbd
  echo "[role:file_server] Standalone Samba file server up"
fi

testparm -s /etc/samba/smb.conf >/dev/null
echo "[role:file_server] Departments: ${DEPARTMENTS:-(none)}"
"""


def _install_auth_server(p: dict[str, Any]) -> tuple[int, str, str]:
    env = {
        **os.environ,
        "REALM": (p.get("realm") or "").upper(),
        "DOMAIN": (p.get("domain") or "").upper(),
        "ADMIN_PASSWORD": p.get("admin_password") or "",
        "DNS_FORWARDER": p.get("dns_forwarder") or "1.1.1.1",
    }
    if not env["REALM"] or not env["DOMAIN"] or not env["ADMIN_PASSWORD"]:
        return 1, "", "missing required parameters"
    return _run(_AUTH_SERVER_SCRIPT, shell=True, env=env, timeout=1800)


def _install_file_server(p: dict[str, Any]) -> tuple[int, str, str]:
    env = {
        **os.environ,
        "MODE": p.get("mode") or "standalone",
        "DEPARTMENTS": " ".join(p.get("departments") or []),
        "SHARES_ROOT": p.get("shares_root") or "/srv/shares",
        "HOMES_ROOT": p.get("homes_root") or "/srv/homes",
    }
    if env["MODE"] == "domain":
        if not p.get("realm") or not p.get("domain") or not p.get("join_password"):
            return 1, "", "domain mode requires realm, domain, and join_password"
        env["REALM"] = p["realm"].upper()
        env["DOMAIN"] = p["domain"].upper()
        env["JOIN_PASSWORD"] = p["join_password"]
        env["DC_IP"] = p.get("dc_ip") or ""
    return _run(_FILE_SERVER_SCRIPT, shell=True, env=env, timeout=1800)


_PRINT_SERVER_SCRIPT = r"""
set -euo pipefail
: "${ALLOW_REMOTE:=0}"
: "${ADMIN_USERNAME:=}"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends cups cups-client cups-bsd printer-driver-all

systemctl enable --now cups

if [ -n "${ADMIN_USERNAME}" ]; then
  if id -u "${ADMIN_USERNAME}" >/dev/null 2>&1; then
    usermod -aG lpadmin "${ADMIN_USERNAME}" || true
  fi
fi

if [ "${ALLOW_REMOTE}" = "1" ]; then
  cupsctl --remote-admin --remote-any --share-printers
else
  cupsctl --no-remote-admin
fi

systemctl restart cups
echo "[role:print_server] CUPS up. Admin UI at http://$(hostname -I | awk '{print $1}'):631 (root or lpadmin members)."
"""


def _install_print_server(p: dict[str, Any]) -> tuple[int, str, str]:
    env = {
        **os.environ,
        "ALLOW_REMOTE": "1" if p.get("allow_remote_admin") else "0",
        "ADMIN_USERNAME": p.get("admin_username") or "",
    }
    return _run(_PRINT_SERVER_SCRIPT, shell=True, env=env, timeout=1800)


_DHCP_DNS_SCRIPT = r"""
set -euo pipefail
: "${INTERFACE:?INTERFACE required}"
: "${SUBNET:?SUBNET required}"
: "${RANGE_START:?RANGE_START required}"
: "${RANGE_END:?RANGE_END required}"
: "${GATEWAY:?GATEWAY required}"
: "${NETMASK:=255.255.255.0}"
: "${UPSTREAM_DNS:=1.1.1.1}"
: "${DOMAIN:=lan}"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends dnsmasq

# dnsmasq listens on 53; systemd-resolved would conflict.
if systemctl is-enabled systemd-resolved >/dev/null 2>&1; then
  systemctl disable --now systemd-resolved
fi
# Replace the symlink to /run/systemd/resolve/stub-resolv.conf with a real file.
if [ -L /etc/resolv.conf ]; then
  rm /etc/resolv.conf
fi
cat > /etc/resolv.conf <<EOF
nameserver 127.0.0.1
nameserver ${UPSTREAM_DNS}
search ${DOMAIN}
EOF

cat > /etc/dnsmasq.d/manage.conf <<EOF
# Managed by Manage. Do not edit by hand.
interface=${INTERFACE}
bind-interfaces
domain-needed
bogus-priv
no-resolv
server=${UPSTREAM_DNS}
domain=${DOMAIN}
local=/${DOMAIN}/
expand-hosts

dhcp-range=${RANGE_START},${RANGE_END},${NETMASK},12h
dhcp-option=3,${GATEWAY}
dhcp-option=6,${GATEWAY}
EOF

dnsmasq --test
systemctl enable --now dnsmasq
systemctl restart dnsmasq
echo "[role:dhcp_dns] dnsmasq up on ${INTERFACE} (${RANGE_START}-${RANGE_END}, gw=${GATEWAY})"
"""


def _install_dhcp_dns(p: dict[str, Any]) -> tuple[int, str, str]:
    env = {
        **os.environ,
        "INTERFACE": p.get("interface", ""),
        "SUBNET": p.get("subnet", ""),
        "RANGE_START": p.get("range_start", ""),
        "RANGE_END": p.get("range_end", ""),
        "GATEWAY": p.get("gateway", ""),
        "NETMASK": p.get("netmask") or "255.255.255.0",
        "UPSTREAM_DNS": p.get("upstream_dns") or "1.1.1.1",
        "DOMAIN": p.get("domain") or "lan",
    }
    return _run(_DHCP_DNS_SCRIPT, shell=True, env=env, timeout=1800)


# --- Compliance check (rule evaluator) -------------------------------------


def _check_compliance(p: dict[str, Any]) -> tuple[int, str, str]:
    """Evaluates the rules in p['rules'] and reports drift as JSON on stdout.

    Exit code 0 = compliant, 1 = drift found (for the server's status mapping).
    """
    rules = p.get("rules") or []
    findings: list[dict] = []
    for rule in rules:
        kind = rule.get("kind")
        params = rule.get("params") or {}
        try:
            f = _eval_rule(kind, params)
        except Exception as e:
            f = {
                "rule_id": rule.get("id"),
                "kind": kind,
                "params": params,
                "message": f"check error: {e}",
            }
        if f:
            f.setdefault("rule_id", rule.get("id"))
            f.setdefault("kind", kind)
            f.setdefault("params", params)
            findings.append(f)
    body = json.dumps({"drift": findings}, separators=(",", ":"))
    return (1 if findings else 0), body, ""


def _eval_rule(kind: str, params: dict) -> dict | None:
    if kind == "package_installed":
        wanted = params.get("packages") or []
        installed = _apt_installed_set()
        missing = [p for p in wanted if p not in installed]
        if missing:
            return {"missing": missing, "message": f"missing packages: {' '.join(missing)}"}
        return None
    if kind == "package_absent":
        forbidden = params.get("packages") or []
        installed = _apt_installed_set()
        present = [p for p in forbidden if p in installed]
        if present:
            return {"present": present, "message": f"forbidden packages installed: {' '.join(present)}"}
        return None
    if kind == "service_running":
        bad = []
        for svc in params.get("services") or []:
            r = subprocess.run(["systemctl", "is-active", svc], capture_output=True, text=True)
            if r.stdout.strip() != "active":
                bad.append(svc)
        if bad:
            return {"not_running": bad, "message": f"services not running: {' '.join(bad)}"}
        return None
    if kind == "file_contains":
        path = params.get("path", "")
        regex = params.get("regex", "")
        if not path or not regex:
            return {"message": "file_contains requires path + regex"}
        try:
            content = Path(path).read_text(errors="replace")
        except FileNotFoundError:
            return {"message": f"{path} not found"}
        import re as _re
        if not _re.search(regex, content, _re.MULTILINE):
            return {"message": f"{path} does not match /{regex}/"}
        return None
    return {"message": f"unknown rule kind {kind}"}


def _apt_installed_set() -> set[str]:
    out = subprocess.run(
        ["dpkg-query", "-W", "-f=${Package}\t${Status}\n"],
        capture_output=True, text=True,
    )
    installed: set[str] = set()
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and "ok installed" in parts[1]:
            installed.add(parts[0])
    return installed


# --- Inventory upload -------------------------------------------------------


def _inventory_refresh(state: dict[str, Any]) -> tuple[int, str, str]:
    cfg = state.get("config") or {}
    server = cfg.get("server", "")
    token = cfg.get("token", "")
    pkgs = _collect_packages()
    body = json.dumps({"hash": _packages_hash(pkgs), "packages": pkgs}, separators=(",", ":"))
    raw = body.encode()
    req = urllib.request.Request(
        f"{server}/api/agents/inventory/packages",
        data=raw,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": f"manage-agent/{AGENT_VERSION}",
        },
    )
    try:
        urllib.request.urlopen(req, timeout=120).read()
    except Exception as e:
        return 1, "", f"upload failed: {e}"
    return 0, f"uploaded {len(pkgs)} packages", ""


def _collect_packages() -> list[dict]:
    out: list[dict] = []
    dpkg = subprocess.run(
        ["dpkg-query", "-W", "-f=${Package}\t${Version}\t${Status}\n"],
        capture_output=True, text=True,
    )
    for line in dpkg.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and "ok installed" in parts[2]:
            out.append({"source": "apt", "name": parts[0], "version": parts[1]})
    snap = subprocess.run(["snap", "list", "--unicode=never"], capture_output=True, text=True)
    if snap.returncode == 0:
        lines = snap.stdout.splitlines()[1:]  # skip header
        for line in lines:
            parts = line.split()
            if len(parts) >= 2:
                out.append({"source": "snap", "name": parts[0], "version": parts[1]})
    flat = subprocess.run(
        ["flatpak", "list", "--app", "--columns=application,version"],
        capture_output=True, text=True,
    )
    if flat.returncode == 0:
        for line in flat.stdout.splitlines():
            parts = line.split("\t") if "\t" in line else line.split(None, 1)
            if not parts or parts[0] in ("Application", ""):
                continue
            out.append({"source": "flatpak", "name": parts[0], "version": parts[1] if len(parts) > 1 else ""})
    return out


def _packages_hash(pkgs: list[dict]) -> str:
    import hashlib
    h = hashlib.sha256()
    for p in sorted(pkgs, key=lambda x: (x.get("source", ""), x.get("name", ""), x.get("version", ""))):
        h.update(f"{p.get('source')}\t{p.get('name')}\t{p.get('version')}\n".encode())
    return h.hexdigest()


# --- Hardware inventory enrichment (called as part of system_info) ----------


def collect_hardware_info() -> dict[str, str]:
    def read(path: str) -> str:
        try:
            return Path(path).read_text().strip()
        except OSError:
            return ""
    info = {
        "manufacturer": read("/sys/class/dmi/id/sys_vendor"),
        "product_name": read("/sys/class/dmi/id/product_name"),
        "serial_number": read("/sys/class/dmi/id/product_serial"),
        "bios_version": read("/sys/class/dmi/id/bios_version"),
    }
    # GPU: lspci first card with 'VGA' or '3D'
    try:
        r = subprocess.run(["lspci"], capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            if "VGA compatible controller" in line or "3D controller" in line:
                info["gpu_model"] = line.split(":", 2)[-1].strip()[:255]
                break
    except (FileNotFoundError, subprocess.SubprocessError):
        pass
    info.setdefault("gpu_model", "")
    # Primary NIC MAC (the one that owns our default route)
    try:
        r = subprocess.run(["ip", "-o", "route", "show", "default"], capture_output=True, text=True, timeout=5)
        iface = ""
        for tok in r.stdout.split():
            if tok == "dev":
                iface = r.stdout.split()[r.stdout.split().index("dev") + 1]
                break
        if iface:
            info["mac_address"] = read(f"/sys/class/net/{iface}/address")
    except (FileNotFoundError, subprocess.SubprocessError, ValueError, IndexError):
        pass
    info.setdefault("mac_address", "")
    return info


# --- Web terminal (HTTP-streaming bridge) -----------------------------------


def _open_terminal(p: dict[str, Any], state: dict[str, Any]) -> tuple[int, str, str]:
    """Opens a pty and bridges its stdin/stdout to the server over HTTPS.

    Runs synchronously in this thread until the session ends, but the agent's
    main check-in loop continues because each task already runs after checkin
    and before the next sleep — terminal sessions are bounded by their server
    side (which closes the input stream when the admin disconnects).
    """
    session_id = (p.get("session_id") or "").strip()
    if not session_id:
        return 1, "", "missing session_id"
    cfg = state.get("config") or {}
    server = cfg.get("server", "")
    token = cfg.get("token", "")
    if not server or not token:
        return 1, "", "agent has no config"
    try:
        return _terminal_bridge(server, token, session_id)
    except Exception as e:
        return 1, "", f"terminal bridge error: {e}"


def _terminal_bridge(server: str, token: str, session_id: str) -> tuple[int, str, str]:
    import fcntl
    import pty
    import select
    import signal
    import threading

    pid, master_fd = pty.fork()
    if pid == 0:
        os.execvp("bash", ["bash", "-i"])

    flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
    fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": f"manage-agent/{AGENT_VERSION}",
    }
    stop = threading.Event()

    def stdin_pump() -> None:
        # Long-poll the server for keystrokes; write each chunk into the pty.
        while not stop.is_set():
            try:
                req = urllib.request.Request(
                    f"{server}/api/agents/terminal/{session_id}/stdin",
                    headers=headers,
                    method="GET",
                )
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = resp.read()
                    if not data:
                        continue
                    if data == b"__CLOSE__":
                        stop.set()
                        try:
                            os.kill(pid, signal.SIGHUP)
                        except ProcessLookupError:
                            pass
                        return
                    try:
                        os.write(master_fd, data)
                    except OSError:
                        stop.set()
                        return
            except urllib.error.HTTPError as e:
                if e.code == 404:  # session closed server-side
                    stop.set()
                    return
                time.sleep(0.5)
            except Exception:
                time.sleep(0.5)

    threading.Thread(target=stdin_pump, daemon=True).start()

    # Stdout pump: read chunks from the pty, POST each one to the server.
    while not stop.is_set():
        try:
            r, _, _ = select.select([master_fd], [], [], 0.5)
            if master_fd in r:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                try:
                    req = urllib.request.Request(
                        f"{server}/api/agents/terminal/{session_id}/stdout",
                        data=chunk,
                        headers={**headers, "Content-Type": "application/octet-stream"},
                        method="POST",
                    )
                    urllib.request.urlopen(req, timeout=10).read()
                except Exception:
                    pass
            # Reap the child if it exited.
            wpid, _ = os.waitpid(pid, os.WNOHANG)
            if wpid == pid:
                break
        except Exception:
            break
    stop.set()
    try:
        os.kill(pid, signal.SIGHUP)
    except ProcessLookupError:
        pass
    try:
        os.close(master_fd)
    except OSError:
        pass
    # Tell the server we're done so it can close the admin WebSocket.
    try:
        req = urllib.request.Request(
            f"{server}/api/agents/terminal/{session_id}/close",
            headers=headers,
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:
        pass
    return 0, f"terminal session {session_id} closed", ""


def _ensure_flatpak() -> tuple[int, str, str]:
    if subprocess.run(["which", "flatpak"], capture_output=True).returncode == 0:
        return 0, "", ""
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
    rc1, o1, e1 = _run(["apt-get", "install", "-y", "flatpak"], env=env, timeout=300)
    if rc1 != 0:
        return rc1, o1, e1
    rc2, o2, e2 = _run(
        ["flatpak", "remote-add", "--if-not-exists", "flathub",
         "https://flathub.org/repo/flathub.flatpakrepo"],
        timeout=60,
    )
    return rc2, o1 + o2, e1 + e2


def _desktop_session() -> tuple[str | None, int | None]:
    """Return (username, uid) of the active graphical session, else (None, None)."""
    try:
        out = subprocess.run(
            ["loginctl", "list-sessions", "--no-legend"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None, None
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        session_id = parts[0]
        try:
            show = subprocess.run(
                ["loginctl", "show-session", session_id,
                 "-p", "Type", "-p", "Name", "-p", "User", "-p", "Active"],
                capture_output=True, text=True, timeout=5,
            )
        except subprocess.SubprocessError:
            continue
        data = dict(
            ln.split("=", 1) for ln in show.stdout.splitlines() if "=" in ln
        )
        if data.get("Active") == "yes" and data.get("Type") in ("x11", "wayland", "mir"):
            try:
                return data.get("Name"), int(data.get("User", "0"))
            except ValueError:
                return data.get("Name"), None
    return None, None


def _run_as_desktop_user(cmd, *, shell: bool = False, timeout: int = 300):
    user, uid = _desktop_session()
    if not user:
        return 1, "", "no active desktop session detected"
    home = f"/home/{user}"
    env = {
        **os.environ,
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{uid}/bus" if uid else "",
        "XDG_RUNTIME_DIR": f"/run/user/{uid}" if uid else "",
        "HOME": home,
        "USER": user,
    }
    if shell:
        full = ["runuser", "-u", user, "--", "bash", "-c", cmd]
    else:
        full = ["runuser", "-u", user, "--", *list(cmd)]
    return _run(full, env=env, timeout=timeout)


def _set_wallpaper_system(url: str) -> tuple[int, str, str]:
    if not url:
        return 1, "", "no url"
    bg_dir = Path("/usr/share/backgrounds")
    bg_dir.mkdir(parents=True, exist_ok=True)
    target = bg_dir / "manage-wallpaper.jpg"
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            target.write_bytes(r.read())
    except Exception as e:
        return 1, "", f"download failed: {e}"

    profile_dir = Path("/etc/dconf/profile")
    profile_dir.mkdir(parents=True, exist_ok=True)
    user_profile = profile_dir / "user"
    if "system-db:manage" not in (user_profile.read_text() if user_profile.exists() else ""):
        user_profile.write_text("user-db:user\nsystem-db:manage\n")

    db_dir = Path("/etc/dconf/db/manage.d")
    db_dir.mkdir(parents=True, exist_ok=True)
    (db_dir / "00-wallpaper").write_text(
        "[org/gnome/desktop/background]\n"
        f"picture-uri='file://{target}'\n"
        f"picture-uri-dark='file://{target}'\n"
        "picture-options='zoom'\n"
    )
    rc, o, e = _run(["dconf", "update"], timeout=30)
    return rc, f"wallpaper installed at {target}\n{o}", e


def _set_wallpaper_user(url: str) -> tuple[int, str, str]:
    user, uid = _desktop_session()
    if not user:
        return 1, "", "no active desktop session detected"
    target_dir = Path(f"/home/{user}/.local/share/backgrounds")
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "manage-wallpaper.jpg"
        with urllib.request.urlopen(url, timeout=60) as r:
            target.write_bytes(r.read())
        import pwd
        pw = pwd.getpwnam(user)
        os.chown(target, pw.pw_uid, pw.pw_gid)
    except Exception as e:
        return 1, "", f"download failed: {e}"
    uri = f"'file://{target}'"
    rc1, o1, e1 = _run_as_desktop_user(
        ["gsettings", "set", "org.gnome.desktop.background", "picture-uri", uri]
    )
    rc2, o2, e2 = _run_as_desktop_user(
        ["gsettings", "set", "org.gnome.desktop.background", "picture-uri-dark", uri]
    )
    return (rc1 or rc2), o1 + o2, e1 + e2


# --- self update ---


def _self_update(server: str, expected_version: str, expected_sha256: str) -> bool:
    """Download latest agent, atomically replace self, exit so systemd restarts us."""
    import hashlib
    import tempfile

    self_path = Path(__file__).resolve()
    log.info("Self-update: downloading %s -> %s", expected_version, self_path)
    try:
        with urllib.request.urlopen(f"{server}/agent/manage-agent.py", timeout=60) as r:
            content = r.read()
    except Exception as e:
        log.error("Self-update download failed: %s", e)
        return False
    actual = hashlib.sha256(content).hexdigest()
    if expected_sha256 and actual != expected_sha256:
        log.error("Self-update sha256 mismatch (got %s, want %s)", actual, expected_sha256)
        return False
    try:
        tmp = tempfile.NamedTemporaryFile(
            "wb", dir=str(self_path.parent), delete=False, prefix=".manage-agent.", suffix=".new"
        )
        tmp.write(content)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.chmod(tmp.name, 0o755)
        os.replace(tmp.name, self_path)
    except OSError as e:
        log.error("Self-update install failed: %s", e)
        return False
    log.info("Self-update installed; exiting to let systemd restart us.")
    os._exit(0)


# --- main loop ---


def checkin_loop(cfg: dict[str, Any]) -> None:
    server = cfg["server"]
    headers = {"Authorization": f"Bearer {cfg['token']}"}
    interval = int(cfg.get("checkin_interval_seconds", DEFAULT_INTERVAL))
    state: dict[str, Any] = {"force_self_update": False, "config": cfg}

    while True:
        try:
            resp = _http(
                "POST",
                f"{server}/api/agents/checkin",
                headers=headers,
                body={"system_info": collect_system_info()},
                timeout=30,
            )
            new_interval = int(resp.get("checkin_interval_seconds", interval))
            if new_interval != interval:
                interval = new_interval

            for task in resp.get("tasks") or []:
                try:
                    rc, out, err = execute_task(task, state)
                except Exception as e:
                    rc, out, err = 1, "", f"agent crash in execute_task: {e}"
                try:
                    _http(
                        "POST",
                        f"{server}/api/agents/tasks/{task['id']}/result",
                        headers=headers,
                        body={"exit_code": rc, "stdout": out, "stderr": err},
                        timeout=60,
                    )
                except Exception as e:
                    log.error("Failed to report result for task %s: %s", task["id"], e)

            update = resp.get("agent_update") or {}
            wanted = update.get("version")
            sha = update.get("sha256", "")
            should_update = state.get("force_self_update") or (wanted and wanted != AGENT_VERSION)
            if should_update and wanted:
                _self_update(server, wanted, sha)
                state["force_self_update"] = False
        except Exception as e:
            log.warning("Check-in failed: %s", e)
        time.sleep(interval)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "enroll":
        server = os.environ.get("MANAGE_SERVER")
        token = os.environ.get("MANAGE_TOKEN")
        if not server or not token:
            log.error("MANAGE_SERVER and MANAGE_TOKEN env vars required for enrollment")
            sys.exit(2)
        enroll(server, token)
        return

    cfg = load_config()
    log.info("Starting check-in loop against %s as %s", cfg["server"], cfg["agent_id"])
    checkin_loop(cfg)


if __name__ == "__main__":
    main()
