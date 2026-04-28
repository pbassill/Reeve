#!/usr/bin/env bash
# Uninstall the native reevectl server install. Leaves data behind by default.
#
# Usage:
#   sudo ./server/uninstall.sh             # keeps /var/lib/reevectl and the env file
#   sudo PURGE=1 ./server/uninstall.sh     # also deletes data and config

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "ERROR: must run as root." >&2
  exit 2
fi

INSTALL_PREFIX="${INSTALL_PREFIX:-/opt/reevectl}"
DATA_DIR="${REEVECTL_DATA_DIR:-/var/lib/reevectl}"
ETC_DIR="${REEVECTL_ETC_DIR:-/etc/reevectl}"
SERVICE_USER="reevectl"

echo "[reevectl] Stopping and disabling service..."
systemctl disable --now reevectl.service 2>/dev/null || true
rm -f /etc/systemd/system/reevectl.service
systemctl daemon-reload

echo "[reevectl] Removing $INSTALL_PREFIX..."
rm -rf "$INSTALL_PREFIX"

if [[ "${PURGE:-}" == "1" ]]; then
  echo "[reevectl] PURGE=1 — also removing $DATA_DIR and $ETC_DIR..."
  rm -rf "$DATA_DIR" "$ETC_DIR"
  if id -u "$SERVICE_USER" >/dev/null 2>&1; then
    userdel "$SERVICE_USER" || true
  fi
else
  echo "[reevectl] Keeping $DATA_DIR and $ETC_DIR. Re-run with PURGE=1 to remove them."
fi

echo "[reevectl] Note: Caddy/nginx config and the system 'reevectl' user are left in place."
echo "[reevectl] Done."
