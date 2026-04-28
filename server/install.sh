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

# We pin to Python 3.12 explicitly. Newer Python versions (3.13+) lack
# prebuilt wheels for some of our deps (pydantic-core in particular), and
# letting pip fall back to compiling from Rust source needs ~1 GB of
# toolchain and minutes of CPU. 3.12 is well-supported by every dep we use
# and ships in Ubuntu 22.04+ either by default or as a non-default package.
PYTHON_BIN="${REEVECTL_PYTHON_BIN:-python3.12}"

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
  ca-certificates curl rsync

# Verify a candidate binary actually reports CPython 3.12 — not just exists.
# (Some distros ship a `python3.12` transitional package whose binary points
# to a newer interpreter; a presence check would let it through.)
verify_python_312() {
  local bin="$1"
  command -v "$bin" >/dev/null 2>&1 || return 1
  local ver
  ver=$("$bin" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null) || return 1
  [[ "$ver" == "3.12" ]]
}

# Install Python 3.12, trying three paths in order:
#   1. Host's default apt repos (works on Ubuntu 22.04 / 24.04)
#   2. The deadsnakes PPA (works on most Ubuntu LTS releases)
#   3. uv's standalone Python builds (works on any Linux, including
#      Ubuntu interim releases that deadsnakes doesn't track yet — e.g.
#      'resolute' / 25.10).
# On success, $PYTHON_BIN is set to a working Python 3.12 binary path.
ensure_python312() {
  # Try the host's default apt repos first.
  if apt-get install -y --no-install-recommends \
       python3.12 python3.12-venv python3.12-dev; then
    if verify_python_312 python3.12; then
      PYTHON_BIN="python3.12"
      return 0
    fi
  fi
  echo "[reevectl] python3.12 not in default apt — trying deadsnakes PPA..."
  if apt-get install -y --no-install-recommends software-properties-common gnupg \
     && add-apt-repository -y ppa:deadsnakes/ppa \
     && apt-get update -qq \
     && apt-get install -y --no-install-recommends \
            python3.12 python3.12-venv python3.12-dev; then
    if verify_python_312 python3.12; then
      PYTHON_BIN="python3.12"
      return 0
    fi
  fi
  echo "[reevectl] deadsnakes doesn't ship python3.12 for this Ubuntu release ('$(lsb_release -sc 2>/dev/null || echo unknown)')."
  # Remove the now-broken deadsnakes source so subsequent `apt update` calls don't 404.
  rm -f /etc/apt/sources.list.d/deadsnakes-ubuntu-ppa-*.list \
        /etc/apt/sources.list.d/deadsnakes-ubuntu-ppa-*.sources 2>/dev/null || true
  echo "[reevectl] Falling back to a standalone Python 3.12 build via uv (Astral)."
  install_python312_via_uv
}

# Install Astral's `uv` and use it to fetch a prebuilt CPython 3.12 from
# python-build-standalone. Stable, distro-independent, works behind any apt.
install_python312_via_uv() {
  if ! command -v uv >/dev/null 2>&1; then
    echo "[reevectl] Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
    if ! command -v uv >/dev/null 2>&1; then
      # Older installers ignore UV_INSTALL_DIR; uv ends up in $HOME/.local/bin.
      for cand in /root/.local/bin/uv "$HOME/.local/bin/uv" /root/.cargo/bin/uv; do
        if [[ -x "$cand" ]]; then
          install -m 0755 "$cand" /usr/local/bin/uv
          break
        fi
      done
    fi
  fi
  if ! command -v uv >/dev/null 2>&1; then
    echo "ERROR: uv install failed. See https://astral.sh/uv for manual setup." >&2
    return 1
  fi
  echo "[reevectl] Installing CPython 3.12 via uv (downloads ~30 MB)..."
  export UV_PYTHON_INSTALL_DIR=/opt/reevectl-python
  install -d -m 0755 "$UV_PYTHON_INSTALL_DIR"
  uv python install 3.12
  local resolved
  resolved=$(uv python find 3.12 2>/dev/null || true)
  if [[ -z "$resolved" || ! -x "$resolved" ]]; then
    resolved=$(find "$UV_PYTHON_INSTALL_DIR" -name 'python3.12' -type f -executable 2>/dev/null | head -n 1)
  fi
  if [[ -z "$resolved" || ! -x "$resolved" ]]; then
    echo "ERROR: uv finished but python3.12 binary not found under $UV_PYTHON_INSTALL_DIR." >&2
    return 1
  fi
  if ! verify_python_312 "$resolved"; then
    echo "ERROR: uv-installed binary at $resolved is not Python 3.12." >&2
    return 1
  fi
  PYTHON_BIN="$resolved"
  # Also expose a stable name for sysadmins.
  ln -sf "$resolved" /usr/local/bin/python3.12
  echo "[reevectl] uv-installed python3.12 at $resolved (also linked at /usr/local/bin/python3.12)"
}

if ! verify_python_312 "$PYTHON_BIN"; then
  ensure_python312
fi
if ! verify_python_312 "$PYTHON_BIN"; then
  WHAT=$("$PYTHON_BIN" --version 2>&1 || echo "cannot invoke")
  RESOLVED=$(command -v "$PYTHON_BIN" 2>/dev/null || echo "not found")
  cat >&2 <<EOF
ERROR: \$PYTHON_BIN ($PYTHON_BIN) is not a working Python 3.12.
       resolves to: $RESOLVED
       reports:     $WHAT
       Set REEVECTL_PYTHON_BIN to a path of a real Python 3.12 binary and re-run, e.g.:
         sudo REEVECTL_PYTHON_BIN=/usr/bin/python3.12 REEVECTL_DOMAIN=$REEVECTL_DOMAIN ./server/install.sh
EOF
  exit 2
fi
echo "[reevectl] Using Python: $($PYTHON_BIN --version) at $(command -v "$PYTHON_BIN")"

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
# If a venv already exists from a previous install, sanity-check that it's
# Python 3.12. Older runs may have created one with whichever `python3`
# was the system default at the time (3.14 on recent Ubuntu); if it's
# wrong, blow it away and rebuild.
if [[ -d "$INSTALL_PREFIX/.venv" ]]; then
  CURRENT_VER=""
  if [[ -x "$INSTALL_PREFIX/.venv/bin/python" ]]; then
    CURRENT_VER=$("$INSTALL_PREFIX/.venv/bin/python" -c \
      'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || true)
  fi
  if [[ "$CURRENT_VER" != "3.12" ]]; then
    echo "[reevectl] Existing venv reports Python '${CURRENT_VER:-unknown}', need 3.12 — recreating..."
    rm -rf "$INSTALL_PREFIX/.venv"
  fi
fi
if [[ ! -x "$INSTALL_PREFIX/.venv/bin/python" ]]; then
  echo "[reevectl] Creating Python virtualenv with $PYTHON_BIN..."
  "$PYTHON_BIN" -m venv "$INSTALL_PREFIX/.venv"
fi

# Belt + braces: verify the venv is actually 3.12 before installing deps.
# If this assertion fails, $PYTHON_BIN was a wrapper / transitional package
# whose -m venv ended up calling a different interpreter — bail out before
# pip wastes 10 minutes compiling pydantic-core from Rust source.
VENV_VER=$("$INSTALL_PREFIX/.venv/bin/python" -c \
  'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
if [[ "$VENV_VER" != "3.12" ]]; then
  cat >&2 <<EOF
ERROR: venv at $INSTALL_PREFIX/.venv reports Python $VENV_VER, expected 3.12.
       \$PYTHON_BIN: $PYTHON_BIN
       resolves to: $(command -v "$PYTHON_BIN" 2>/dev/null || echo 'not found')
       reports:     $("$PYTHON_BIN" --version 2>&1 || echo 'cannot invoke')

This usually means \$PYTHON_BIN is a wrapper or transitional package that
forwards to a different interpreter. Try pointing at the real binary:
  sudo REEVECTL_PYTHON_BIN=/usr/bin/python3.12 REEVECTL_DOMAIN=... ./server/install.sh

Or install python3.12 from deadsnakes:
  sudo add-apt-repository ppa:deadsnakes/ppa
  sudo apt update
  sudo apt install python3.12 python3.12-venv python3.12-dev
EOF
  exit 2
fi
echo "[reevectl] venv ready: $("$INSTALL_PREFIX/.venv/bin/python" --version) at $INSTALL_PREFIX/.venv"

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
