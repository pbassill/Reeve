from datetime import datetime, timezone
from typing import Optional

from .config import settings


def aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def relative_time(dt: Optional[datetime]) -> str:
    dt = aware(dt)
    if dt is None:
        return "never"
    delta = (datetime.now(timezone.utc) - dt).total_seconds()
    if delta < 0:
        return "in the future"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def is_online(last_seen: Optional[datetime]) -> bool:
    last_seen = aware(last_seen)
    if last_seen is None:
        return False
    delta = (datetime.now(timezone.utc) - last_seen).total_seconds()
    return delta <= settings.offline_after_seconds


def status_label(last_seen: Optional[datetime]) -> str:
    last_seen = aware(last_seen)
    if last_seen is None:
        return "offline"
    delta = (datetime.now(timezone.utc) - last_seen).total_seconds()
    if delta <= settings.offline_after_seconds:
        return "online"
    if delta <= settings.offline_after_seconds * 5:
        return "stale"
    return "offline"


def format_uptime(seconds: int) -> str:
    if seconds <= 0:
        return "—"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def format_bytes_gb(gb: int) -> str:
    if gb >= 1024:
        return f"{gb / 1024:.1f} TB"
    return f"{gb} GB"


def truncate_output(text: str) -> str:
    if not text:
        return ""
    if len(text) <= settings.max_task_output_chars:
        return text
    return text[: settings.max_task_output_chars] + "\n…[truncated]"
