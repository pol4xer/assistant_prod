from datetime import datetime
from zoneinfo import ZoneInfo

from app.services.analysis import SchedulingAnalyzer
from app.services.scheduler import Scheduler


def test_scheduler_avoids_busy_window_when_proposing_slots():
    analyzer = SchedulingAnalyzer()
    scheduler = Scheduler()
    now = datetime(2026, 4, 9, 9, 0, tzinfo=ZoneInfo("Europe/Istanbul"))
    analysis = analyzer.analyze_new_request(
        subject="Team sync",
        body="Could we meet tomorrow afternoon for 45 minutes online?",
        reference_at=now,
        timezone_name="Europe/Istanbul",
        default_duration_minutes=45,
    )

    workspace = {
        "timezone": "Europe/Istanbul",
        "meeting_buffer_minutes": 15,
        "min_notice_hours": 1,
        "base_lat": None,
        "base_lng": None,
    }
    availability = [
        {"weekday": 4, "start_time": "09:00", "end_time": "18:00"},
    ]
    busy_slots = [
        {
            "starts_at": "2026-04-10T13:00+03:00",
            "ends_at": "2026-04-10T15:00+03:00",
        }
    ]

    options = scheduler.propose_options(
        workspace=workspace,
        availability_rules=availability,
        busy_slots=busy_slots,
        locations=[],
        analysis=analysis,
        now=now,
    )

    assert options
    assert all(option.starts_at.hour >= 15 for option in options)


def test_scheduler_does_not_generate_fake_google_meet_links():
    scheduler = Scheduler()

    assert scheduler.build_meeting_link("google_meet", "abc123") is None


def test_scheduler_matches_earliest_option_from_natural_language_acceptance():
    analyzer = SchedulingAnalyzer()
    scheduler = Scheduler()
    now = datetime(2026, 4, 9, 9, 0, tzinfo=ZoneInfo("Europe/Istanbul"))
    analysis = analyzer.analyze_reply(
        subject="Re: Team sync",
        body="The earliest time works for me!",
        reference_at=now,
        timezone_name="Europe/Istanbul",
        default_duration_minutes=30,
    )

    options = [
        {"starts_at": "2026-04-10T12:30+03:00", "ends_at": "2026-04-10T13:00+03:00"},
        {"starts_at": "2026-04-10T13:00+03:00", "ends_at": "2026-04-10T13:30+03:00"},
    ]

    matched = scheduler.match_existing_option("The earliest time works for me!", options, analysis)

    assert matched == options[0]
