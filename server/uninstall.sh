#!/usr/bin/env bash
# Uninstall the native Manage server install. Leaves data behind by default.
#
# Usage:
#   sudo ./server/uninstall.sh             # keeps /var/lib/manage and the env file
#   sudo PURGE=1 ./server/uninstall.sh     # also deletes data and config

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "ERROR: must run as root." >&2
  exit 2
fi

INSTALL_PREFIX="${INSTALL_PREFIX:-/opt/manage}"
DATA_DIR="${MANAGE_DATA_DIR:-/var/lib/manage}"
ETC_DIR="${MANAGE_ETC_DIR:-/etc/manage}"
SERVICE_USER="manage"

echo "[manage] Stopping and disabling service..."
systemctl disable --now manage.service 2>/dev/null || true
rm -f /etc/systemd/system/manage.service
systemctl daemon-reload

echo "[manage] Removing $INSTALL_PREFIX..."
rm -rf "$INSTALL_PREFIX"

if [[ "${PURGE:-}" == "1" ]]; then
  echo "[manage] PURGE=1 — also removing $DATA_DIR and $ETC_DIR..."
  rm -rf "$DATA_DIR" "$ETC_DIR"
  if id -u "$SERVICE_USER" >/dev/null 2>&1; then
    userdel "$SERVICE_USER" || true
  fi
else
  echo "[manage] Keeping $DATA_DIR and $ETC_DIR. Re-run with PURGE=1 to remove them."
fi

echo "[manage] Note: Caddy/nginx config and the system 'manage' user are left in place."
echo "[manage] Done."
