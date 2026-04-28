"""WebSocket broadcast for live device status on the dashboard.

Admins connect to /ws/devices (cookie auth via SessionMiddleware).
api_agents.checkin calls broadcast_agent_status(agent) after each check-in;
this fans the update out to every connected admin.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .models import Agent
from .utils import relative_time, status_label

log = logging.getLogger(__name__)

router = APIRouter()


class Manager:
    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self.clients.add(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self.clients.discard(ws)

    async def _send_all(self, message: dict[str, Any]) -> None:
        text = json.dumps(message)
        async with self._lock:
            stale: list[WebSocket] = []
            for ws in self.clients:
                try:
                    await ws.send_text(text)
                except Exception:
                    stale.append(ws)
            for s in stale:
                self.clients.discard(s)

    def broadcast_threadsafe(self, message: dict[str, Any]) -> None:
        if not self.clients:
            return
        loop = self._loop or asyncio.get_event_loop_policy().get_event_loop()
        try:
            asyncio.run_coroutine_threadsafe(self._send_all(message), loop)
        except RuntimeError:
            log.debug("Could not schedule broadcast (no running loop)")


manager = Manager()


def broadcast_agent_status(agent: Agent) -> None:
    """Build a snapshot of an agent and push it to all connected dashboards."""
    payload = {
        "type": "agent_status",
        "agent_id": agent.agent_id,
        "hostname": agent.hostname,
        "ip_address": agent.ip_address,
        "cpu_percent": round(agent.cpu_percent or 0, 1),
        "mem_used_mb": agent.mem_used_mb,
        "mem_total_mb": agent.mem_total_mb,
        "disk_used_gb": agent.disk_used_gb,
        "disk_total_gb": agent.disk_total_gb,
        "uptime_seconds": agent.uptime_seconds,
        "last_seen": relative_time(agent.last_seen),
        "status": status_label(agent.last_seen),
    }
    manager.broadcast_threadsafe(payload)


@router.websocket("/ws/devices")
async def ws_devices(ws: WebSocket) -> None:
    session = ws.scope.get("session") or {}
    user = session.get("user")
    if not user:
        await ws.close(code=1008)
        return
    await manager.connect(ws)
    try:
        while True:
            # We don't expect client messages; just keep the connection open.
            msg = await ws.receive_text()
            if msg == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(ws)


@router.on_event("startup")
async def _attach() -> None:
    manager.attach_loop(asyncio.get_running_loop())
