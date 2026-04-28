#!/usr/bin/env bash
# reevectl agent installer.
#
# Usage on each Ubuntu host (run as root):
#   curl -fsSL https://your-server/install.sh | sudo REEVECTL_SERVER=https://your-server REEVECTL_TOKEN=<enrollment-token> bash
#
# When this script is served by the reevectl server itself, REEVECTL_SERVER will
# be auto-injected as @@SERVER_URL@@ at request time.
set -euo pipefail

SERVER="${REEVECTL_SERVER:-@@SERVER_URL@@}"
TOKEN="${REEVECTL_TOKEN:-}"

if [[ "$SERVER" == "@@SERVER_URL@@" || -z "$SERVER" ]]; then
  echo "ERROR: REEVECTL_SERVER not set" >&2
  exit 2
fi
if [[ -z "$TOKEN" ]]; then
  echo "ERROR: REEVECTL_TOKEN not set (generate one in /enrollment)" >&2
  exit 2
fi

if [[ $EUID -ne 0 ]]; then
  echo "ERROR: install.sh must run as root (use sudo)" >&2
  exit 2
fi

echo "[reevectl] Installing agent against ${SERVER}"

# Python 3 is in main on every supported Ubuntu — but ensure it's there.
if ! command -v python3 >/dev/null; then
  apt-get update
  apt-get install -y python3
fi

# dconf-cli for set_wallpaper (best-effort; only needed on desktops).
if ! command -v dconf >/dev/null; then
  apt-get install -y dconf-cli || true
fi

install -d -m 0755 /opt/reevectl-agent
install -d -m 0700 /etc/reevectl-agent

curl -fsSL "${SERVER}/agent/reevectl-agent.py" -o /opt/reevectl-agent/reevectl-agent.py
chmod 0755 /opt/reevectl-agent/reevectl-agent.py

curl -fsSL "${SERVER}/agent/reevectl-agent.service" -o /etc/systemd/system/reevectl-agent.service

# Enroll once before starting the service.
REEVECTL_SERVER="${SERVER}" REEVECTL_TOKEN="${TOKEN}" \
  /usr/bin/python3 /opt/reevectl-agent/reevectl-agent.py enroll

systemctl daemon-reload
systemctl enable --now reevectl-agent.service

echo "[reevectl] Agent installed and started. Check journalctl -u reevectl-agent -f for logs."
