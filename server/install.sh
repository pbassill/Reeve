#!/usr/bin/env bash
# Native installer for the Manage server on Ubuntu 22.04 / 24.04.
#
# Idempotent — safe to re-run to upgrade an existing install.
#
# Usage:
#   sudo MANAGE_DOMAIN=manage.example.com ./server/install.sh
#
# Optional env vars:
#   MANAGE_TLS=caddy   (default — install Caddy and auto-issue Let's Encrypt cert)
#   MANAGE_TLS=none    (skip web server; uvicorn will still bind to 127.0.0.1)
#   MANAGE_TLS=manual  (skip web server; you'll handle TLS yourself, see notes)
#
#   MANAGE_ADMIN_USER=admin
#   MANAGE_ADMIN_PASSWORD=  (blank = generate random and write to disk)
#   MANAGE_PORT=8000
#
# Layout after install:
#   /opt/manage/server/        (code)
#   /opt/manage/agent/         (agent files served to clients)
#   /opt/manage/.venv/         (python virtualenv)
#   /etc/manage/manage.env     (config — readable only by root + the manage user)
#   /var/lib/manage/           (sqlite db, secret key, initial admin password)
#   /etc/systemd/system/manage.service

set -euo pipefail

INSTALL_PREFIX="${INSTALL_PREFIX:-/opt/manage}"
DATA_DIR="${MANAGE_DATA_DIR:-/var/lib/manage}"
ETC_DIR="${MANAGE_ETC_DIR:-/etc/manage}"
SERVICE_USER="manage"
TLS_MODE="${MANAGE_TLS:-caddy}"
PORT="${MANAGE_PORT:-8000}"

if [[ $EUID -ne 0 ]]; then
  echo "ERROR: install.sh must run as root (sudo)." >&2
  exit 2
fi

if [[ -z "${MANAGE_DOMAIN:-}" && "$TLS_MODE" != "none" && "$TLS_MODE" != "manual" ]]; then
  echo "ERROR: MANAGE_DOMAIN must be set (or use MANAGE_TLS=none/manual to skip Caddy)." >&2
  exit 2
fi

# Resolve the source directory — wherever this script lives, the repo root
# is its parent (i.e. server/install.sh -> repo at ..).
SRC_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ ! -d "$SRC_ROOT/server/app" || ! -d "$SRC_ROOT/agent" ]]; then
  echo "ERROR: expected to find $SRC_ROOT/server/app and $SRC_ROOT/agent." >&2
  echo "Run this script from a checkout of the manage repository." >&2
  exit 2
fi

echo "[manage] Installing from $SRC_ROOT"
echo "[manage] Prefix: $INSTALL_PREFIX  Data: $DATA_DIR  TLS: $TLS_MODE"

# --- 1. System packages ------------------------------------------------------
echo "[manage] Installing system packages..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip \
  ca-certificates curl rsync

# --- 2. Service user ---------------------------------------------------------
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  echo "[manage] Creating system user $SERVICE_USER..."
  useradd --system --home-dir "$INSTALL_PREFIX" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

# --- 3. Directories ----------------------------------------------------------
install -d -m 0755 -o "$SERVICE_USER" -g "$SERVICE_USER" "$INSTALL_PREFIX"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$DATA_DIR"
install -d -m 0755                                       "$ETC_DIR"

# --- 4. Sync code (use rsync so re-runs upgrade in place) --------------------
echo "[manage] Syncing code to $INSTALL_PREFIX..."
rsync -a --delete \
  --exclude='__pycache__' --exclude='*.pyc' \
  "$SRC_ROOT/server/" "$INSTALL_PREFIX/server/"
rsync -a --delete \
  --exclude='__pycache__' --exclude='*.pyc' \
  "$SRC_ROOT/agent/"  "$INSTALL_PREFIX/agent/"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_PREFIX/server" "$INSTALL_PREFIX/agent"

# --- 5. Virtualenv -----------------------------------------------------------
if [[ ! -x "$INSTALL_PREFIX/.venv/bin/python" ]]; then
  echo "[manage] Creating Python virtualenv..."
  python3 -m venv "$INSTALL_PREFIX/.venv"
fi
echo "[manage] Installing/upgrading Python dependencies..."
"$INSTALL_PREFIX/.venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_PREFIX/.venv/bin/pip" install --quiet --upgrade -r "$INSTALL_PREFIX/server/requirements.txt"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_PREFIX/.venv"

# --- 6. Environment file -----------------------------------------------------
ENV_FILE="$ETC_DIR/manage.env"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "[manage] Writing $ENV_FILE..."
  PUBLIC_URL_DEFAULT="${MANAGE_PUBLIC_URL:-https://${MANAGE_DOMAIN:-localhost}}"
  cat > "$ENV_FILE" <<EOF
MANAGE_PUBLIC_URL=${PUBLIC_URL_DEFAULT}
MANAGE_DATA_DIR=${DATA_DIR}
MANAGE_AGENT_DIR=${INSTALL_PREFIX}/agent
MANAGE_PORT=${PORT}
MANAGE_ADMIN_USER=${MANAGE_ADMIN_USER:-admin}
MANAGE_ADMIN_PASSWORD=${MANAGE_ADMIN_PASSWORD:-}
MANAGE_CHECKIN_INTERVAL=${MANAGE_CHECKIN_INTERVAL:-30}
EOF
  chown root:"$SERVICE_USER" "$ENV_FILE"
  chmod 0640 "$ENV_FILE"
else
  echo "[manage] $ENV_FILE already exists — leaving it alone."
fi

# --- 7. Systemd unit ---------------------------------------------------------
echo "[manage] Installing systemd unit..."
install -m 0644 "$INSTALL_PREFIX/server/manage.service" /etc/systemd/system/manage.service
systemctl daemon-reload
systemctl enable manage.service >/dev/null
systemctl restart manage.service

# --- 8. Reverse proxy --------------------------------------------------------
case "$TLS_MODE" in
  caddy)
    if ! command -v caddy >/dev/null; then
      echo "[manage] Installing Caddy..."
      apt-get install -y --no-install-recommends debian-keyring debian-archive-keyring apt-transport-https gnupg
      curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
      curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        > /etc/apt/sources.list.d/caddy-stable.list
      apt-get update -qq
      apt-get install -y caddy
    fi

    CADDY_FILE="/etc/caddy/Caddyfile"
    DEFAULT_MARKER="# The Caddyfile is an easy way to configure your Caddy web server"
    SHOULD_OVERWRITE=true
    if [[ -f "$CADDY_FILE" ]] && ! grep -q "$DEFAULT_MARKER" "$CADDY_FILE"; then
      # Existing custom Caddyfile — don't clobber.
      SHOULD_OVERWRITE=false
    fi
    if $SHOULD_OVERWRITE; then
      echo "[manage] Writing $CADDY_FILE..."
      cat > "$CADDY_FILE" <<EOF
${MANAGE_DOMAIN} {
    encode zstd gzip
    reverse_proxy 127.0.0.1:${PORT}
}
EOF
    else
      SNIPPET="/etc/caddy/manage.Caddyfile"
      echo "[manage] Existing custom $CADDY_FILE detected — wrote snippet to $SNIPPET instead."
      echo "[manage] Add this line to your $CADDY_FILE:"
      echo "[manage]     import $SNIPPET"
      cat > "$SNIPPET" <<EOF
${MANAGE_DOMAIN} {
    encode zstd gzip
    reverse_proxy 127.0.0.1:${PORT}
}
EOF
    fi
    systemctl enable caddy >/dev/null
    systemctl restart caddy
    ;;
  none|manual)
    echo "[manage] Skipping reverse-proxy setup (MANAGE_TLS=$TLS_MODE)."
    if [[ "$TLS_MODE" == "manual" ]]; then
      cat <<EOF
[manage] You chose manual TLS. The manage server is listening on 127.0.0.1:${PORT}.
[manage] Point your existing reverse proxy (nginx, Apache, HAProxy, Cloudflare Tunnel, …) at it.
[manage] Make sure to forward the Host header and pass-through WebSocket upgrades.
EOF
    fi
    ;;
  *)
    echo "ERROR: unknown MANAGE_TLS=$TLS_MODE (expected caddy|none|manual)" >&2
    exit 2
    ;;
esac

# --- 9. Surface the bootstrap admin password --------------------------------
sleep 2  # let the service write the file on first start
INITIAL_PWD_FILE="$DATA_DIR/INITIAL_ADMIN_PASSWORD"
if [[ -f "$INITIAL_PWD_FILE" ]]; then
  echo
  echo "==========================================================================="
  echo "  Bootstrap admin credentials (also stored at $INITIAL_PWD_FILE):"
  echo "---------------------------------------------------------------------------"
  cat "$INITIAL_PWD_FILE"
  echo "==========================================================================="
fi

echo
echo "[manage] Done. Service status:"
systemctl --no-pager --lines=0 status manage.service || true
echo
echo "[manage] Logs:    journalctl -u manage -f"
echo "[manage] Open:    ${MANAGE_PUBLIC_URL:-https://${MANAGE_DOMAIN:-<your-domain>}}"
