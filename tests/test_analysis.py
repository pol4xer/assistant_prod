from datetime import datetime
from zoneinfo import ZoneInfo

from app.main import create_app
from app.models import MeetingMode, MeetingPriority, MessageIntent
from app.services.analysis import SchedulingAnalyzer


def test_analyzer_extracts_priority_mode_duration_and_hints():
    analyzer = SchedulingAnalyzer()
    result = analyzer.analyze_new_request(
        subject="Board prep review",
        body=(
            "Can we meet tomorrow afternoon in person for 45 minutes to review the investor deck? "
            "This is urgent."
        ),
        reference_at=datetime(2026, 4, 9, 10, 0, tzinfo=ZoneInfo("Europe/Istanbul")),
        timezone_name="Europe/Istanbul",
        default_duration_minutes=30,
    )

    assert result.priority == MeetingPriority.CRITICAL
    assert result.mode == MeetingMode.OFFLINE
    assert result.duration_minutes == 45
    assert result.availability_hints
    assert result.availability_hints[0].start.date().isoformat() == "2026-04-10"


def test_analyzer_uses_detected_duration_for_exact_time_hints():
    analyzer = SchedulingAnalyzer()
    result = analyzer.analyze_new_request(
        subject="Quick check-in",
        body="Please schedule a 30 minute online meeting tomorrow at 12:30 pm.",
        reference_at=datetime(2026, 4, 9, 10, 0, tzinfo=ZoneInfo("Europe/Istanbul")),
        timezone_name="Europe/Istanbul",
        default_duration_minutes=45,
    )

    assert result.duration_minutes == 30
    assert result.availability_hints[0].start.isoformat() == "2026-04-10T12:30:00+03:00"
    assert result.availability_hints[0].end.isoformat() == "2026-04-10T13:00:00+03:00"


def test_service_analyze_message_uses_openai_overlay(tmp_path, monkeypatch):
    app = create_app(db_path=str(tmp_path / "assistant.db"))
    service = app.state.service

    monkeypatch.setattr(
        service,
        "_analyze_with_openai",
        lambda **kwargs: {
            "priority": "critical",
            "mode": "online",
            "duration_minutes": 30,
            "summary": "Board meeting request.",
            "agenda": "Board meeting request.",
            "availability_hints": [
                {
                    "start": "2026-04-10T14:00:00+03:00",
                    "end": "2026-04-10T14:30:00+03:00",
                    "exact": True,
                    "source_text": "tomorrow at 2pm",
                }
            ],
        },
    )

    result = service._analyze_message(
        subject="Board review",
        body="Can we do a quick board review tomorrow at 2pm?",
        reference_at=datetime(2026, 4, 9, 10, 0, tzinfo=ZoneInfo("Europe/Istanbul")),
        timezone_name="Europe/Istanbul",
        default_duration_minutes=45,
        forced_intent=MessageIntent.NEW_REQUEST,
    )

    assert result.priority == MeetingPriority.CRITICAL
    assert result.mode == MeetingMode.ONLINE
    assert result.duration_minutes == 30
    assert result.summary == "Board meeting request."
    assert result.availability_hints[0].start.isoformat() == "2026-04-10T14:00:00+03:00"
