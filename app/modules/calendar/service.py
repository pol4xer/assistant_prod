from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from googleapiclient.errors import HttpError

from app.models import MeetingMode, RequestStatus, isoformat_minute, parse_iso_datetime
from app.modules.shared.constants import GOOGLE_CALENDAR_PROVIDER, GOOGLE_CALENDAR_SOURCE

logger = logging.getLogger(__name__)


class GoogleCalendarService:
    """Owns Google Calendar sync, booking, repair, and cancellation mechanics."""

    def __init__(self, assistant: Any):
        self.assistant = assistant

    @property
    def db(self):
        return self.assistant.db

    @property
    def settings(self):
        return self.assistant.settings

    def connect_google_calendar(self) -> Dict[str, Any]:
        self.assistant.require_live_integrations()
        now = self.assistant._now()
        bundle = self.assistant._load_provider_bundle("google_workspace")
        if not bundle.get("tokens"):
            raise ValueError("Google account is not connected yet. Use Login with Google first.")

        previous_calendar_id = bundle.get("selected_calendar_id")
        previous_calendar_summary = bundle.get("selected_calendar_summary")
        previous_last_sync_at = bundle.get("last_calendar_sync_at")
        sync_bundle = dict(bundle)
        sync_bundle["selected_calendar_id"] = "primary"
        sync_bundle["selected_calendar_summary"] = (
            bundle.get("connected_email") or "Primary Google Calendar"
        )
        try:
            sync_result = self.assistant._sync_google_calendar(
                sync_bundle, force=True, reference_time=now
            )
            repair_result = self.assistant._repair_missing_google_calendar_events(
                reference_time=now
            )
            sync_result["repair_attempted"] = repair_result["attempted"]
            sync_result["repaired_events"] = repair_result["created"]
            bundle["selected_calendar_id"] = sync_bundle.get("selected_calendar_id")
            bundle["selected_calendar_summary"] = sync_bundle.get("selected_calendar_summary")
            bundle["last_calendar_sync_at"] = sync_bundle.get("last_calendar_sync_at")
            bundle["warnings"] = self.assistant._dedupe_messages(sync_result.get("warnings", []))
            logger.info(
                "Google Calendar connected calendar_id=%s imported_busy_slots=%s repaired_events=%s",
                sync_result.get("calendar_id"),
                sync_result.get("imported_busy_slots"),
                sync_result.get("repaired_events"),
            )
        except Exception as exc:
            warning = self.assistant._describe_google_api_issue("Google Calendar API", exc)
            bundle["warnings"] = self.assistant._dedupe_messages(
                [*(bundle.get("warnings", []) or []), warning]
            )
            if previous_calendar_id:
                bundle["selected_calendar_id"] = previous_calendar_id
                bundle["selected_calendar_summary"] = previous_calendar_summary
                bundle["last_calendar_sync_at"] = previous_last_sync_at
            else:
                bundle.pop("selected_calendar_id", None)
                bundle.pop("selected_calendar_summary", None)
                bundle["last_calendar_sync_at"] = None
            sync_result = {
                "ok": False,
                "warnings": bundle["warnings"],
                "calendar_id": bundle.get("selected_calendar_id"),
                "calendar_summary": bundle.get("selected_calendar_summary"),
                "last_calendar_sync_at": bundle.get("last_calendar_sync_at"),
            }
        updated_at = isoformat_minute(now)
        self.assistant._save_provider_bundle("google_workspace", bundle, updated_at)
        snapshot = self.assistant._integration_snapshot("google_workspace")
        snapshot["calendar_sync"] = sync_result
        return snapshot

    def refresh_google_calendar_context(
        self,
        force: bool,
        reference_time: datetime,
    ) -> Optional[Dict[str, Any]]:
        if self.assistant.demo_mode:
            return None
        bundle = self.assistant._load_provider_bundle("google_workspace")
        if not bundle.get("account_connected") or not bundle.get("selected_calendar_id"):
            return None
        if not force and not self.assistant._google_calendar_sync_is_stale(bundle, reference_time):
            return None
        try:
            sync_result = self.assistant._sync_google_calendar(
                bundle, force=force, reference_time=reference_time
            )
        except Exception as exc:
            warning = self.assistant._describe_google_api_issue("Google Calendar sync", exc)
            bundle["warnings"] = self.assistant._dedupe_messages(
                [*(bundle.get("warnings", []) or []), warning]
            )
            self.assistant._save_provider_bundle(
                "google_workspace", bundle, isoformat_minute(reference_time)
            )
            return {"ok": False, "warnings": bundle["warnings"]}
        bundle["warnings"] = self.assistant._dedupe_messages(sync_result.get("warnings", []))
        self.assistant._save_provider_bundle(
            "google_workspace", bundle, isoformat_minute(reference_time)
        )
        return sync_result

    def google_calendar_sync_is_stale(
        self, bundle: Dict[str, Any], reference_time: datetime
    ) -> bool:
        last_sync = bundle.get("last_calendar_sync_at")
        if not last_sync:
            return True
        try:
            synced_at = parse_iso_datetime(str(last_sync))
        except Exception:
            return True
        ttl_minutes = int(getattr(self.settings, "google_calendar_sync_ttl_minutes", 5))
        return synced_at <= reference_time - timedelta(minutes=ttl_minutes)

    def sync_google_calendar(
        self,
        bundle: Dict[str, Any],
        force: bool,
        reference_time: datetime,
    ) -> Dict[str, Any]:
        self.assistant.require_live_integrations()
        calendar_id = str(bundle.get("selected_calendar_id") or "primary").strip() or "primary"
        calendar_label = (
            str(bundle.get("selected_calendar_summary") or "").strip() or "Primary Google Calendar"
        )
        service = self.assistant._google_calendar_service(bundle)
        events_call = service.events().list(
            calendarId=calendar_id,
            timeMin=reference_time.astimezone(ZoneInfo("UTC")).isoformat(),
            timeMax=(
                reference_time
                + timedelta(days=int(getattr(self.settings, "google_calendar_sync_days", 21)))
            )
            .astimezone(ZoneInfo("UTC"))
            .isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=250,
            showDeleted=False,
        )
        response = events_call.execute()
        busy_slot_rows: List[Dict[str, str]] = []
        for item in response.get("items", []):
            if item.get("status") == "cancelled":
                continue
            if item.get("transparency") == "transparent":
                continue
            starts_at, ends_at = self.assistant._google_event_window(
                item, reference_time.tzinfo or ZoneInfo("UTC")
            )
            if starts_at is None or ends_at is None or ends_at <= starts_at:
                continue
            busy_slot_rows.append(
                {
                    "title": str(item.get("summary") or "Busy in Google Calendar"),
                    "starts_at": isoformat_minute(starts_at),
                    "ends_at": isoformat_minute(ends_at),
                }
            )
        with self.assistant._state_lock:
            local_calendar_id = self.assistant._ensure_google_calendar_row(
                calendar_id, calendar_label, reference_time
            )
            self.db.replace_busy_slots_for_source(
                source=GOOGLE_CALENDAR_SOURCE,
                calendar_id=local_calendar_id,
                slots=busy_slot_rows,
            )
        imported = len(busy_slot_rows)
        bundle["selected_calendar_id"] = calendar_id
        bundle["selected_calendar_summary"] = calendar_label
        bundle["last_calendar_sync_at"] = isoformat_minute(reference_time)
        existing_warnings = [
            message
            for message in (bundle.get("warnings", []) or [])
            if "Google Calendar" not in message
        ]
        bundle["warnings"] = self.assistant._dedupe_messages(existing_warnings)
        return {
            "ok": True,
            "force": force,
            "calendar_id": calendar_id,
            "calendar_summary": calendar_label,
            "imported_busy_slots": imported,
            "warnings": bundle["warnings"],
            "last_calendar_sync_at": bundle["last_calendar_sync_at"],
        }

    def ensure_google_calendar_row(
        self, calendar_external_id: str, label: str, created_at: datetime
    ) -> int:
        existing = self.db.get_calendar_by_provider_external_id(
            GOOGLE_CALENDAR_PROVIDER, calendar_external_id
        )
        if existing is None:
            return self.db.add_calendar(
                name=label,
                provider=GOOGLE_CALENDAR_PROVIDER,
                external_id=calendar_external_id,
                is_primary=True,
                created_at=isoformat_minute(created_at),
            )
        updates: Dict[str, Any] = {}
        if existing["name"] != label:
            updates["name"] = label
        if int(existing["is_primary"] or 0) != 1:
            updates["is_primary"] = 1
        if updates:
            self.db.update_calendar(int(existing["id"]), updates)
        return int(existing["id"])

    def google_event_window(
        self,
        event: Dict[str, Any],
        default_tzinfo: Any,
    ) -> tuple[Optional[datetime], Optional[datetime]]:
        start = event.get("start", {})
        end = event.get("end", {})
        if start.get("dateTime") and end.get("dateTime"):
            return parse_iso_datetime(start["dateTime"]), parse_iso_datetime(end["dateTime"])
        if start.get("date") and end.get("date"):
            start_dt = datetime.fromisoformat(start["date"]).replace(tzinfo=default_tzinfo)
            end_dt = datetime.fromisoformat(end["date"]).replace(tzinfo=default_tzinfo)
            return start_dt, end_dt
        return None, None

    def create_google_calendar_event(
        self,
        request_row: Dict[str, Any],
        workspace: Dict[str, Any],
        option_row: Dict[str, Any],
        participant_emails: List[str],
        fallback_meeting_link: Optional[str],
    ) -> Optional[Dict[str, Optional[str]]]:
        if self.assistant.demo_mode:
            return None
        bundle = self.assistant._load_provider_bundle("google_workspace")
        calendar_id = str(bundle.get("selected_calendar_id") or "").strip()
        if not calendar_id or not bundle.get("tokens"):
            logger.info(
                "Google Calendar booking skipped request_id=%s reason=calendar_not_connected",
                request_row.get("id"),
            )
            return None
        try:
            service = self.assistant._google_calendar_service(bundle)
            wants_google_meet = (
                option_row["mode"] == MeetingMode.ONLINE.value
                and str(workspace["online_provider"]) == "google_meet"
            )
            logger.info(
                "Google Calendar booking start request_id=%s public_id=%s calendar_id=%s starts_at=%s ends_at=%s provider=%s attendees=%s",
                request_row.get("id"),
                request_row.get("public_id"),
                calendar_id,
                option_row.get("starts_at"),
                option_row.get("ends_at"),
                workspace.get("online_provider"),
                len([email for email in [workspace["owner_email"], *participant_emails] if email]),
            )
            event_body = self.assistant._build_google_event_body(
                request_row=request_row,
                workspace=workspace,
                option_row=option_row,
                participant_emails=participant_emails,
                fallback_meeting_link=fallback_meeting_link,
                include_conference_data=wants_google_meet,
            )
            insert_kwargs: Dict[str, Any] = {
                "calendarId": calendar_id,
                "body": event_body,
                "sendUpdates": "all",
            }
            if event_body.get("conferenceData"):
                insert_kwargs["conferenceDataVersion"] = 1
            try:
                event = service.events().insert(**insert_kwargs).execute()
            except HttpError as exc:
                if wants_google_meet and self.assistant._should_retry_google_booking_without_meet(
                    exc
                ):
                    logger.warning(
                        "Google Calendar booking retry_without_meet request_id=%s calendar_id=%s reason=%s",
                        request_row.get("id"),
                        calendar_id,
                        self.assistant._describe_google_api_issue("Google Calendar booking", exc),
                    )
                    retry_body = self.assistant._build_google_event_body(
                        request_row=request_row,
                        workspace=workspace,
                        option_row=option_row,
                        participant_emails=participant_emails,
                        fallback_meeting_link=fallback_meeting_link,
                        include_conference_data=False,
                    )
                    event = (
                        service.events()
                        .insert(
                            calendarId=calendar_id,
                            body=retry_body,
                            sendUpdates="all",
                        )
                        .execute()
                    )
                else:
                    raise
            google_meet_link = event.get("hangoutLink") or self.assistant._google_conference_link(
                event
            )
            meet_link = google_meet_link or (None if wants_google_meet else fallback_meeting_link)
            if google_meet_link:
                event = self.assistant._sync_google_event_link_metadata(
                    service=service,
                    calendar_id=calendar_id,
                    event=event,
                    meeting_link=google_meet_link,
                    workspace=workspace,
                )
                meet_link = (
                    event.get("hangoutLink")
                    or self.assistant._google_conference_link(event)
                    or google_meet_link
                )
            self.assistant._clear_google_warnings(
                [
                    "Google Calendar API",
                    "Google Calendar sync",
                    "Google Calendar booking",
                ]
            )
            logger.info(
                "Google Calendar booking success request_id=%s calendar_id=%s event_id=%s meet_link=%s",
                request_row.get("id"),
                calendar_id,
                event.get("id"),
                meet_link or "",
            )
            return {
                "external_calendar_id": calendar_id,
                "external_event_id": event.get("id"),
                "meeting_link": meet_link,
            }
        except Exception as exc:
            warning = self.assistant._describe_google_api_issue("Google Calendar booking", exc)
            logger.exception(
                "Google Calendar booking failed request_id=%s calendar_id=%s starts_at=%s ends_at=%s reason=%s",
                request_row.get("id"),
                calendar_id or "not-selected",
                option_row.get("starts_at"),
                option_row.get("ends_at"),
                warning,
            )
            self.assistant._append_google_warning(warning)
            return None

    def build_google_event_body(
        self,
        request_row: Dict[str, Any],
        workspace: Dict[str, Any],
        option_row: Dict[str, Any],
        participant_emails: List[str],
        fallback_meeting_link: Optional[str],
        include_conference_data: bool,
    ) -> Dict[str, Any]:
        event_body: Dict[str, Any] = {
            "summary": request_row["subject"],
            "description": request_row.get("agenda")
            or request_row.get("summary")
            or request_row["subject"],
            "start": {
                "dateTime": self.assistant._google_event_datetime(str(option_row["starts_at"])),
                "timeZone": workspace["timezone"],
            },
            "end": {
                "dateTime": self.assistant._google_event_datetime(str(option_row["ends_at"])),
                "timeZone": workspace["timezone"],
            },
            "attendees": [
                {"email": email}
                for email in [workspace["owner_email"], *participant_emails]
                if email
            ],
        }
        if option_row["mode"] == MeetingMode.OFFLINE.value:
            event_body["location"] = option_row.get("location_address") or option_row.get(
                "location_label"
            )
        elif fallback_meeting_link:
            event_body["description"] = (
                f"{event_body['description']}\n\nMeeting link: {fallback_meeting_link}"
            )
        if include_conference_data:
            event_body["conferenceData"] = {
                "createRequest": {
                    "requestId": uuid.uuid4().hex[:16],
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            }
        return event_body

    def should_retry_google_booking_without_meet(self, exc: HttpError) -> bool:
        status = getattr(getattr(exc, "resp", None), "status", None)
        if status != 400:
            return False
        return True

    def google_event_datetime(self, value: str) -> str:
        return parse_iso_datetime(value).isoformat(timespec="seconds")

    def sync_google_event_link_metadata(
        self,
        service: Any,
        calendar_id: str,
        event: Dict[str, Any],
        meeting_link: str,
        workspace: Dict[str, Any],
    ) -> Dict[str, Any]:
        self.assistant.require_live_integrations()
        event_id = str(event.get("id") or "").strip()
        if not event_id or not meeting_link:
            return event
        existing_description = str(event.get("description") or "").strip()
        link_label = (
            "Google Meet"
            if str(workspace.get("online_provider")) == "google_meet"
            else "Meeting link"
        )
        link_line = f"{link_label}: {meeting_link}"
        patch_body: Dict[str, Any] = {}
        if link_line not in existing_description:
            patch_body["description"] = (
                f"{existing_description}\n\n{link_line}" if existing_description else link_line
            )
        existing_location = str(event.get("location") or "").strip()
        if existing_location != meeting_link:
            patch_body["location"] = meeting_link
        if not patch_body:
            return event
        try:
            patched = (
                service.events()
                .patch(
                    calendarId=calendar_id,
                    eventId=event_id,
                    body=patch_body,
                    sendUpdates="none",
                )
                .execute()
            )
            logger.info(
                "Google Calendar event metadata patched event_id=%s calendar_id=%s",
                event_id,
                calendar_id,
            )
            if isinstance(patched, dict):
                return patched
        except Exception as exc:
            logger.warning(
                "Google Calendar event metadata patch skipped event_id=%s calendar_id=%s reason=%s",
                event_id,
                calendar_id,
                self.assistant._describe_google_api_issue(
                    "Google Calendar event metadata patch", exc
                ),
            )
            return event
        return event

    def repair_missing_google_calendar_events(self, reference_time: datetime) -> Dict[str, int]:
        if self.assistant.demo_mode:
            return {"attempted": 0, "created": 0}
        workspace = self.assistant.require_workspace()
        attempted = 0
        created = 0
        for meeting in self.db.rows_to_dicts(self.db.list_meetings()):
            if str(meeting.get("external_event_id") or "").strip():
                continue
            request_id = int(meeting["request_id"])
            request_row = self.db.row_to_dict(self.db.get_request(request_id))
            if request_row is None or request_row.get("status") != RequestStatus.CONFIRMED.value:
                continue
            option_row = self.assistant._meeting_option_for_repair(request_row, meeting)
            if option_row is None:
                continue
            attempted += 1
            participant_emails = [
                item["email"]
                for item in self.db.rows_to_dicts(self.db.list_participants(request_id))
            ]
            google_event = self.assistant._create_google_calendar_event(
                request_row=request_row,
                workspace=workspace,
                option_row=option_row,
                participant_emails=participant_emails,
                fallback_meeting_link=meeting.get("meeting_link"),
            )
            if not google_event or not google_event.get("external_event_id"):
                continue
            self.db.create_meeting(
                {
                    "request_id": request_id,
                    "title": meeting["title"],
                    "agenda": meeting.get("agenda"),
                    "starts_at": meeting["starts_at"],
                    "ends_at": meeting["ends_at"],
                    "mode": meeting["mode"],
                    "location_label": meeting.get("location_label"),
                    "location_address": meeting.get("location_address"),
                    "meeting_link": google_event.get("meeting_link") or meeting.get("meeting_link"),
                    "external_calendar_id": google_event.get("external_calendar_id"),
                    "external_event_id": google_event.get("external_event_id"),
                    "confirmation_status": meeting.get("confirmation_status") or "pending",
                    "created_at": meeting["created_at"],
                    "updated_at": isoformat_minute(reference_time),
                }
            )
            if google_event.get("meeting_link"):
                self.db.update_request(
                    request_id,
                    {
                        "meeting_link": google_event["meeting_link"],
                        "updated_at": isoformat_minute(reference_time),
                    },
                )
            created += 1
            logger.info(
                "Google Calendar booking repaired request_id=%s event_id=%s",
                request_id,
                google_event.get("external_event_id"),
            )
        if attempted:
            logger.info(
                "Google Calendar repair finished attempted=%s created=%s",
                attempted,
                created,
            )
        return {"attempted": attempted, "created": created}

    def meeting_option_for_repair(
        self,
        request_row: Dict[str, Any],
        meeting: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        selected_option_id = int(request_row.get("selected_option_id") or 0)
        if selected_option_id:
            for option in self.db.rows_to_dicts(self.db.list_options(int(request_row["id"]))):
                if int(option["id"]) == selected_option_id:
                    return option
        if not meeting.get("starts_at") or not meeting.get("ends_at"):
            return None
        return {
            "starts_at": meeting["starts_at"],
            "ends_at": meeting["ends_at"],
            "mode": meeting["mode"],
            "location_label": meeting.get("location_label"),
            "location_address": meeting.get("location_address"),
            "meeting_link": meeting.get("meeting_link"),
        }

    def remove_google_calendar_event(self, meeting: Optional[Dict[str, Any]]) -> bool:
        if self.assistant.demo_mode:
            return False
        if not meeting:
            return False
        event_id = str(meeting.get("external_event_id") or "").strip()
        calendar_id = str(meeting.get("external_calendar_id") or "").strip()
        if not event_id or not calendar_id:
            return False
        bundle = self.assistant._load_provider_bundle("google_workspace")
        if not bundle.get("tokens"):
            return False
        try:
            service = self.assistant._google_calendar_service(bundle)
            service.events().delete(
                calendarId=calendar_id, eventId=event_id, sendUpdates="all"
            ).execute()
            logger.info(
                "Google Calendar event removed calendar_id=%s event_id=%s request_id=%s",
                calendar_id,
                event_id,
                meeting.get("request_id"),
            )
            return True
        except Exception as exc:
            self.assistant._append_google_warning(
                self.assistant._describe_google_api_issue("Google Calendar booking", exc)
            )
            return False

    def google_conference_link(self, event: Dict[str, Any]) -> Optional[str]:
        conference = event.get("conferenceData") or {}
        for entry in conference.get("entryPoints", []):
            if entry.get("entryPointType") == "video" and entry.get("uri"):
                return str(entry["uri"])
        return None
