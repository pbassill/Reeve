from typing import Any, Optional

from pydantic import BaseModel, Field


class SystemInfo(BaseModel):
    hostname: str = ""
    os_name: str = ""
    os_version: str = ""
    kernel: str = ""
    arch: str = ""
    cpu_model: str = ""
    cpu_cores: int = 0
    cpu_percent: float = 0.0
    mem_total_mb: int = 0
    mem_used_mb: int = 0
    disk_total_gb: int = 0
    disk_used_gb: int = 0
    uptime_seconds: int = 0
    ip_address: str = ""
    agent_version: str = ""
    logged_in_user: str = ""


class EnrollRequest(BaseModel):
    enrollment_token: str
    system_info: SystemInfo


class EnrollResponse(BaseModel):
    agent_id: str
    token: str
    checkin_interval_seconds: int


class CheckinRequest(BaseModel):
    system_info: SystemInfo


class TaskOut(BaseModel):
    id: int
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class AgentUpdate(BaseModel):
    version: str
    sha256: str
    url: str


class CheckinResponse(BaseModel):
    tasks: list[TaskOut]
    checkin_interval_seconds: int
    agent_update: Optional[AgentUpdate] = None


class TaskResultRequest(BaseModel):
    exit_code: int
    stdout: str = ""
    stderr: str = ""


class CreateTaskRequest(BaseModel):
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    title: str = ""
    target_agent_ids: list[str] = Field(default_factory=list)
    target_group_ids: list[int] = Field(default_factory=list)
    target_all: bool = False


class CreateGroupRequest(BaseModel):
    name: str
    description: str = ""


class UpdateGroupMembersRequest(BaseModel):
    agent_ids: list[str]


class CreateEnrollmentTokenRequest(BaseModel):
    label: str = ""
    default_group_id: Optional[int] = None
    expires_in_hours: Optional[int] = None
