"""Web terminal: admin opens a WS in their browser; the agent runs a pty and
streams stdout via HTTP POST + reads stdin via long-polled HTTP GET.

In-memory only. Single-process — fine for the size of fleet this product targets.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .auth import require_admin, require_agent
from .db import get_db
from .models import Admin, Agent, Task, TerminalSession
from .views import templates

log = logging.getLogger(__name__)

router = APIRouter()


class _Session:
    def __init__(self) -> None:
        self.stdin_q: asyncio.Queue[bytes] = asyncio.Queue()
        self.admin_ws: Optional[WebSocket] = None
        self.closed = False
        self.opened_at = datetime.now(timezone.utc)


_sessions: dict[str, _Session] = {}
_loop: Optional[asyncio.AbstractEventLoop] = None


# ---------------------------------------------------------------------------
# Admin: open the terminal page (queues open_terminal task on the agent)
# ---------------------------------------------------------------------------


@router.post("/devices/{agent_id}/terminal")
def terminal_open(
    agent_id: str,
    request: Request,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    a = db.query(Agent).filter_by(agent_id=agent_id).first()
    if not a:
        raise HTTPException(status_code=404, detail="Agent not found")
    session_id = secrets.token_urlsafe(16)
    db.add(TerminalSession(session_id=session_id, agent_pk=a.id, opened_by=admin.username))
    db.add(Task(
        agent_pk=a.id,
        type="open_terminal",
        payload={"session_id": session_id},
        created_by=admin.username,
        batch_id=uuid.uuid4().hex,
        title=f"terminal session {session_id[:8]}",
    ))
    db.commit()
    _sessions[session_id] = _Session()
    return RedirectResponse(f"/devices/{agent_id}/terminal/{session_id}", status_code=303)


@router.get("/devices/{agent_id}/terminal/{session_id}", response_class=HTMLResponse)
def terminal_page(
    agent_id: str,
    session_id: str,
    request: Request,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    a = db.query(Agent).filter_by(agent_id=agent_id).first()
    if not a:
        raise HTTPException(status_code=404, detail="Agent not found")
    session = db.query(TerminalSession).filter_by(session_id=session_id, agent_pk=a.id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Terminal session not found")
    return templates.TemplateResponse(
        request,
        "terminal.html",
        {
            "active": "devices",
            "agent": a,
            "session_id": session_id,
            "flash": request.session.pop("flash", None),
        },
    )


# ---------------------------------------------------------------------------
# Admin browser WebSocket (xterm.js attaches here)
# ---------------------------------------------------------------------------


@router.websocket("/ws/terminal/{session_id}")
async def admin_ws(ws: WebSocket, session_id: str) -> None:
    user = (ws.scope.get("session") or {}).get("user")
    if not user:
        await ws.close(code=1008)
        return
    sess = _sessions.get(session_id)
    if not sess:
        # Late connect or server restart — accept then close so xterm shows the message.
        await ws.accept()
        await ws.send_text("[manage] session no longer available\r\n")
        await ws.close()
        return
    await ws.accept()
    sess.admin_ws = ws
    try:
        while True:
            data = await ws.receive_bytes()
            await sess.stdin_q.put(data)
    except WebSocketDisconnect:
        pass
    finally:
        # Tell the agent's stdin pump to give up.
        await sess.stdin_q.put(b"__CLOSE__")
        sess.closed = True
        sess.admin_ws = None


# ---------------------------------------------------------------------------
# Agent endpoints
# ---------------------------------------------------------------------------


@router.get("/api/agents/terminal/{session_id}/stdin")
async def agent_get_stdin(
    session_id: str,
    agent: Agent = Depends(require_agent),
) -> Response:
    sess = _sessions.get(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="No such session")
    try:
        chunk = await asyncio.wait_for(sess.stdin_q.get(), timeout=55)
        return Response(content=chunk, media_type="application/octet-stream")
    except asyncio.TimeoutError:
        return Response(content=b"", media_type="application/octet-stream")


@router.post("/api/agents/terminal/{session_id}/stdout")
async def agent_post_stdout(
    session_id: str,
    request: Request,
    agent: Agent = Depends(require_agent),
) -> dict:
    sess = _sessions.get(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="No such session")
    body = await request.body()
    if sess.admin_ws is not None and not sess.closed:
        try:
            await sess.admin_ws.send_text(body.decode("utf-8", errors="replace"))
        except Exception:
            pass
    return {"ok": True}


@router.post("/api/agents/terminal/{session_id}/close")
async def agent_close(
    session_id: str,
    agent: Agent = Depends(require_agent),
    db: Session = Depends(get_db),
) -> dict:
    sess = _sessions.pop(session_id, None)
    if sess and sess.admin_ws is not None:
        try:
            await sess.admin_ws.send_text("\r\n[manage] session closed\r\n")
            await sess.admin_ws.close()
        except Exception:
            pass
    row = db.query(TerminalSession).filter_by(session_id=session_id).first()
    if row and row.closed_at is None:
        row.closed_at = datetime.now(timezone.utc)
        db.commit()
    return {"ok": True}
