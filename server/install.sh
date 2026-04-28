#!/usr/bin/env bash
# Native installer for the reevectl server on Ubuntu 22.04 / 24.04.
#
# Idempotent — safe to re-run to upgrade an existing install.
#
# Usage:
#   sudo REEVECTL_DOMAIN=reevectl.example.com ./server/install.sh
#
# Optional env vars:
#   REEVECTL_TLS=caddy   (default — install Caddy and auto-issue Let's Encrypt cert)
#   REEVECTL_TLS=none    (skip web server; uvicorn will still bind to 127.0.0.1)
#   REEVECTL_TLS=manual  (skip web server; you'll handle TLS yourself, see notes)
#
#   REEVECTL_ADMIN_USER=admin
#   REEVECTL_ADMIN_PASSWORD=  (blank = generate random and write to disk)
#   REEVECTL_PORT=8000
#
# Layout after install:
#   /opt/reevectl/server/        (code)
#   /opt/reevectl/agent/         (agent files served to clients)
#   /opt/reevectl/.venv/         (python virtualenv)
#   /etc/reevectl/reevectl.env   (config — readable only by root + the reevectl user)
#   /var/lib/reevectl/           (sqlite db, secret key, initial admin password)
#   /etc/systemd/system/reevectl.service

set -euo pipefail

INSTALL_PREFIX="${INSTALL_PREFIX:-/opt/reevectl}"
DATA_DIR="${REEVECTL_DATA_DIR:-/var/lib/reevectl}"
ETC_DIR="${REEVECTL_ETC_DIR:-/etc/reevectl}"
SERVICE_USER="reevectl"
TLS_MODE="${REEVECTL_TLS:-caddy}"
PORT="${REEVECTL_PORT:-8000}"

if [[ $EUID -ne 0 ]]; then
  echo "ERROR: install.sh must run as root (sudo)." >&2
  exit 2
fi

if [[ -z "${REEVECTL_DOMAIN:-}" && "$TLS_MODE" != "none" && "$TLS_MODE" != "manual" ]]; then
  echo "ERROR: REEVECTL_DOMAIN must be set (or use REEVECTL_TLS=none/manual to skip Caddy)." >&2
  exit 2
fi

# Resolve the source directory — wherever this script lives, the repo root
# is its parent (i.e. server/install.sh -> repo at ..).
SRC_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ ! -d "$SRC_ROOT/server/app" || ! -d "$SRC_ROOT/agent" ]]; then
  echo "ERROR: expected to find $SRC_ROOT/server/app and $SRC_ROOT/agent." >&2
  echo "Run this script from a checkout of the reevectl repository." >&2
  exit 2
fi

echo "[reevectl] Installing from $SRC_ROOT"
echo "[reevectl] Prefix: $INSTALL_PREFIX  Data: $DATA_DIR  TLS: $TLS_MODE"

# --- 1. System packages ------------------------------------------------------
echo "[reevectl] Installing system packages..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip \
  ca-certificates curl rsync

# --- 2. Service user ---------------------------------------------------------
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  echo "[reevectl] Creating system user $SERVICE_USER..."
  useradd --system --home-dir "$INSTALL_PREFIX" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

# --- 3. Directories ----------------------------------------------------------
install -d -m 0755 -o "$SERVICE_USER" -g "$SERVICE_USER" "$INSTALL_PREFIX"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$DATA_DIR"
install -d -m 0755                                       "$ETC_DIR"

# --- 4. Sync code (use rsync so re-runs upgrade in place) --------------------
echo "[reevectl] Syncing code to $INSTALL_PREFIX..."
rsync -a --delete \
  --exclude='__pycache__' --exclude='*.pyc' \
  "$SRC_ROOT/server/" "$INSTALL_PREFIX/server/"
rsync -a --delete \
  --exclude='__pycache__' --exclude='*.pyc' \
  "$SRC_ROOT/agent/"  "$INSTALL_PREFIX/agent/"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_PREFIX/server" "$INSTALL_PREFIX/agent"

# --- 5. Virtualenv -----------------------------------------------------------
if [[ ! -x "$INSTALL_PREFIX/.venv/bin/python" ]]; then
  echo "[reevectl] Creating Python virtualenv..."
  python3 -m venv "$INSTALL_PREFIX/.venv"
fi
echo "[reevectl] Installing/upgrading Python dependencies..."
"$INSTALL_PREFIX/.venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_PREFIX/.venv/bin/pip" install --quiet --upgrade -r "$INSTALL_PREFIX/server/requirements.txt"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_PREFIX/.venv"

# --- 6. Environment file -----------------------------------------------------
ENV_FILE="$ETC_DIR/reevectl.env"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "[reevectl] Writing $ENV_FILE..."
  PUBLIC_URL_DEFAULT="${REEVECTL_PUBLIC_URL:-https://${REEVECTL_DOMAIN:-localhost}}"
  cat > "$ENV_FILE" <<EOF
REEVECTL_PUBLIC_URL=${PUBLIC_URL_DEFAULT}
REEVECTL_DATA_DIR=${DATA_DIR}
REEVECTL_AGENT_DIR=${INSTALL_PREFIX}/agent
REEVECTL_PORT=${PORT}
REEVECTL_ADMIN_USER=${REEVECTL_ADMIN_USER:-admin}
REEVECTL_ADMIN_PASSWORD=${REEVECTL_ADMIN_PASSWORD:-}
REEVECTL_CHECKIN_INTERVAL=${REEVECTL_CHECKIN_INTERVAL:-30}
EOF
  chown root:"$SERVICE_USER" "$ENV_FILE"
  chmod 0640 "$ENV_FILE"
else
  echo "[reevectl] $ENV_FILE already exists — leaving it alone."
fi

# --- 7. Systemd unit ---------------------------------------------------------
echo "[reevectl] Installing systemd unit..."
install -m 0644 "$INSTALL_PREFIX/server/reevectl.service" /etc/systemd/system/reevectl.service
systemctl daemon-reload
systemctl enable reevectl.service >/dev/null
systemctl restart reevectl.service

# --- 8. Reverse proxy --------------------------------------------------------
case "$TLS_MODE" in
  caddy)
    if ! command -v caddy >/dev/null; then
      echo "[reevectl] Installing Caddy..."
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
      echo "[reevectl] Writing $CADDY_FILE..."
      cat > "$CADDY_FILE" <<EOF
${REEVECTL_DOMAIN} {
    encode zstd gzip
    reverse_proxy 127.0.0.1:${PORT}
}
EOF
    else
      SNIPPET="/etc/caddy/reevectl.Caddyfile"
      echo "[reevectl] Existing custom $CADDY_FILE detected — wrote snippet to $SNIPPET instead."
      echo "[reevectl] Add this line to your $CADDY_FILE:"
      echo "[reevectl]     import $SNIPPET"
      cat > "$SNIPPET" <<EOF
${REEVECTL_DOMAIN} {
    encode zstd gzip
    reverse_proxy 127.0.0.1:${PORT}
}
EOF
    fi
    systemctl enable caddy >/dev/null
    systemctl restart caddy
    ;;
  none|manual)
    echo "[reevectl] Skipping reverse-proxy setup (REEVECTL_TLS=$TLS_MODE)."
    if [[ "$TLS_MODE" == "manual" ]]; then
      cat <<EOF
[reevectl] You chose manual TLS. The reevectl server is listening on 127.0.0.1:${PORT}.
[reevectl] Point your existing reverse proxy (nginx, Apache, HAProxy, Cloudflare Tunnel, …) at it.
[reevectl] Make sure to forward the Host header and pass-through WebSocket upgrades.
EOF
    fi
    ;;
  *)
    echo "ERROR: unknown REEVECTL_TLS=$TLS_MODE (expected caddy|none|manual)" >&2
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
echo "[reevectl] Done. Service status:"
systemctl --no-pager --lines=0 status reevectl.service || true
echo
echo "[reevectl] Logs:    journalctl -u reevectl -f"
echo "[reevectl] Open:    ${REEVECTL_PUBLIC_URL:-https://${REEVECTL_DOMAIN:-<your-domain>}}"
