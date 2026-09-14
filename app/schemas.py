from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

from app.models import MeetingMode, MeetingPriority, parse_clock


class WorkspaceUpdateInput(BaseModel):
    assistant_name: str = "Marlow"
    assistant_email: str = "assistant@example.com"
    owner_name: str = "Alex Morgan"
    owner_email: str = "owner@example.com"
    timezone: str = "UTC"
    default_duration_minutes: int = Field(default=45, ge=15, le=240)
    meeting_buffer_minutes: int = Field(default=15, ge=0, le=120)
    min_notice_hours: int = Field(default=2, ge=0, le=168)
    confirmation_lead_hours: int = Field(default=24, ge=1, le=168)
    workday_start: str = "09:00"
    workday_end: str = "18:00"
    base_location_label: Optional[str] = None
    base_lat: Optional[float] = None
    base_lng: Optional[float] = None
    online_provider: str = "google_meet"

    @field_validator("assistant_email", "owner_email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("workday_start", "workday_end")
    @classmethod
    def validate_clock(cls, value: str) -> str:
        parse_clock(value)
        return value


class AvailabilityRuleInput(BaseModel):
    weekday: int = Field(ge=0, le=6)
    start_time: str
    end_time: str
    priority: str = "normal"

    @field_validator("start_time", "end_time")
    @classmethod
    def validate_clock(cls, value: str) -> str:
        parse_clock(value)
        return value


class CalendarInput(BaseModel):
    name: str
    provider: str = "manual"
    external_id: Optional[str] = None
    is_primary: bool = False


class BusySlotInput(BaseModel):
    title: str
    starts_at: datetime
    ends_at: datetime
    source: str = "manual"


class LocationInput(BaseModel):
    label: str
    address: str
    lat: Optional[float] = None
    lng: Optional[float] = None
    priority: int = Field(default=100, ge=1, le=999)
    is_default: bool = False


class ParticipantInput(BaseModel):
    email: str
    display_name: Optional[str] = None
    required: bool = True

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.strip().lower()


class CreateRequestInput(BaseModel):
    subject: str
    body: str
    participants: List[ParticipantInput]
    mode: Optional[MeetingMode] = None
    priority: Optional[MeetingPriority] = None
    requested_location: Optional[str] = None
    thread_key: Optional[str] = None


class IngestEmailInput(BaseModel):
    sender_email: str
    sender_name: Optional[str] = None
    to: List[str]
    cc: List[str] = Field(default_factory=list)
    subject: str
    body: str
    thread_key: Optional[str] = None
    sent_at: Optional[datetime] = None

    @field_validator("sender_email")
    @classmethod
    def normalize_sender_email(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("to", "cc")
    @classmethod
    def normalize_recipients(cls, values: List[str]) -> List[str]:
        return [value.strip().lower() for value in values if value.strip()]


class AutomationRunInput(BaseModel):
    now: Optional[datetime] = None


class IntegrationCredentialsInput(BaseModel):
    values: Dict[str, str] = Field(default_factory=dict)
