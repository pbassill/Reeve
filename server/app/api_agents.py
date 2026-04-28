import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from . import agent_release
from .auth import generate_token, hash_token, require_agent
from .config import settings
from .db import get_db
from .models import Agent, EnrollmentToken, Task
from .schemas import (
    AgentUpdate,
    CheckinRequest,
    CheckinResponse,
    EnrollRequest,
    EnrollResponse,
    SystemInfo,
    TaskOut,
    TaskResultRequest,
)
from .utils import aware, truncate_output

router = APIRouter(prefix="/api/agents", tags=["agents"])


def _apply_system_info(agent: Agent, info: SystemInfo) -> None:
    if info.hostname:
        agent.hostname = info.hostname
    agent.os_name = info.os_name
    agent.os_version = info.os_version
    agent.kernel = info.kernel
    agent.arch = info.arch
    agent.cpu_model = info.cpu_model
    agent.cpu_cores = info.cpu_cores
    agent.cpu_percent = info.cpu_percent
    agent.mem_total_mb = info.mem_total_mb
    agent.mem_used_mb = info.mem_used_mb
    agent.disk_total_gb = info.disk_total_gb
    agent.disk_used_gb = info.disk_used_gb
    agent.uptime_seconds = info.uptime_seconds
    agent.ip_address = info.ip_address
    agent.agent_version = info.agent_version
    agent.logged_in_user = info.logged_in_user
    agent.last_seen = datetime.now(timezone.utc)


@router.post("/enroll", response_model=EnrollResponse)
def enroll(req: EnrollRequest, db: Session = Depends(get_db)) -> EnrollResponse:
    enrollment = (
        db.query(EnrollmentToken)
        .filter(EnrollmentToken.token == req.enrollment_token)
        .first()
    )
    if not enrollment or enrollment.revoked:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid enrollment token")
    expires_at = aware(enrollment.expires_at)
    if expires_at and expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Enrollment token expired")

    public_id = uuid.uuid4().hex
    token = generate_token()
    agent = Agent(
        agent_id=public_id,
        token_hash=hash_token(token),
        hostname=req.system_info.hostname or "unknown",
    )
    _apply_system_info(agent, req.system_info)
    db.add(agent)
    db.flush()
    if enrollment.default_group_id:
        agent.groups.append(enrollment.default_group)
    db.commit()
    return EnrollResponse(
        agent_id=public_id,
        token=token,
        checkin_interval_seconds=settings.checkin_interval_seconds,
    )


@router.post("/checkin", response_model=CheckinResponse)
def checkin(
    req: CheckinRequest,
    agent: Agent = Depends(require_agent),
    db: Session = Depends(get_db),
) -> CheckinResponse:
    _apply_system_info(agent, req.system_info)

    pending = (
        db.query(Task)
        .filter(Task.agent_pk == agent.id, Task.status == "pending")
        .order_by(Task.id.asc())
        .all()
    )
    now = datetime.now(timezone.utc)
    out: list[TaskOut] = []
    for t in pending:
        t.status = "running"
        t.dispatched_at = now
        out.append(TaskOut(id=t.id, type=t.type, payload=t.payload or {}))
    db.commit()

    # Notify connected dashboards.
    try:
        from . import ws  # avoid import cycle at module load
        ws.broadcast_agent_status(agent)
    except Exception:
        pass

    release = agent_release.info()
    update = None
    if release["version"] and release["version"] != agent.agent_version:
        update = AgentUpdate(
            version=release["version"],
            sha256=release["sha256"],
            url="/agent/manage-agent.py",
        )

    return CheckinResponse(
        tasks=out,
        checkin_interval_seconds=settings.checkin_interval_seconds,
        agent_update=update,
    )


@router.post("/tasks/{task_id}/result")
def task_result(
    task_id: int,
    req: TaskResultRequest,
    agent: Agent = Depends(require_agent),
    db: Session = Depends(get_db),
) -> dict:
    task = db.get(Task, task_id)
    if not task or task.agent_pk != agent.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    task.exit_code = req.exit_code
    task.stdout = truncate_output(req.stdout)
    task.stderr = truncate_output(req.stderr)
    task.status = "complete" if req.exit_code == 0 else "failed"
    task.completed_at = datetime.now(timezone.utc)
    db.commit()
    return {"ok": True}
