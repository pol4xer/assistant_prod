from __future__ import annotations

import math
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence

from app.models import (
    AnalysisResult,
    CandidateOption,
    MeetingMode,
    MeetingPriority,
    combine_local,
    ensure_timezone,
    isoformat_minute,
    parse_clock,
    parse_iso_datetime,
)


class Scheduler:
    SEARCH_HORIZON_DAYS = {
        MeetingPriority.LOW: 5,
        MeetingPriority.NORMAL: 7,
        MeetingPriority.HIGH: 10,
        MeetingPriority.CRITICAL: 14,
    }
    FOLLOW_UP_HOURS = {
        MeetingPriority.LOW: 48,
        MeetingPriority.NORMAL: 24,
        MeetingPriority.HIGH: 8,
        MeetingPriority.CRITICAL: 4,
    }

    def propose_options(
        self,
        workspace: Dict[str, object],
        availability_rules: Sequence[Dict[str, object]],
        busy_slots: Sequence[Dict[str, object]],
        locations: Sequence[Dict[str, object]],
        analysis: AnalysisResult,
        now: datetime,
        requested_mode: Optional[MeetingMode] = None,
        limit: int = 3,
    ) -> List[CandidateOption]:
        timezone_name = str(workspace["timezone"])
        local_now = ensure_timezone(now, timezone_name)
        resolved_mode = self.resolve_mode(analysis, locations, requested_mode)

        options: List[CandidateOption] = []
        options.extend(
            self._options_from_hints(
                workspace=workspace,
                availability_rules=availability_rules,
                busy_slots=busy_slots,
                locations=locations,
                analysis=analysis,
                now=local_now,
                mode=resolved_mode,
            )
        )
        options.extend(
            self._options_from_calendar(
                workspace=workspace,
                availability_rules=availability_rules,
                busy_slots=busy_slots,
                locations=locations,
                analysis=analysis,
                now=local_now,
                mode=resolved_mode,
            )
        )

        unique: Dict[str, CandidateOption] = {}
        for option in sorted(options, key=lambda item: (-item.score, item.starts_at)):
            key = "%s|%s|%s" % (
                option.starts_at.isoformat(),
                option.ends_at.isoformat(),
                option.location_label or option.mode.value,
            )
            if key not in unique:
                unique[key] = option

        chosen = list(unique.values())
        chosen.sort(key=lambda item: (-item.score, item.starts_at))
        return chosen[:limit]

    def resolve_mode(
        self,
        analysis: AnalysisResult,
        locations: Sequence[Dict[str, object]],
        requested_mode: Optional[MeetingMode],
    ) -> MeetingMode:
        if requested_mode is not None:
            return requested_mode
        if analysis.mode != MeetingMode.FLEXIBLE:
            return analysis.mode
        if analysis.requested_location or locations:
            return MeetingMode.OFFLINE if analysis.requested_location else MeetingMode.ONLINE
        return MeetingMode.ONLINE

    def next_follow_up_at(self, priority: MeetingPriority, from_time: datetime) -> datetime:
        return from_time + timedelta(hours=self.FOLLOW_UP_HOURS[priority])

    def confirmation_due_at(self, start_time: datetime, lead_hours: int) -> datetime:
        return start_time - timedelta(hours=lead_hours)

    def build_meeting_link(self, provider: str, request_public_id: str) -> Optional[str]:
        # Meet links come from Google Calendar; Zoom creation is not implemented.
        # Inventing a URL would misrepresent an actual provider reservation.
        return None

    def match_existing_option(
        self,
        body: str,
        options: Sequence[Dict[str, object]],
        analysis: AnalysisResult,
    ) -> Optional[Dict[str, object]]:
        if not options:
            return None
        option_match = re.search(r"\boption\s+(\d+)\b", body, re.IGNORECASE)
        if option_match:
            index = int(option_match.group(1)) - 1
            if 0 <= index < len(options):
                return dict(options[index])

        lowered = body.lower()
        if any(
            phrase in lowered
            for phrase in ["earliest time", "earliest option", "first option", "first one"]
        ):
            return dict(options[0])
        if any(
            phrase in lowered
            for phrase in ["latest time", "latest option", "last option", "last one"]
        ):
            return dict(options[-1])

        for hint in analysis.availability_hints:
            for option in options:
                start = parse_iso_datetime(str(option["starts_at"]))
                delta = abs((start - hint.start).total_seconds())
                if delta <= 90 * 60:
                    return dict(option)

        if analysis.intent.value == "accept" and len(options) == 1:
            return dict(options[0])
        return None

    def _options_from_hints(
        self,
        workspace: Dict[str, object],
        availability_rules: Sequence[Dict[str, object]],
        busy_slots: Sequence[Dict[str, object]],
        locations: Sequence[Dict[str, object]],
        analysis: AnalysisResult,
        now: datetime,
        mode: MeetingMode,
    ) -> List[CandidateOption]:
        options: List[CandidateOption] = []
        for hint in analysis.availability_hints:
            if hint.end <= now:
                continue
            if hint.exact:
                built = self._build_option(
                    workspace=workspace,
                    availability_rules=availability_rules,
                    busy_slots=busy_slots,
                    locations=locations,
                    analysis=analysis,
                    start=hint.start,
                    end=hint.end,
                    mode=mode,
                    now=now,
                )
                if built:
                    options.append(built)
                continue

            cursor = max(hint.start, now)
            while cursor + timedelta(minutes=analysis.duration_minutes) <= hint.end:
                built = self._build_option(
                    workspace=workspace,
                    availability_rules=availability_rules,
                    busy_slots=busy_slots,
                    locations=locations,
                    analysis=analysis,
                    start=cursor,
                    end=cursor + timedelta(minutes=analysis.duration_minutes),
                    mode=mode,
                    now=now,
                )
                if built:
                    options.append(built)
                cursor += timedelta(minutes=30)
                if len(options) >= 6:
                    break
        return options

    def _options_from_calendar(
        self,
        workspace: Dict[str, object],
        availability_rules: Sequence[Dict[str, object]],
        busy_slots: Sequence[Dict[str, object]],
        locations: Sequence[Dict[str, object]],
        analysis: AnalysisResult,
        now: datetime,
        mode: MeetingMode,
    ) -> List[CandidateOption]:
        options: List[CandidateOption] = []
        horizon_days = self.SEARCH_HORIZON_DAYS[analysis.priority]
        for day_offset in range(horizon_days + 1):
            current_day = (now + timedelta(days=day_offset)).date()
            weekday_rules = [
                dict(rule)
                for rule in availability_rules
                if int(rule["weekday"]) == current_day.weekday()
            ]
            for rule in weekday_rules:
                window_start = combine_local(
                    current_day,
                    parse_clock(str(rule["start_time"])),
                    str(workspace["timezone"]),
                )
                window_end = combine_local(
                    current_day,
                    parse_clock(str(rule["end_time"])),
                    str(workspace["timezone"]),
                )
                cursor = max(window_start, now)
                while cursor + timedelta(minutes=analysis.duration_minutes) <= window_end:
                    built = self._build_option(
                        workspace=workspace,
                        availability_rules=availability_rules,
                        busy_slots=busy_slots,
                        locations=locations,
                        analysis=analysis,
                        start=cursor,
                        end=cursor + timedelta(minutes=analysis.duration_minutes),
                        mode=mode,
                        now=now,
                    )
                    if built:
                        options.append(built)
                    cursor += timedelta(minutes=30)
                    if len(options) >= 12:
                        return options
        return options

    def _build_option(
        self,
        workspace: Dict[str, object],
        availability_rules: Sequence[Dict[str, object]],
        busy_slots: Sequence[Dict[str, object]],
        locations: Sequence[Dict[str, object]],
        analysis: AnalysisResult,
        start: datetime,
        end: datetime,
        mode: MeetingMode,
        now: datetime,
    ) -> Optional[CandidateOption]:
        if end <= now:
            return None
        min_notice_deadline = now + timedelta(hours=int(workspace["min_notice_hours"]))
        if start < min_notice_deadline:
            return None

        location = None
        travel_minutes = 0
        if mode == MeetingMode.OFFLINE:
            ordered_locations = self._ordered_locations(analysis.requested_location, locations)
            if not ordered_locations:
                return None
            location = ordered_locations[0]
            travel_minutes = self._estimate_travel_minutes(workspace, location, start)

        buffer_minutes = int(workspace["meeting_buffer_minutes"])
        padded_start = start - timedelta(minutes=buffer_minutes + travel_minutes)
        padded_end = end + timedelta(minutes=buffer_minutes + travel_minutes)

        if not self._within_availability(availability_rules, workspace, padded_start, padded_end):
            return None
        if self._overlaps_busy_slot(busy_slots, padded_start, padded_end):
            return None

        score = self._score_candidate(
            analysis=analysis,
            start=start,
            now=now,
            travel_minutes=travel_minutes,
            location=location,
        )
        rationale = "Fits current availability"
        if analysis.availability_hints:
            rationale = "Aligned with a requested window"
        if mode == MeetingMode.OFFLINE and location:
            rationale = "%s; %s min travel estimate" % (rationale, travel_minutes)

        return CandidateOption(
            starts_at=start,
            ends_at=end,
            mode=mode,
            location_label=str(location["label"]) if location else None,
            location_address=str(location["address"]) if location else None,
            travel_minutes=travel_minutes,
            score=score,
            rationale=rationale,
        )

    def _within_availability(
        self,
        availability_rules: Sequence[Dict[str, object]],
        workspace: Dict[str, object],
        start: datetime,
        end: datetime,
    ) -> bool:
        timezone_name = str(workspace["timezone"])
        local_start = ensure_timezone(start, timezone_name)
        local_end = ensure_timezone(end, timezone_name)
        if local_start.date() != local_end.date():
            return False
        for rule in availability_rules:
            if int(rule["weekday"]) != local_start.weekday():
                continue
            window_start = combine_local(
                local_start.date(),
                parse_clock(str(rule["start_time"])),
                timezone_name,
            )
            window_end = combine_local(
                local_start.date(),
                parse_clock(str(rule["end_time"])),
                timezone_name,
            )
            if local_start >= window_start and local_end <= window_end:
                return True
        return False

    def _overlaps_busy_slot(
        self,
        busy_slots: Sequence[Dict[str, object]],
        padded_start: datetime,
        padded_end: datetime,
    ) -> bool:
        for slot in busy_slots:
            busy_start = parse_iso_datetime(str(slot["starts_at"]))
            busy_end = parse_iso_datetime(str(slot["ends_at"]))
            if padded_start < busy_end and padded_end > busy_start:
                return True
        return False

    def _ordered_locations(
        self,
        requested_location: Optional[str],
        locations: Sequence[Dict[str, object]],
    ) -> List[Dict[str, object]]:
        ordered = [dict(location) for location in locations]
        if not requested_location:
            ordered.sort(key=lambda item: (-(int(item["is_default"])), int(item["priority"])))
            return ordered

        lowered = requested_location.lower()
        ordered.sort(
            key=lambda item: (
                0
                if lowered in str(item["label"]).lower() or lowered in str(item["address"]).lower()
                else 1,
                -(int(item["is_default"])),
                int(item["priority"]),
            )
        )
        return ordered

    def _estimate_travel_minutes(
        self,
        workspace: Dict[str, object],
        location: Dict[str, object],
        start: datetime,
    ) -> int:
        base_lat = workspace.get("base_lat")
        base_lng = workspace.get("base_lng")
        if (
            base_lat is None
            or base_lng is None
            or location.get("lat") is None
            or location.get("lng") is None
        ):
            return 20

        distance_km = haversine_km(
            float(base_lat),
            float(base_lng),
            float(location["lat"]),
            float(location["lng"]),
        )
        hour = start.hour
        traffic_factor = 1.0
        if hour in (7, 8, 9, 16, 17, 18, 19):
            traffic_factor = 1.35
        elif hour in (12, 13):
            traffic_factor = 1.15
        minutes = (distance_km / 32.0) * 60 * traffic_factor
        return max(10, int(round(minutes)))

    def _score_candidate(
        self,
        analysis: AnalysisResult,
        start: datetime,
        now: datetime,
        travel_minutes: int,
        location: Optional[Dict[str, object]],
    ) -> float:
        score = 100.0
        score -= max(0, (start.date() - now.date()).days) * 3.5
        score -= travel_minutes * 0.7
        if 10 <= start.hour <= 16:
            score += 6
        if location and int(location.get("is_default") or 0):
            score += 8

        for hint in analysis.availability_hints:
            if hint.start <= start <= hint.end:
                score += 12
            elif abs((start - hint.start).total_seconds()) <= 3600:
                score += 8
        return score


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return radius * c


def option_to_dict(option: CandidateOption, created_at: datetime) -> Dict[str, object]:
    return {
        "starts_at": isoformat_minute(option.starts_at),
        "ends_at": isoformat_minute(option.ends_at),
        "mode": option.mode.value,
        "location_label": option.location_label,
        "location_address": option.location_address,
        "meeting_link": option.meeting_link,
        "travel_minutes": option.travel_minutes,
        "score": option.score,
        "rationale": option.rationale,
        "status": "proposed",
        "created_at": isoformat_minute(created_at),
    }
