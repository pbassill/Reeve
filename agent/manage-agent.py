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

AGENT_VERSION = "0.2.0"
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
    return 1, "", f"unknown task type {t}"


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
    state: dict[str, Any] = {"force_self_update": False}

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
