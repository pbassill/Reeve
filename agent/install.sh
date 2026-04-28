#!/usr/bin/env bash
# Manage agent installer.
#
# Usage on each Ubuntu host (run as root):
#   curl -fsSL https://your-server/install.sh | sudo MANAGE_SERVER=https://your-server MANAGE_TOKEN=<enrollment-token> bash
#
# When this script is served by the Manage server itself, MANAGE_SERVER will
# be auto-injected as @@SERVER_URL@@ at request time.
set -euo pipefail

SERVER="${MANAGE_SERVER:-@@SERVER_URL@@}"
TOKEN="${MANAGE_TOKEN:-}"

if [[ "$SERVER" == "@@SERVER_URL@@" || -z "$SERVER" ]]; then
  echo "ERROR: MANAGE_SERVER not set" >&2
  exit 2
fi
if [[ -z "$TOKEN" ]]; then
  echo "ERROR: MANAGE_TOKEN not set (generate one in /enrollment)" >&2
  exit 2
fi

if [[ $EUID -ne 0 ]]; then
  echo "ERROR: install.sh must run as root (use sudo)" >&2
  exit 2
fi

echo "[manage] Installing agent against ${SERVER}"

# Python 3 is in main on every supported Ubuntu — but ensure it's there.
if ! command -v python3 >/dev/null; then
  apt-get update
  apt-get install -y python3
fi

# dconf-cli for set_wallpaper (best-effort; only needed on desktops).
if ! command -v dconf >/dev/null; then
  apt-get install -y dconf-cli || true
fi

install -d -m 0755 /opt/manage-agent
install -d -m 0700 /etc/manage-agent

curl -fsSL "${SERVER}/agent/manage-agent.py" -o /opt/manage-agent/manage-agent.py
chmod 0755 /opt/manage-agent/manage-agent.py

curl -fsSL "${SERVER}/agent/manage-agent.service" -o /etc/systemd/system/manage-agent.service

# Enroll once before starting the service.
MANAGE_SERVER="${SERVER}" MANAGE_TOKEN="${TOKEN}" \
  /usr/bin/python3 /opt/manage-agent/manage-agent.py enroll

systemctl daemon-reload
systemctl enable --now manage-agent.service

echo "[manage] Agent installed and started. Check journalctl -u manage-agent -f for logs."
