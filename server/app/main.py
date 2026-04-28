import secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import agent_release, migrations
from .admin_settings_views import router as admin_settings_router
from .admins_views import router as admins_router
from .api_admin import router as admin_router
from .api_agents import router as agents_router
from .auth import hash_password
from .config import settings
from .db import SessionLocal, engine
from .models import Admin, Base
from .roles_views import router as roles_router
from .views import router as views_router
from .ws import router as ws_router

app = FastAPI(title="Manage", docs_url=None, redoc_url=None)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="manage_session",
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
app.include_router(agents_router)
app.include_router(admin_router)
app.include_router(ws_router)


@app.on_event("startup")
def startup() -> None:
    Base.metadata.create_all(bind=engine)
    migrations.run()
    agent_release.load()
    _ensure_bootstrap_admin()


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
            print(f"[manage] Bootstrap admin '{username}' created. Password written to "
                  f"{settings.data_dir / 'INITIAL_ADMIN_PASSWORD'}")
        else:
            print(f"[manage] Bootstrap admin '{username}' created from MANAGE_ADMIN_PASSWORD.")


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


@app.get("/agent/manage-agent.py", response_class=PlainTextResponse)
def agent_source() -> Response:
    return PlainTextResponse(
        (agent_release.AGENT_DIR / "manage-agent.py").read_text(),
        headers={"Content-Type": "text/x-python"},
    )


@app.get("/agent/manage-agent.service", response_class=PlainTextResponse)
def agent_unit() -> Response:
    return PlainTextResponse(
        (agent_release.AGENT_DIR / "manage-agent.service").read_text(),
        headers={"Content-Type": "text/plain"},
    )
