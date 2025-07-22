"""
UTC DateTime utilities for consistent timestamp handling
"""

from datetime import datetime, timezone
from typing import Optional


def utc_now() -> datetime:
    """Get current UTC datetime - single source of truth"""
    return datetime.now(timezone.utc)


def utc_timestamp() -> str:
    """Get current UTC timestamp in ISO format"""
    return utc_now().isoformat()


def format_utc_log_time(dt: Optional[datetime] = None) -> str:
    """Format datetime for logging (YYYY-MM-DD HH:MM:SS UTC)"""
    if dt is None:
        dt = utc_now()
    return dt.strftime('%Y-%m-%d %H:%M:%S UTC')


def format_utc_display_time(dt: Optional[datetime] = None) -> str:
    """Format datetime for UI display (HH:MM:SS UTC)"""
    if dt is None:
        dt = utc_now()
    return dt.strftime('%H:%M:%S UTC')


def ensure_utc(dt: datetime) -> datetime:
    """Ensure datetime is in UTC timezone"""
    if dt.tzinfo is None:
        # Assume naive datetime is already UTC
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)