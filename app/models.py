from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from enum import Enum
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo


class MeetingMode(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    FLEXIBLE = "flexible"


class MeetingPriority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class RequestStatus(str, Enum):
    DRAFT = "draft"
    NEGOTIATING = "negotiating"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    NEEDS_INPUT = "needs_input"


class MessageIntent(str, Enum):
    NEW_REQUEST = "new_request"
    ACCEPT = "accept"
    PROPOSE_TIME = "propose_time"
    RESCHEDULE = "reschedule"
    CANCEL = "cancel"
    GENERAL = "general"
    FOLLOW_UP = "follow_up"
    CONFIRMATION = "confirmation"


@dataclass
class AvailabilityHint:
    start: datetime
    end: datetime
    exact: bool
    source_text: str


@dataclass
class AnalysisResult:
    intent: MessageIntent
    priority: MeetingPriority
    mode: MeetingMode
    duration_minutes: int
    objective: str
    summary: str
    agenda: str
    requested_location: Optional[str]
    availability_hints: list


@dataclass
class CandidateOption:
    starts_at: datetime
    ends_at: datetime
    mode: MeetingMode
    location_label: Optional[str] = None
    location_address: Optional[str] = None
    meeting_link: Optional[str] = None
    travel_minutes: int = 0
    score: float = 0.0
    rationale: str = ""


def parse_clock(value: str) -> time:
    return time.fromisoformat(value.strip())


def parse_iso_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=ZoneInfo("UTC"))
    return parsed


def ensure_timezone(value: datetime, timezone_name: str) -> datetime:
    zone = ZoneInfo(timezone_name)
    if value.tzinfo is None:
        return value.replace(tzinfo=zone)
    return value.astimezone(zone)


def combine_local(day: date, clock_value: time, timezone_name: str) -> datetime:
    return datetime.combine(day, clock_value, tzinfo=ZoneInfo(timezone_name))


def isoformat_minute(value: datetime) -> str:
    return value.isoformat(timespec="minutes")


def display_datetime(value: Optional[str], timezone_name: str) -> Optional[str]:
    if not value:
        return None
    return ensure_timezone(parse_iso_datetime(value), timezone_name).strftime("%a, %b %d %H:%M")


def row_bool(value: Any) -> bool:
    return bool(int(value)) if value is not None else False


def workspace_to_dict(row: Dict[str, Any]) -> Dict[str, Any]:
    row = dict(row)
    return row
