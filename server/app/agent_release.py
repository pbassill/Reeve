"""Reads the agent script bundled into the server image and exposes its
version + sha256 so check-ins can tell agents to self-update.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path


def _resolve_agent_dir() -> Path:
    """Locate the directory containing the bundled agent files.

    Search order:
      1. $MANAGE_AGENT_DIR (explicit override; used by the systemd unit).
      2. /agent  (Docker image layout — see server/Dockerfile).
      3. ../agent relative to this file (local dev / git checkout).
    """
    env = os.environ.get("MANAGE_AGENT_DIR")
    if env:
        return Path(env)
    docker_path = Path("/agent")
    if docker_path.exists():
        return docker_path
    return Path(__file__).resolve().parent.parent.parent / "agent"


AGENT_DIR = _resolve_agent_dir()
_AGENT_PATH = AGENT_DIR / "manage-agent.py"


_state: dict[str, str] = {"version": "", "sha256": ""}


def load() -> None:
    if not _AGENT_PATH.exists():
        return
    raw = _AGENT_PATH.read_bytes()
    _state["sha256"] = hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8", errors="replace")
    m = re.search(r'^AGENT_VERSION\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    _state["version"] = m.group(1) if m else ""


def info() -> dict[str, str]:
    return {"version": _state["version"], "sha256": _state["sha256"]}


def path() -> Path:
    return _AGENT_PATH
