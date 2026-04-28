from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Admin(Base):
    __tablename__ = "admins"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    auth_source: Mapped[str] = mapped_column(String(16), default="local")
    display_name: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class Group(Base):
    __tablename__ = "groups"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    members: Mapped[list["Agent"]] = relationship(
        secondary="agent_groups", back_populates="groups", lazy="selectin"
    )


class EnrollmentToken(Base):
    __tablename__ = "enrollment_tokens"
    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    label: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    default_group_id: Mapped[Optional[int]] = mapped_column(ForeignKey("groups.id"), nullable=True)
    default_group: Mapped[Optional[Group]] = relationship(lazy="joined")


class Agent(Base):
    __tablename__ = "agents"
    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    token_hash: Mapped[str] = mapped_column(String(255))
    hostname: Mapped[str] = mapped_column(String(255), default="")
    enrolled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    os_name: Mapped[str] = mapped_column(String(128), default="")
    os_version: Mapped[str] = mapped_column(String(128), default="")
    kernel: Mapped[str] = mapped_column(String(128), default="")
    arch: Mapped[str] = mapped_column(String(32), default="")
    cpu_model: Mapped[str] = mapped_column(String(255), default="")
    cpu_cores: Mapped[int] = mapped_column(Integer, default=0)
    cpu_percent: Mapped[float] = mapped_column(default=0.0)
    mem_total_mb: Mapped[int] = mapped_column(Integer, default=0)
    mem_used_mb: Mapped[int] = mapped_column(Integer, default=0)
    disk_total_gb: Mapped[int] = mapped_column(Integer, default=0)
    disk_used_gb: Mapped[int] = mapped_column(Integer, default=0)
    uptime_seconds: Mapped[int] = mapped_column(Integer, default=0)
    ip_address: Mapped[str] = mapped_column(String(64), default="")
    agent_version: Mapped[str] = mapped_column(String(32), default="")
    logged_in_user: Mapped[str] = mapped_column(String(64), default="")

    # Extended hardware inventory (collected at enrollment + via inventory_refresh).
    manufacturer: Mapped[str] = mapped_column(String(128), default="")
    product_name: Mapped[str] = mapped_column(String(128), default="")
    serial_number: Mapped[str] = mapped_column(String(128), default="")
    bios_version: Mapped[str] = mapped_column(String(64), default="")
    gpu_model: Mapped[str] = mapped_column(String(255), default="")
    mac_address: Mapped[str] = mapped_column(String(32), default="")

    # Last known package-list hash, lets the agent skip re-uploading 1500 packages every check-in.
    packages_hash: Mapped[str] = mapped_column(String(64), default="")
    packages_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    maintenance_window_start: Mapped[Optional[str]] = mapped_column(String(5), nullable=True)
    maintenance_window_end: Mapped[Optional[str]] = mapped_column(String(5), nullable=True)

    groups: Mapped[list[Group]] = relationship(
        secondary="agent_groups", back_populates="members", lazy="selectin"
    )
    tasks: Mapped[list["Task"]] = relationship(back_populates="agent", cascade="all, delete-orphan")


class AgentGroup(Base):
    __tablename__ = "agent_groups"
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True)
    __table_args__ = (UniqueConstraint("agent_id", "group_id"),)


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    agent_pk: Mapped[int] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String(32), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    dispatched_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    stdout: Mapped[str] = mapped_column(Text, default="")
    stderr: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(64), default="")
    batch_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(255), default="")

    agent: Mapped[Agent] = relationship(back_populates="tasks", lazy="joined")


class Schedule(Base):
    __tablename__ = "schedules"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Trigger: cron expression OR simple interval. Exactly one of these.
    cron_expr: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    interval_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Optional daily maintenance window in UTC, "HH:MM" strings (inclusive start, exclusive end).
    window_start: Mapped[Optional[str]] = mapped_column(String(5), nullable=True)
    window_end: Mapped[Optional[str]] = mapped_column(String(5), nullable=True)

    # Task template
    task_type: Mapped[str] = mapped_column(String(32))
    task_payload: Mapped[dict] = mapped_column(JSON, default=dict)

    # Targeting (mirrors CreateTaskRequest semantics)
    target_all: Mapped[bool] = mapped_column(Boolean, default=False)
    target_agent_ids: Mapped[list] = mapped_column(JSON, default=list)
    target_group_ids: Mapped[list] = mapped_column(JSON, default=list)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


class Policy(Base):
    __tablename__ = "policies"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_remediate: Mapped[bool] = mapped_column(Boolean, default=False)
    target_group_ids: Mapped[list] = mapped_column(JSON, default=list)
    rules: Mapped[list[dict]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ComplianceCheck(Base):
    __tablename__ = "compliance_checks"
    id: Mapped[int] = mapped_column(primary_key=True)
    agent_pk: Mapped[int] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    policy_id: Mapped[int] = mapped_column(ForeignKey("policies.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="unknown")  # ok | drift | error | unknown
    drift: Mapped[list[dict]] = mapped_column(JSON, default=list)
    last_checked: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (UniqueConstraint("agent_pk", "policy_id", name="uq_compliance_agent_policy"),)

    agent: Mapped[Agent] = relationship(lazy="joined")
    policy: Mapped[Policy] = relationship(lazy="joined")


class Package(Base):
    __tablename__ = "packages"
    id: Mapped[int] = mapped_column(primary_key=True)
    agent_pk: Mapped[int] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(16), index=True)  # apt | snap | flatpak
    name: Mapped[str] = mapped_column(String(255), index=True)
    version: Mapped[str] = mapped_column(String(128), default="")
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("agent_pk", "source", "name", name="uq_pkg_agent_source_name"),)


class AlertRule(Base):
    __tablename__ = "alert_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    kind: Mapped[str] = mapped_column(String(32))  # offline | task_failed | disk_full | cpu_high
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    webhook_url: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AlertEvent(Base):
    __tablename__ = "alert_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("alert_rules.id", ondelete="CASCADE"), index=True)
    agent_pk: Mapped[Optional[int]] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), nullable=True)
    fired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)

    rule: Mapped[AlertRule] = relationship(lazy="joined")
    agent: Mapped[Optional[Agent]] = relationship(lazy="joined")


class TerminalSession(Base):
    __tablename__ = "terminal_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    agent_pk: Mapped[int] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    opened_by: Mapped[str] = mapped_column(String(64))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    agent: Mapped[Agent] = relationship(lazy="joined")


class AgentRole(Base):
    __tablename__ = "agent_roles"
    id: Mapped[int] = mapped_column(primary_key=True)
    agent_pk: Mapped[int] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    role_type: Mapped[str] = mapped_column(String(32), index=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="installing")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    installed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    install_task_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    notes: Mapped[str] = mapped_column(Text, default="")

    agent: Mapped[Agent] = relationship(lazy="joined")
    install_task: Mapped[Optional[Task]] = relationship(foreign_keys=[install_task_id], lazy="joined")
