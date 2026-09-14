from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from app.models import (
    AnalysisResult,
    AvailabilityHint,
    MeetingMode,
    MeetingPriority,
    MessageIntent,
)

WEEKDAY_INDEX = {
    "monday": 0,
    "mon": 0,
    "tuesday": 1,
    "tue": 1,
    "wednesday": 2,
    "wed": 2,
    "thursday": 3,
    "thu": 3,
    "friday": 4,
    "fri": 4,
    "saturday": 5,
    "sat": 5,
    "sunday": 6,
    "sun": 6,
}

PERIOD_WINDOWS = {
    "morning": (time(9, 0), time(12, 0)),
    "afternoon": (time(13, 0), time(17, 0)),
    "evening": (time(17, 0), time(20, 0)),
    "lunch": (time(12, 0), time(14, 0)),
}

ISO_DATETIME_RE = re.compile(
    r"\b(?P<date>\d{4}-\d{2}-\d{2})(?:\s+(?:at\s+)?)?"
    r"(?P<start>\d{1,2}(?::\d{2})?\s*(?:am|pm)?)"
    r"(?:\s*(?:-|to|until)\s*(?P<end>\d{1,2}(?::\d{2})?\s*(?:am|pm)?))?",
    re.IGNORECASE,
)
RELATIVE_TIME_RE = re.compile(
    r"\b(?P<ref>today|tomorrow|next\s+(?:monday|mon|tuesday|tue|wednesday|wed|thursday|thu|friday|fri|saturday|sat|sunday|sun)|monday|mon|tuesday|tue|wednesday|wed|thursday|thu|friday|fri|saturday|sat|sunday|sun)"
    r"(?:\s+(?:at|around|from|after)\s+)"
    r"(?P<start>\d{1,2}(?::\d{2})?\s*(?:am|pm)?)"
    r"(?:\s*(?:-|to|until)\s*(?P<end>\d{1,2}(?::\d{2})?\s*(?:am|pm)?))?",
    re.IGNORECASE,
)
RELATIVE_PERIOD_RE = re.compile(
    r"\b(?P<ref>today|tomorrow|next\s+(?:monday|mon|tuesday|tue|wednesday|wed|thursday|thu|friday|fri|saturday|sat|sunday|sun)|monday|mon|tuesday|tue|wednesday|wed|thursday|thu|friday|fri|saturday|sat|sunday|sun)\s+"
    r"(?P<period>morning|afternoon|evening|lunch)\b",
    re.IGNORECASE,
)
REPLY_PREFIX_RE = re.compile(r"^(re|fw|fwd):\s*", re.IGNORECASE)

CRITICAL_PRIORITY_WORDS = {"urgent", "asap", "immediately", "critical", "board", "investor"}
HIGH_PRIORITY_WORDS = {"deadline", "launch", "contract", "interview", "today", "tomorrow"}
LOW_PRIORITY_WORDS = {"sometime", "whenever", "not urgent", "next month", "low priority"}
ONLINE_WORDS = {"zoom", "google meet", "meet link", "virtual", "video", "call", "online"}
OFFLINE_WORDS = {"lunch", "coffee", "office", "restaurant", "cafe", "in person", "meet up"}
ACCEPT_WORDS = {
    "works for me",
    "sounds good",
    "confirmed",
    "let's do it",
    "see you then",
    "that works",
}
CANCEL_WORDS = {"cancel", "call it off", "won't make it", "cannot make it"}
RESCHEDULE_WORDS = {"reschedule", "move it", "different time", "another time", "push it"}


class SchedulingAnalyzer:
    def analyze_new_request(
        self,
        subject: str,
        body: str,
        reference_at: datetime,
        timezone_name: str,
        default_duration_minutes: int,
    ) -> AnalysisResult:
        return self._analyze(
            subject=subject,
            body=body,
            reference_at=reference_at,
            timezone_name=timezone_name,
            default_duration_minutes=default_duration_minutes,
            forced_intent=MessageIntent.NEW_REQUEST,
        )

    def analyze_reply(
        self,
        subject: str,
        body: str,
        reference_at: datetime,
        timezone_name: str,
        default_duration_minutes: int,
    ) -> AnalysisResult:
        return self._analyze(
            subject=subject,
            body=body,
            reference_at=reference_at,
            timezone_name=timezone_name,
            default_duration_minutes=default_duration_minutes,
            forced_intent=None,
        )

    def _analyze(
        self,
        subject: str,
        body: str,
        reference_at: datetime,
        timezone_name: str,
        default_duration_minutes: int,
        forced_intent: Optional[MessageIntent],
    ) -> AnalysisResult:
        clean_subject = normalize_subject(subject)
        clean_body = collapse_whitespace(body)
        lowered = clean_body.lower()
        duration = infer_duration_minutes(lowered, default_duration_minutes)

        hints = extract_availability_hints(
            text=clean_body,
            reference_at=reference_at,
            timezone_name=timezone_name,
            duration_minutes=duration,
        )
        intent = forced_intent or infer_intent(lowered, hints)
        priority = infer_priority(lowered)
        mode = infer_mode(lowered)
        summary = first_sentence(clean_body) or clean_subject
        objective = clean_subject or summary
        agenda = build_agenda(objective, summary)
        requested_location = extract_requested_location(clean_body, mode)

        return AnalysisResult(
            intent=intent,
            priority=priority,
            mode=mode,
            duration_minutes=duration,
            objective=objective,
            summary=summary,
            agenda=agenda,
            requested_location=requested_location,
            availability_hints=hints,
        )


def normalize_subject(subject: str) -> str:
    current = subject.strip()
    while True:
        updated = REPLY_PREFIX_RE.sub("", current).strip()
        if updated == current:
            return updated
        current = updated


def collapse_whitespace(text: str) -> str:
    return " ".join((text or "").replace("\r", " ").replace("\n", " ").split())


def first_sentence(text: str) -> str:
    cleaned = collapse_whitespace(text)
    if not cleaned:
        return ""
    for delimiter in [".", "!", "?"]:
        if delimiter in cleaned:
            head = cleaned.split(delimiter, 1)[0].strip()
            if head:
                return head
    return cleaned[:160].strip()


def build_agenda(objective: str, summary: str) -> str:
    if not objective:
        return summary[:220]
    if summary.lower().startswith(objective.lower()):
        return summary[:220]
    return ("%s. %s" % (objective, summary)).strip()[:220]


def infer_intent(lowered_text: str, hints: List[AvailabilityHint]) -> MessageIntent:
    if any(word in lowered_text for word in CANCEL_WORDS):
        return MessageIntent.CANCEL
    if any(word in lowered_text for word in RESCHEDULE_WORDS):
        return MessageIntent.RESCHEDULE
    if any(word in lowered_text for word in ACCEPT_WORDS):
        return MessageIntent.ACCEPT
    if hints:
        return MessageIntent.PROPOSE_TIME
    if "meet" in lowered_text or "schedule" in lowered_text or "available" in lowered_text:
        return MessageIntent.NEW_REQUEST
    return MessageIntent.GENERAL


def infer_priority(lowered_text: str) -> MeetingPriority:
    if any(word in lowered_text for word in CRITICAL_PRIORITY_WORDS):
        return MeetingPriority.CRITICAL
    if any(word in lowered_text for word in HIGH_PRIORITY_WORDS):
        return MeetingPriority.HIGH
    if any(word in lowered_text for word in LOW_PRIORITY_WORDS):
        return MeetingPriority.LOW
    return MeetingPriority.NORMAL


def infer_mode(lowered_text: str) -> MeetingMode:
    online_score = sum(1 for word in ONLINE_WORDS if word in lowered_text)
    offline_score = sum(1 for word in OFFLINE_WORDS if word in lowered_text)
    if online_score > offline_score:
        return MeetingMode.ONLINE
    if offline_score > online_score:
        return MeetingMode.OFFLINE
    return MeetingMode.FLEXIBLE


def infer_duration_minutes(lowered_text: str, default_duration_minutes: int) -> int:
    duration_match = re.search(
        r"\b(?P<value>\d{1,3})\s*(?P<unit>minutes?|mins?|hours?|hrs?)\b",
        lowered_text,
    )
    if not duration_match:
        return default_duration_minutes
    amount = int(duration_match.group("value"))
    unit = duration_match.group("unit")
    if unit.startswith("hour") or unit.startswith("hr"):
        return max(15, amount * 60)
    return max(15, amount)


def extract_requested_location(text: str, mode: MeetingMode) -> Optional[str]:
    if mode == MeetingMode.ONLINE:
        return None
    location_match = re.search(
        r"\b(?:at|in|near)\s+([A-Za-z0-9][A-Za-z0-9 .,'&-]{2,80})",
        text,
        re.IGNORECASE,
    )
    if not location_match:
        return None
    return location_match.group(1).strip(" .,")


def extract_availability_hints(
    text: str,
    reference_at: datetime,
    timezone_name: str,
    duration_minutes: int,
) -> List[AvailabilityHint]:
    zone = ZoneInfo(timezone_name)
    local_reference = (
        reference_at.astimezone(zone) if reference_at.tzinfo else reference_at.replace(tzinfo=zone)
    )
    hints: List[AvailabilityHint] = []

    for match in ISO_DATETIME_RE.finditer(text):
        day = date.fromisoformat(match.group("date"))
        start_token = match.group("start")
        end_token = match.group("end")
        inferred_meridiem = extract_meridiem(end_token)
        start_time, start_meridiem = parse_time_token(
            start_token,
            inherited_meridiem=inferred_meridiem if extract_meridiem(start_token) is None else None,
        )
        end_time, _ = (
            parse_time_token(end_token, start_meridiem) if end_token else (None, start_meridiem)
        )
        start_dt = datetime.combine(day, start_time, tzinfo=zone)
        end_dt = (
            datetime.combine(day, end_time, tzinfo=zone)
            if end_time
            else start_dt + timedelta(minutes=duration_minutes)
        )
        hints.append(
            AvailabilityHint(
                start=start_dt,
                end=end_dt,
                exact=True,
                source_text=match.group(0),
            )
        )

    for match in RELATIVE_TIME_RE.finditer(text):
        day = resolve_reference_day(match.group("ref"), local_reference.date())
        if day is None:
            continue
        start_token = match.group("start")
        end_token = match.group("end")
        inferred_meridiem = extract_meridiem(end_token)
        start_time, start_meridiem = parse_time_token(
            start_token,
            inherited_meridiem=inferred_meridiem if extract_meridiem(start_token) is None else None,
        )
        end_time, _ = (
            parse_time_token(end_token, start_meridiem) if end_token else (None, start_meridiem)
        )
        start_dt = datetime.combine(day, start_time, tzinfo=zone)
        end_dt = (
            datetime.combine(day, end_time, tzinfo=zone)
            if end_time
            else start_dt + timedelta(minutes=duration_minutes)
        )
        hints.append(
            AvailabilityHint(
                start=start_dt,
                end=end_dt,
                exact=True,
                source_text=match.group(0),
            )
        )

    for match in RELATIVE_PERIOD_RE.finditer(text):
        day = resolve_reference_day(match.group("ref"), local_reference.date())
        if day is None:
            continue
        start_time, end_time = PERIOD_WINDOWS[match.group("period").lower()]
        hints.append(
            AvailabilityHint(
                start=datetime.combine(day, start_time, tzinfo=zone),
                end=datetime.combine(day, end_time, tzinfo=zone),
                exact=False,
                source_text=match.group(0),
            )
        )

    return dedupe_hints(hints)


def parse_time_token(
    token: Optional[str],
    inherited_meridiem: Optional[str] = None,
) -> Tuple[time, Optional[str]]:
    if not token:
        return time(9, 0), inherited_meridiem
    cleaned = token.strip().lower().replace(".", ":")
    match = re.match(r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<meridiem>am|pm)?", cleaned)
    if not match:
        return time(9, 0), inherited_meridiem

    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    meridiem = match.group("meridiem") or inherited_meridiem

    if meridiem == "pm" and hour != 12:
        hour += 12
    if meridiem == "am" and hour == 12:
        hour = 0

    if meridiem is None and hour < 8 and inherited_meridiem == "pm":
        hour += 12

    return time(hour % 24, minute), meridiem


def extract_meridiem(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    match = re.search(r"\b(am|pm)\b", token.strip().lower())
    return match.group(1) if match else None


def resolve_reference_day(reference: str, today: date) -> Optional[date]:
    current = reference.strip().lower()
    if current == "today":
        return today
    if current == "tomorrow":
        return today + timedelta(days=1)
    if current.startswith("next "):
        target = current.split(" ", 1)[1]
        if target not in WEEKDAY_INDEX:
            return None
        return next_weekday(today, WEEKDAY_INDEX[target], force_next_week=True)
    if current in WEEKDAY_INDEX:
        return next_weekday(today, WEEKDAY_INDEX[current], force_next_week=False)
    return None


def next_weekday(current_day: date, target_weekday: int, force_next_week: bool) -> date:
    days_ahead = (target_weekday - current_day.weekday()) % 7
    if days_ahead == 0 and force_next_week:
        days_ahead = 7
    return current_day + timedelta(days=days_ahead)


def dedupe_hints(hints: List[AvailabilityHint]) -> List[AvailabilityHint]:
    seen: Dict[Tuple[str, str], AvailabilityHint] = {}
    for hint in hints:
        key = (hint.start.isoformat(), hint.end.isoformat())
        seen[key] = hint
    return sorted(seen.values(), key=lambda hint: hint.start)
