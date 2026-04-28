import asyncio
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import agent_release, alerts, migrations, scheduler
from .admin_settings_views import router as admin_settings_router
from .admins_views import router as admins_router
from .alerts_views import router as alerts_router
from .api_admin import router as admin_router
from .api_agents import router as agents_router
from .auth import hash_password
from .config import settings
from .db import SessionLocal, engine
from .inventory_views import router as inventory_router
from .models import Admin, Base
from .policy_views import router as policy_router
from .roles_views import router as roles_router
from .schedule_views import router as schedule_router
from .terminal_views import router as terminal_router
from .views import router as views_router
from .ws import router as ws_router

_background_stop: asyncio.Event | None = None
_background_tasks: list[asyncio.Task] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    Base.metadata.create_all(bind=engine)
    migrations.run()
    agent_release.load()
    _ensure_bootstrap_admin()
    # Wire the running loop into modules that need to schedule from sync code.
    from . import terminal_views, ws  # local import to avoid cycles at module load
    loop = asyncio.get_running_loop()
    ws.manager.attach_loop(loop)
    terminal_views._loop = loop
    global _background_stop
    _background_stop = asyncio.Event()
    _background_tasks.append(asyncio.create_task(scheduler.run_loop(_background_stop)))
    _background_tasks.append(asyncio.create_task(alerts.run_loop(_background_stop)))
    try:
        yield
    finally:
        # Shutdown — give the background loops a chance to exit cleanly.
        if _background_stop:
            _background_stop.set()
        for t in _background_tasks:
            try:
                await asyncio.wait_for(t, timeout=5)
            except (asyncio.TimeoutError, Exception):
                t.cancel()


app = FastAPI(title="reevectl", docs_url=None, redoc_url=None, lifespan=lifespan)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="reevectl_session",
    same_site="lax",
    https_only=False,
    max_age=60 * 60 * 24 * 7,
)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(views_router)
app.include_router(admins_router)
app.include_router(admin_settings_router)
app.include_router(roles_router)
app.include_router(schedule_router)
app.include_router(policy_router)
app.include_router(inventory_router)
app.include_router(alerts_router)
app.include_router(terminal_router)
app.include_router(agents_router)
app.include_router(admin_router)
app.include_router(ws_router)


def _ensure_bootstrap_admin() -> None:
    with SessionLocal() as db:
        if db.query(Admin).first():
            return
        username = settings.bootstrap_admin_user
        password = settings.bootstrap_admin_password or secrets.token_urlsafe(16)
        db.add(Admin(username=username, password_hash=hash_password(password)))
        db.commit()
        if not settings.bootstrap_admin_password:
            (settings.data_dir / "INITIAL_ADMIN_PASSWORD").write_text(
                f"{username}\n{password}\n"
            )
            print(f"[reevectl] Bootstrap admin '{username}' created. Password written to "
                  f"{settings.data_dir / 'INITIAL_ADMIN_PASSWORD'}")
        else:
            print(f"[reevectl] Bootstrap admin '{username}' created from REEVECTL_ADMIN_PASSWORD.")


@app.get("/health", response_class=PlainTextResponse)
def health() -> str:
    return "ok"


@app.get("/install.sh", response_class=PlainTextResponse)
def install_script(request: Request) -> Response:
    """Public agent install script. Run on each Ubuntu host you want to enroll."""
    server_url = settings.public_url or str(request.base_url).rstrip("/")
    body = (agent_release.AGENT_DIR / "install.sh").read_text() if (agent_release.AGENT_DIR / "install.sh").exists() else "#!/usr/bin/env bash\necho 'install.sh missing on server' >&2\nexit 1\n"
    body = body.replace("@@SERVER_URL@@", server_url)
    return PlainTextResponse(body, headers={"Content-Type": "text/x-shellscript"})


@app.get("/agent/reevectl-agent.py", response_class=PlainTextResponse)
def agent_source() -> Response:
    return PlainTextResponse(
        (agent_release.AGENT_DIR / "reevectl-agent.py").read_text(),
        headers={"Content-Type": "text/x-python"},
    )


@app.get("/agent/reevectl-agent.service", response_class=PlainTextResponse)
def agent_unit() -> Response:
    return PlainTextResponse(
        (agent_release.AGENT_DIR / "reevectl-agent.service").read_text(),
        headers={"Content-Type": "text/plain"},
    )
