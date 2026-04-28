"""Settings page: SQLite backup download + restore upload, agent release info."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from . import agent_release
from .auth import require_admin
from .config import settings
from .db import engine, get_db
from .models import Admin
from .views import templates

router = APIRouter()

_BACKUP_LOCK = threading.Lock()


def _sqlite_path() -> Path | None:
    url = str(engine.url)
    if not url.startswith("sqlite"):
        return None
    # sqlite:////absolute/path or sqlite:///relative/path
    raw = engine.url.database
    return Path(raw) if raw else None


@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    admin: Admin = Depends(require_admin),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    sqlite_p = _sqlite_path()
    db_size = sqlite_p.stat().st_size if sqlite_p and sqlite_p.exists() else 0
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "active": "settings",
            "agent_release": agent_release.info(),
            "db_path": str(sqlite_p) if sqlite_p else "(non-SQLite database)",
            "db_size_mb": round(db_size / (1024 * 1024), 2),
            "is_sqlite": sqlite_p is not None,
            "public_url": settings.public_url,
            "checkin_interval": settings.checkin_interval_seconds,
            "flash": request.session.pop("flash", None),
        },
    )


@router.get("/settings/backup")
def backup_download(admin: Admin = Depends(require_admin)) -> FileResponse:
    sqlite_p = _sqlite_path()
    if not sqlite_p or not sqlite_p.exists():
        raise HTTPException(status_code=400, detail="Backup only supported for SQLite")
    with _BACKUP_LOCK:
        out = Path(tempfile.gettempdir()) / f"manage-backup-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.db"
        # Use SQLite's online backup API for a consistent snapshot.
        src = sqlite3.connect(str(sqlite_p))
        try:
            dst = sqlite3.connect(str(out))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
    filename = f"manage-backup-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.db"
    return FileResponse(
        path=str(out),
        media_type="application/x-sqlite3",
        filename=filename,
    )


@router.post("/settings/restore")
async def restore_upload(
    request: Request,
    db_file: UploadFile = File(...),
    admin: Admin = Depends(require_admin),
) -> RedirectResponse:
    sqlite_p = _sqlite_path()
    if not sqlite_p:
        raise HTTPException(status_code=400, detail="Restore only supported for SQLite")
    contents = await db_file.read()
    if not contents.startswith(b"SQLite format 3\x00"):
        request.session["flash"] = "Uploaded file is not a SQLite database."
        return RedirectResponse("/settings", status_code=303)

    # Write to a side file, validate by opening, then atomically replace.
    sqlite_p.parent.mkdir(parents=True, exist_ok=True)
    staged = sqlite_p.with_suffix(sqlite_p.suffix + ".incoming")
    staged.write_bytes(contents)
    try:
        con = sqlite3.connect(str(staged))
        con.execute("PRAGMA quick_check").fetchall()
        con.close()
    except sqlite3.DatabaseError as e:
        staged.unlink(missing_ok=True)
        request.session["flash"] = f"Uploaded DB failed integrity check: {e}"
        return RedirectResponse("/settings", status_code=303)

    # Close all current SQLAlchemy connections so we can replace the file.
    engine.dispose()
    os.replace(staged, sqlite_p)
    request.session["flash"] = "Restore staged. The server will restart in a moment to load it."
    # Schedule shutdown so the response gets sent first.
    threading.Timer(1.0, lambda: os._exit(0)).start()
    return RedirectResponse("/settings", status_code=303)
