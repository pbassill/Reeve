# Manage

A self-hosted, open-source alternative to **Zorin Grid** for centrally managing
fleets of Ubuntu workstations and servers. One web console, one lightweight
agent per host, no SaaS dependency.

Originally built because Zorin Grid was previewed but never released. Designed
for schools, labs, branch offices, and small-to-medium IT shops that already
run Linux.

---

## What it does

- **Live fleet inventory** — hostname, OS, kernel, CPU/RAM/disk, IP, uptime,
  manufacturer/model/serial, GPU, MAC, logged-in user. Live status via
  WebSocket; the dashboard updates as agents check in.
- **Groups** — tag devices ("Lab-1", "Servers") and target tasks at groups,
  individual hosts, or the whole fleet.
- **Remote tasks** — shell, apt / snap / flatpak (install / remove / upgrade),
  push file, reboot, shutdown, set wallpaper (system + per-user), GSettings,
  dock favorites. Full audit log per task with stdout/stderr.
- **Web terminal** — root shell into any agent via xterm.js in your browser,
  no SSH key handling required. Bridged through the agent's existing transport.
- **Server roles (one-click installers)** — provision a fully-configured stack
  on any agent:
  - **Authentication Server** — Samba 4 Active Directory Domain Controller
    (LDAP + Kerberos + DNS).
  - **File Server** — Samba shares with departmental ACLs and per-user homes.
    Standalone or domain-joined.
  - **Print Server** — CUPS with optional remote admin.
  - **DHCP + DNS** — dnsmasq for branch-office / lab networks.
- **Scheduled tasks** — cron expressions or simple intervals; optional daily
  maintenance windows; per-agent maintenance windows for "never reboot during
  business hours".
- **Compliance / drift detection** — define policies (packages installed,
  packages absent, services running, file contents match a regex), apply them
  to groups, get a green/red dashboard, optionally auto-remediate.
- **Software inventory** — fleet-wide search ("which hosts run firefox <
  120?"). apt + snap + flatpak. Hash-based incremental upload.
- **Alerts** — rule-based (offline > N minutes, disk > N %, CPU > N %, any
  task failed) → Slack-compatible webhook.
- **Multi-admin** — local accounts via the UI **and** LDAP/AD single sign-on,
  optionally restricted to a specific group.
- **Backup / restore** — one-click SQLite snapshot via the online-backup API;
  upload to restore.
- **Agent self-update** — server publishes the latest agent version + sha256;
  agents update themselves between check-ins. No SSH-and-rebuild dance.
- **Two deployment paths** — Docker Compose with auto-TLS via Caddy, **or**
  native systemd install on the host.

---

## Quick start

### Docker (simplest)

```bash
git clone <this-repo> manage
cd manage
cp .env.example .env
# Edit .env — set MANAGE_DOMAIN and MANAGE_PUBLIC_URL.
docker compose up -d --build
docker compose exec manage cat /data/INITIAL_ADMIN_PASSWORD
```

Open `https://<your-domain>/` and sign in.

### Native (Ubuntu 22.04 / 24.04)

```bash
git clone <this-repo> manage
cd manage
sudo MANAGE_DOMAIN=manage.example.com ./server/install.sh
```

The script installs Python deps, creates a `manage` system user, lays the
project at `/opt/manage`, writes `/etc/manage/manage.env`, drops a systemd
unit, installs Caddy from upstream, and prints the bootstrap admin password.

Re-running the script upgrades in place. To uninstall: `sudo
./server/uninstall.sh` (add `PURGE=1` to also drop the database).

### Enrolling an Ubuntu host

In the dashboard, **Enrollment → Generate token** prints a one-line install
command. Run it on each Ubuntu machine:

```bash
curl -fsSL https://manage.example.com/install.sh \
  | sudo MANAGE_SERVER=https://manage.example.com MANAGE_TOKEN=<token> bash
```

The agent installs to `/opt/manage-agent`, registers a systemd unit, enrolls,
and starts checking in (default 30s).

---

## Architecture

```
            ┌──────────────────────┐
 admin ──▶  │  manage server       │
 browser    │  ─ FastAPI + SQLite  │  ─── WebSocket  ───▶  admin browser (live status, terminal)
            │  ─ scheduler loop    │
            │  ─ alert sweep loop  │
            └──────┬───────────────┘
                   │ HTTPS (Caddy or native systemd, no inbound on agents)
                   ▼
            ┌──────────────────────┐
            │  manage-agent.py     │  pure stdlib, runs as root
            │  ─ systemd service   │  Ubuntu host
            │  ─ polls every 30s   │
            └──────────────────────┘
```

- **Agents poll outbound** — no inbound ports needed on managed machines.
  Works for road-warrior laptops behind NAT.
- **Single-process server** — FastAPI + SQLite + a couple of asyncio
  background loops. Scales to thousands of hosts on a small VM.
- **Single binary on each host** — `manage-agent.py` is pure stdlib (no pip
  install). Self-updates from the server.
- **Audit-logged** — every task has stdout/stderr/exit code stored on the
  server, with the admin who issued it.

---

## Concepts

| Thing | What it is |
| --- | --- |
| **Agent** | An Ubuntu machine running `manage-agent.py`. |
| **Group** | A label you apply to agents to target them in bulk. |
| **Task** | A unit of work (shell command, apt install, reboot, …) sent to one or more agents. |
| **Role** | A bundle of installation steps that turns an agent into a configured Auth/File/Print/DHCP server. |
| **Schedule** | A recurring task (cron or interval) with an optional maintenance window. |
| **Policy** | A set of rules describing the desired state. Evaluated on agents; drift is reported and optionally auto-remediated. |
| **Alert rule** | Triggers on fleet conditions (offline, disk full, CPU high, task failed) and POSTs to a webhook. |

### Task types

| Type | Notes |
| --- | --- |
| `shell` | Arbitrary bash, runs as root. |
| `apt_install` / `apt_remove` / `apt_upgrade` | Standard Debian package management. |
| `snap_install` / `snap_remove` / `snap_refresh` | Snap. |
| `flatpak_install` / `flatpak_remove` / `flatpak_update` | Flatpak; auto-installs flatpak + flathub remote on first use. |
| `push_file` | Write a file (base64-encoded) to a path, with mode. |
| `reboot` / `shutdown` | Scheduled 60s out so the result can flow back. |
| `set_wallpaper` | System-wide via dconf override. |
| `user_shell` / `user_gsettings` / `set_wallpaper_user` / `set_dock_favorites` | Run as the active desktop user via `runuser` + dbus. |
| `install_auth_server` / `install_file_server` / `install_print_server` / `install_dhcp_dns` | Role provisioners. |
| `check_compliance` | Evaluates policy rules; returns drift JSON. |
| `inventory_refresh` | Uploads installed packages (apt + snap + flatpak). |
| `open_terminal` | Opens a pty bridged to the admin browser. |
| `self_update` | Force the agent to pull the latest version from the server. |

---

## Server roles in detail

### Authentication Server (Samba AD-DC)

Provisions a real LDAP + Kerberos + DNS domain controller. Form fields:

- **Realm** (FQDN, uppercase, e.g. `EXAMPLE.LOCAL`)
- **NetBIOS domain** (1-15 chars, e.g. `EXAMPLE`)
- **Administrator password** (min 8 chars; never persisted on the manage
  server — wiped from the task payload as soon as the agent picks it up)
- **DNS forwarder**

Idempotent: re-running detects an existing provision and just makes sure the
service is up. `chattr +i`s `/etc/resolv.conf` so NetworkManager doesn't
clobber the loopback resolver.

After install, add a user with a shell task on the DC:
```
samba-tool user create alice 'TempPass123!'
```

### File Server (Samba)

Two modes:

- **Standalone** — local Unix users, `tdbsam` Samba PDB.
- **Domain-joined** — uses the AD created above. winbind for AD user
  resolution, RID-based idmap, `nsswitch.conf` patched, `net ads join`.

Departments become `dept-<name>` Unix groups + Samba shares with `setgid 2770`
so files inherit the group. Per-user `[homes]` share is included.

### Print Server (CUPS)

Installs CUPS + every printer driver Ubuntu ships. Optionally exposes the
admin UI to the LAN and adds a chosen user to the `lpadmin` group.

### DHCP + DNS (dnsmasq)

The lightweight branch-office server: dnsmasq handles both DHCP and forwarding
DNS. Disables `systemd-resolved`, replaces `/etc/resolv.conf`, writes
`/etc/dnsmasq.d/manage.conf` with the configured scope. Useful as the LAN
infra companion to the AD DC.

---

## Compliance policies

Define rules in JSON:

```json
[
  {"kind": "package_installed", "params": {"packages": ["ufw", "fail2ban", "unattended-upgrades"]}},
  {"kind": "package_absent",    "params": {"packages": ["telnetd"]}},
  {"kind": "service_running",   "params": {"services": ["ssh", "ufw"]}},
  {"kind": "file_contains",     "params": {"path": "/etc/ssh/sshd_config", "regex": "^\\s*PermitRootLogin\\s+no"}}
]
```

Apply to one or more groups. Click **Check now** to fire across the fleet
immediately, or pair with a schedule running task type `check_compliance`
with payload `{"policy_id": <id>}` to evaluate periodically.

Toggle **Auto-remediate** to have the server queue `apt_install` /
`apt_remove` tasks for package-related drift automatically.

---

## Schedules

Two trigger styles:

- **Cron** — standard 5-field UTC, e.g. `0 3 * * 0` (Sundays at 03:00 UTC).
- **Interval** — every N seconds.

Optional **daily maintenance window** (`HH:MM`–`HH:MM` UTC). If a schedule
fires outside the window, it waits until the next acceptable tick. **Per-agent
maintenance windows** override at the agent level: even if the schedule is
window-less, an agent with `maintenance_window_start = 22:00, end = 04:00`
will skip business-hours tasks.

The schedule fires the *task template* (any task type) at the specified
targets (single agent / groups / all).

---

## Alerts

Built-in rule kinds:

- `offline` — fires when an agent's last check-in is older than N minutes.
  Auto-resolves when the agent comes back. Configurable threshold.
- `disk_full` — disk usage above N %.
- `cpu_high` — CPU above N %.
- `task_failed` — fires once per failed task.

Each rule has a webhook URL — a Slack incoming-webhook URL works as-is. The
payload is `{"text": "[manage] <summary>", "kind": "...", "rule": "...",
"details": {...}}`.

Sweep runs once a minute.

---

## LDAP / Active Directory login

Set in `.env` (Docker) or `/etc/manage/manage.env` (native):

```bash
MANAGE_LDAP_URL=ldaps://ad.example.com
MANAGE_LDAP_USE_SSL=true
MANAGE_LDAP_BIND_DN=CN=ManageBind,OU=Service,DC=example,DC=com
MANAGE_LDAP_BIND_PASSWORD=changeme
MANAGE_LDAP_USER_SEARCH_BASE=OU=Users,DC=example,DC=com
MANAGE_LDAP_USER_FILTER=(sAMAccountName={username})
# optional — restrict admin login to a group:
MANAGE_LDAP_ADMIN_GROUP_DN=CN=Manage Admins,OU=Groups,DC=example,DC=com
```

Login flow tries local password first, then LDAP. On first successful LDAP
sign-in the user gets an `Admin` row with `auth_source=ldap` (no password
stored). Local accounts are managed in **Admins**.

---

## Backup / restore

In **Settings**:

- **Download backup** — uses SQLite's online-backup API, safe to run while
  agents are checking in.
- **Upload restore** — replaces the database after passing
  `PRAGMA quick_check`. The server schedules an immediate exit so systemd /
  Docker brings it back with the new file.

---

## Self-update

The server reads `AGENT_VERSION` and computes a SHA-256 of
`agent/manage-agent.py` at startup. Every check-in response includes
`agent_update: {version, sha256, url}` when the agent is behind. The agent
downloads, hash-verifies, atomically replaces itself, and exits — systemd
restarts it. Admins can force an update by queuing the `self_update` task.

---

## API

All admin pages back onto a JSON API at `/api/admin/*` (auth via session
cookie or Bearer token). A few highlights:

- `POST /api/admin/tasks` — queue any task type at an agent / group / fleet.
- `POST /api/admin/roles` — install a server role.
- `POST /api/admin/groups`, `PUT /api/admin/groups/{id}/members` — group CRUD.
- `POST /api/admin/enrollment-tokens` — generate enrollment tokens.

Agent-facing endpoints are at `/api/agents/*` and use Bearer-token auth with
the per-agent token issued at enrollment.

---

## Project layout

```
manage/
├── docker-compose.yml          # Docker deploy
├── deploy/Caddyfile
├── server/
│   ├── Dockerfile
│   ├── install.sh              # Native Ubuntu installer
│   ├── uninstall.sh
│   ├── manage.service          # systemd unit (native deploy)
│   ├── manage.env.example
│   ├── requirements.txt
│   └── app/
│       ├── main.py             # FastAPI app + lifespan (scheduler + alerts)
│       ├── models.py           # SQLAlchemy 2.0 schema
│       ├── api_agents.py       # Agent-facing API
│       ├── api_admin.py        # Admin JSON API + task validation
│       ├── views.py            # Devices / groups / tasks / login HTML
│       ├── roles_views.py      # /roles
│       ├── schedule_views.py   # /schedules
│       ├── policy_views.py     # /policies
│       ├── inventory_views.py  # /inventory + agent package upload
│       ├── alerts_views.py     # /alerts
│       ├── alerts.py           # Sweep + webhook dispatch loops
│       ├── scheduler.py        # Schedule materialiser loop
│       ├── terminal_views.py   # Web terminal bridge
│       ├── ws.py               # Live-status WebSocket
│       ├── admins_views.py     # /admins
│       ├── admin_settings_views.py # /settings (backup/restore)
│       ├── ldap_auth.py
│       ├── auth.py
│       ├── migrations.py
│       ├── agent_release.py    # Reads AGENT_VERSION + sha256 for self-update
│       ├── static/style.css
│       └── templates/
└── agent/
    ├── manage-agent.py         # Pure-stdlib agent
    ├── manage-agent.service    # systemd unit installed on managed hosts
    └── install.sh              # Served at <server>/install.sh
```

---

## Development

The agent is pure stdlib — runs on any modern Ubuntu without pip. The server
needs Python 3.12+ and the deps in `server/requirements.txt`. For local dev:

```bash
cd server
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
MANAGE_DATA_DIR=./data \
MANAGE_PUBLIC_URL=http://localhost:8000 \
MANAGE_ADMIN_PASSWORD=devpass \
uvicorn app.main:app --reload --port 8000
```

Then enroll a test VM with `MANAGE_SERVER=http://your-laptop-ip:8000`.

---

## Security notes

- All agent traffic is bearer-token authenticated and outbound-only.
- Admin passwords are stored bcrypt-hashed; LDAP-sourced admins have no
  local password.
- Role admin passwords (Samba AD admin password, file-server join password)
  are scrubbed from the task payload as soon as the agent picks them up —
  they live in the database for at most one check-in interval.
- The web terminal opens a **root** shell on the agent. Treat dashboard
  access accordingly: deploy behind TLS (Caddy by default), use strong admin
  passwords, restrict to LDAP-group members in production.
- Native deploy runs the server as a non-root `manage` user with
  `ProtectSystem=strict`. The agent must run as root to do its job (apt,
  systemctl, push files anywhere).

---

## License

MIT. See [LICENSE](LICENSE) (add one if you fork).
