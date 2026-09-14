from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS workspace(
    id INTEGER PRIMARY KEY CHECK (id = 1),
    assistant_name TEXT NOT NULL,
    assistant_email TEXT NOT NULL,
    owner_name TEXT NOT NULL,
    owner_email TEXT NOT NULL,
    timezone TEXT NOT NULL,
    default_duration_minutes INTEGER NOT NULL,
    meeting_buffer_minutes INTEGER NOT NULL,
    min_notice_hours INTEGER NOT NULL,
    confirmation_lead_hours INTEGER NOT NULL,
    workday_start TEXT NOT NULL,
    workday_end TEXT NOT NULL,
    base_location_label TEXT,
    base_lat REAL,
    base_lng REAL,
    online_provider TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_credentials(
    provider TEXT PRIMARY KEY,
    credentials_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_states(
    state TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    redirect_to TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS availability_rules(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    weekday INTEGER NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT 'normal'
);

CREATE TABLE IF NOT EXISTS calendars(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    provider TEXT NOT NULL,
    external_id TEXT,
    is_primary INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS busy_slots(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    calendar_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    source TEXT NOT NULL,
    FOREIGN KEY(calendar_id) REFERENCES calendars(id)
);

CREATE TABLE IF NOT EXISTS location_preferences(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    address TEXT NOT NULL,
    lat REAL,
    lng REAL,
    priority INTEGER NOT NULL DEFAULT 100,
    is_default INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS contacts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    display_name TEXT,
    organization TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meeting_requests(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id TEXT UNIQUE NOT NULL,
    thread_key TEXT UNIQUE NOT NULL,
    direction TEXT NOT NULL,
    source TEXT NOT NULL,
    subject TEXT NOT NULL,
    objective TEXT,
    summary TEXT,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    priority TEXT NOT NULL,
    duration_minutes INTEGER NOT NULL,
    requested_location TEXT,
    organizer_name TEXT,
    organizer_email TEXT NOT NULL,
    scheduled_starts_at TEXT,
    scheduled_ends_at TEXT,
    location_label TEXT,
    location_address TEXT,
    meeting_link TEXT,
    selected_option_id INTEGER,
    next_follow_up_at TEXT,
    confirmation_due_at TEXT,
    confirmation_sent_at TEXT,
    agenda TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_inbound_at TEXT,
    last_outbound_at TEXT
);

CREATE TABLE IF NOT EXISTS meeting_options(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    mode TEXT NOT NULL,
    location_label TEXT,
    location_address TEXT,
    meeting_link TEXT,
    travel_minutes INTEGER NOT NULL DEFAULT 0,
    score REAL NOT NULL DEFAULT 0,
    rationale TEXT,
    status TEXT NOT NULL DEFAULT 'proposed',
    created_at TEXT NOT NULL,
    FOREIGN KEY(request_id) REFERENCES meeting_requests(id)
);

CREATE TABLE IF NOT EXISTS participants(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL,
    contact_id INTEGER,
    email TEXT NOT NULL,
    display_name TEXT,
    role TEXT NOT NULL DEFAULT 'attendee',
    required INTEGER NOT NULL DEFAULT 1,
    response_status TEXT NOT NULL DEFAULT 'pending',
    accepted_option_id INTEGER,
    notes TEXT,
    last_response_at TEXT,
    UNIQUE(request_id, email),
    FOREIGN KEY(request_id) REFERENCES meeting_requests(id),
    FOREIGN KEY(contact_id) REFERENCES contacts(id),
    FOREIGN KEY(accepted_option_id) REFERENCES meeting_options(id)
);

CREATE TABLE IF NOT EXISTS email_messages(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL,
    direction TEXT NOT NULL,
    sender_email TEXT NOT NULL,
    recipients_json TEXT NOT NULL,
    cc_json TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'local',
    external_message_id TEXT,
    external_thread_id TEXT,
    internet_message_id TEXT,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    intent TEXT,
    extracted_windows_json TEXT NOT NULL DEFAULT '[]',
    sent_at TEXT NOT NULL,
    FOREIGN KEY(request_id) REFERENCES meeting_requests(id)
);

CREATE TABLE IF NOT EXISTS ignored_inbox_messages(
    provider TEXT NOT NULL,
    external_message_id TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(provider, external_message_id)
);

CREATE TABLE IF NOT EXISTS ignored_inbox_threads(
    provider TEXT NOT NULL,
    external_thread_id TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(provider, external_thread_id)
);

CREATE TABLE IF NOT EXISTS meetings(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER UNIQUE NOT NULL,
    title TEXT NOT NULL,
    agenda TEXT,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    mode TEXT NOT NULL,
    location_label TEXT,
    location_address TEXT,
    meeting_link TEXT,
    external_calendar_id TEXT,
    external_event_id TEXT,
    confirmation_status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(request_id) REFERENCES meeting_requests(id)
);

CREATE INDEX IF NOT EXISTS idx_meeting_requests_status_updated_at
ON meeting_requests(status, updated_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_meeting_requests_follow_up_due
ON meeting_requests(status, unixepoch(next_follow_up_at))
WHERE next_follow_up_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_meeting_requests_confirmation_due
ON meeting_requests(status, unixepoch(confirmation_due_at))
WHERE confirmation_due_at IS NOT NULL AND confirmation_sent_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_participants_request_id
ON participants(request_id, id ASC);

CREATE INDEX IF NOT EXISTS idx_meeting_options_request_id_starts_at
ON meeting_options(request_id, starts_at ASC, id ASC);

CREATE INDEX IF NOT EXISTS idx_email_messages_request_id_sent_at
ON email_messages(request_id, sent_at ASC, id ASC);

CREATE INDEX IF NOT EXISTS idx_email_messages_provider_external_message_id
ON email_messages(provider, external_message_id)
WHERE external_message_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_email_messages_external_thread_id
ON email_messages(external_thread_id)
WHERE external_thread_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_busy_slots_source
ON busy_slots(source);

CREATE INDEX IF NOT EXISTS idx_busy_slots_calendar_starts_at
ON busy_slots(calendar_id, starts_at ASC, id ASC);
"""


class Database:
    def __init__(self, path: str):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def init(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            self._ensure_column(connection, "meetings", "external_calendar_id", "TEXT")
            self._ensure_column(connection, "meetings", "external_event_id", "TEXT")
            self._ensure_column(
                connection, "email_messages", "provider", "TEXT NOT NULL DEFAULT 'local'"
            )
            self._ensure_column(connection, "email_messages", "external_message_id", "TEXT")
            self._ensure_column(connection, "email_messages", "external_thread_id", "TEXT")
            self._ensure_column(connection, "email_messages", "internet_message_id", "TEXT")
            self._ensure_column(
                connection, "email_messages", "delivery_status", "TEXT NOT NULL DEFAULT 'local'"
            )
            self._ensure_column(connection, "email_messages", "delivery_error", "TEXT")

    def _ensure_column(
        self,
        connection: sqlite3.Connection,
        table_name: str,
        column_name: str,
        column_sql: str,
    ) -> None:
        columns = {
            row["name"] for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        if column_name in columns:
            return
        connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")
        connection.commit()

    def reset(self) -> None:
        tables = [
            "meetings",
            "ignored_inbox_threads",
            "ignored_inbox_messages",
            "email_messages",
            "participants",
            "meeting_options",
            "meeting_requests",
            "contacts",
            "location_preferences",
            "busy_slots",
            "calendars",
            "availability_rules",
            "oauth_states",
            "provider_credentials",
            "workspace",
        ]
        with self.connect() as connection:
            for table in tables:
                connection.execute("DROP TABLE IF EXISTS %s" % table)
            connection.executescript(SCHEMA)

    def _fetchone(self, query: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(query, tuple(params)).fetchone()

    def _fetchall(self, query: str, params: Iterable[Any] = ()) -> List[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(query, tuple(params)).fetchall()

    def _execute(self, query: str, params: Iterable[Any] = ()) -> int:
        with self.connect() as connection:
            cursor = connection.execute(query, tuple(params))
            connection.commit()
            return int(cursor.lastrowid or 0)

    def _execute_many(self, query: str, rows: Iterable[Iterable[Any]]) -> None:
        with self.connect() as connection:
            connection.executemany(query, list(rows))
            connection.commit()

    def ensure_workspace(self, defaults: Dict[str, Any]) -> None:
        current = self.get_workspace()
        if current:
            return
        self._execute(
            """
            INSERT INTO workspace(
                id, assistant_name, assistant_email, owner_name, owner_email, timezone,
                default_duration_minutes, meeting_buffer_minutes, min_notice_hours,
                confirmation_lead_hours, workday_start, workday_end, base_location_label,
                base_lat, base_lng, online_provider, created_at, updated_at
            ) VALUES(1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                defaults["assistant_name"],
                defaults["assistant_email"],
                defaults["owner_name"],
                defaults["owner_email"],
                defaults["timezone"],
                defaults["default_duration_minutes"],
                defaults["meeting_buffer_minutes"],
                defaults["min_notice_hours"],
                defaults["confirmation_lead_hours"],
                defaults["workday_start"],
                defaults["workday_end"],
                defaults.get("base_location_label"),
                defaults.get("base_lat"),
                defaults.get("base_lng"),
                defaults["online_provider"],
                defaults["created_at"],
                defaults["updated_at"],
            ),
        )

    def get_workspace(self) -> Optional[sqlite3.Row]:
        return self._fetchone("SELECT * FROM workspace WHERE id = 1")

    def upsert_provider_credentials(
        self,
        provider: str,
        credentials: Dict[str, Any],
        updated_at: str,
    ) -> None:
        self._execute(
            """
            INSERT INTO provider_credentials(provider, credentials_json, updated_at)
            VALUES(?,?,?)
            ON CONFLICT(provider) DO UPDATE SET
                credentials_json = excluded.credentials_json,
                updated_at = excluded.updated_at
            """,
            (provider, json.dumps(credentials, ensure_ascii=False), updated_at),
        )

    def get_provider_credentials(self, provider: str) -> Optional[sqlite3.Row]:
        return self._fetchone(
            "SELECT * FROM provider_credentials WHERE provider = ?",
            (provider,),
        )

    def list_provider_credentials(self) -> List[sqlite3.Row]:
        return self._fetchall("SELECT * FROM provider_credentials ORDER BY provider ASC")

    def delete_provider_credentials(self, provider: str) -> None:
        self._execute("DELETE FROM provider_credentials WHERE provider = ?", (provider,))

    def create_oauth_state(
        self,
        state: str,
        provider: str,
        created_at: str,
        redirect_to: Optional[str] = None,
    ) -> None:
        self._execute(
            """
            INSERT INTO oauth_states(state, provider, redirect_to, created_at)
            VALUES(?,?,?,?)
            """,
            (state, provider, redirect_to, created_at),
        )

    def consume_oauth_state(self, state: str, provider: str) -> Optional[sqlite3.Row]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM oauth_states
                WHERE state = ? AND provider = ?
                """,
                (state, provider),
            ).fetchone()
            connection.execute(
                "DELETE FROM oauth_states WHERE state = ? AND provider = ?",
                (state, provider),
            )
            connection.commit()
            return row

    def consume_oauth_state_any(self, state: str) -> Optional[sqlite3.Row]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM oauth_states
                WHERE state = ?
                """,
                (state,),
            ).fetchone()
            connection.execute("DELETE FROM oauth_states WHERE state = ?", (state,))
            connection.commit()
            return row

    def update_workspace(self, fields: Dict[str, Any]) -> None:
        if not fields:
            return
        assignments = ", ".join("%s = ?" % key for key in fields.keys())
        values = list(fields.values())
        values.append(1)
        self._execute("UPDATE workspace SET %s WHERE id = ?" % assignments, values)

    def replace_availability_rules(self, rules: List[Dict[str, Any]]) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM availability_rules")
            connection.executemany(
                """
                INSERT INTO availability_rules(weekday, start_time, end_time, priority)
                VALUES(?,?,?,?)
                """,
                [
                    (
                        rule["weekday"],
                        rule["start_time"],
                        rule["end_time"],
                        rule.get("priority", "normal"),
                    )
                    for rule in rules
                ],
            )
            connection.commit()

    def list_availability_rules(self) -> List[sqlite3.Row]:
        return self._fetchall(
            "SELECT * FROM availability_rules ORDER BY weekday ASC, start_time ASC, end_time ASC"
        )

    def add_calendar(
        self,
        name: str,
        provider: str,
        created_at: str,
        external_id: Optional[str] = None,
        is_primary: bool = False,
    ) -> int:
        return self._execute(
            """
            INSERT INTO calendars(name, provider, external_id, is_primary, created_at)
            VALUES(?,?,?,?,?)
            """,
            (name, provider, external_id, int(is_primary), created_at),
        )

    def list_calendars(self) -> List[sqlite3.Row]:
        return self._fetchall("SELECT * FROM calendars ORDER BY is_primary DESC, id ASC")

    def get_calendar_by_provider_external_id(
        self,
        provider: str,
        external_id: str,
    ) -> Optional[sqlite3.Row]:
        return self._fetchone(
            "SELECT * FROM calendars WHERE provider = ? AND external_id = ?",
            (provider, external_id),
        )

    def update_calendar(self, calendar_id: int, fields: Dict[str, Any]) -> None:
        if not fields:
            return
        assignments = ", ".join("%s = ?" % key for key in fields.keys())
        values = list(fields.values())
        values.append(calendar_id)
        self._execute("UPDATE calendars SET %s WHERE id = ?" % assignments, values)

    def add_busy_slot(
        self,
        calendar_id: int,
        title: str,
        starts_at: str,
        ends_at: str,
        source: str,
    ) -> int:
        return self._execute(
            """
            INSERT INTO busy_slots(calendar_id, title, starts_at, ends_at, source)
            VALUES(?,?,?,?,?)
            """,
            (calendar_id, title, starts_at, ends_at, source),
        )

    def list_busy_slots(self) -> List[sqlite3.Row]:
        return self._fetchall("SELECT * FROM busy_slots ORDER BY starts_at ASC")

    def delete_busy_slots_by_source(self, source: str) -> None:
        self._execute("DELETE FROM busy_slots WHERE source = ?", (source,))

    def replace_busy_slots_for_source(
        self,
        *,
        source: str,
        calendar_id: int,
        slots: List[Dict[str, Any]],
    ) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM busy_slots WHERE source = ?", (source,))
            connection.executemany(
                """
                INSERT INTO busy_slots(calendar_id, title, starts_at, ends_at, source)
                VALUES(?,?,?,?,?)
                """,
                [
                    (
                        calendar_id,
                        str(slot["title"]),
                        str(slot["starts_at"]),
                        str(slot["ends_at"]),
                        source,
                    )
                    for slot in slots
                ],
            )
            connection.commit()

    def add_location(
        self,
        label: str,
        address: str,
        priority: int,
        is_default: bool = False,
        lat: Optional[float] = None,
        lng: Optional[float] = None,
    ) -> int:
        return self._execute(
            """
            INSERT INTO location_preferences(label, address, lat, lng, priority, is_default)
            VALUES(?,?,?,?,?,?)
            """,
            (label, address, lat, lng, priority, int(is_default)),
        )

    def list_locations(self) -> List[sqlite3.Row]:
        return self._fetchall(
            "SELECT * FROM location_preferences ORDER BY is_default DESC, priority ASC, id ASC"
        )

    def upsert_contact(
        self,
        email: str,
        now: str,
        display_name: Optional[str] = None,
        organization: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> int:
        existing = self._fetchone("SELECT id FROM contacts WHERE email = ?", (email,))
        if existing:
            self._execute(
                """
                UPDATE contacts
                SET display_name = COALESCE(?, display_name),
                    organization = COALESCE(?, organization),
                    notes = COALESCE(?, notes),
                    updated_at = ?
                WHERE id = ?
                """,
                (display_name, organization, notes, now, existing["id"]),
            )
            return int(existing["id"])
        return self._execute(
            """
            INSERT INTO contacts(email, display_name, organization, notes, created_at, updated_at)
            VALUES(?,?,?,?,?,?)
            """,
            (email, display_name, organization, notes, now, now),
        )

    def create_request(self, fields: Dict[str, Any]) -> int:
        columns = ", ".join(fields.keys())
        placeholders = ", ".join("?" for _ in fields)
        return self._execute(
            "INSERT INTO meeting_requests(%s) VALUES(%s)" % (columns, placeholders),
            list(fields.values()),
        )

    def get_request(self, request_id: int) -> Optional[sqlite3.Row]:
        return self._fetchone("SELECT * FROM meeting_requests WHERE id = ?", (request_id,))

    def get_request_by_thread(self, thread_key: str) -> Optional[sqlite3.Row]:
        return self._fetchone(
            "SELECT * FROM meeting_requests WHERE thread_key = ?",
            (thread_key,),
        )

    def list_requests(self) -> List[sqlite3.Row]:
        return self._fetchall("SELECT * FROM meeting_requests ORDER BY updated_at DESC, id DESC")

    def list_follow_ups_due(self, now_iso: str) -> List[sqlite3.Row]:
        return self._fetchall(
            """
            SELECT *
            FROM meeting_requests
            WHERE status = 'negotiating'
              AND next_follow_up_at IS NOT NULL
              AND unixepoch(next_follow_up_at) <= unixepoch(?)
            ORDER BY unixepoch(next_follow_up_at) ASC, id ASC
            """,
            (now_iso,),
        )

    def list_confirmations_due(self, now_iso: str) -> List[sqlite3.Row]:
        return self._fetchall(
            """
            SELECT *
            FROM meeting_requests
            WHERE status = 'confirmed'
              AND confirmation_due_at IS NOT NULL
              AND confirmation_sent_at IS NULL
              AND unixepoch(confirmation_due_at) <= unixepoch(?)
            ORDER BY unixepoch(confirmation_due_at) ASC, id ASC
            """,
            (now_iso,),
        )

    def update_request(self, request_id: int, fields: Dict[str, Any]) -> None:
        if not fields:
            return
        assignments = ", ".join("%s = ?" % key for key in fields.keys())
        values = list(fields.values())
        values.append(request_id)
        self._execute("UPDATE meeting_requests SET %s WHERE id = ?" % assignments, values)

    def add_participant(
        self,
        request_id: int,
        email: str,
        display_name: Optional[str] = None,
        contact_id: Optional[int] = None,
        role: str = "attendee",
        required: bool = True,
    ) -> int:
        return self._execute(
            """
            INSERT OR IGNORE INTO participants(
                request_id, contact_id, email, display_name, role, required
            ) VALUES(?,?,?,?,?,?)
            """,
            (request_id, contact_id, email, display_name, role, int(required)),
        )

    def list_participants(self, request_id: int) -> List[sqlite3.Row]:
        return self._fetchall(
            "SELECT * FROM participants WHERE request_id = ? ORDER BY id ASC",
            (request_id,),
        )

    def update_participant(self, request_id: int, email: str, fields: Dict[str, Any]) -> None:
        if not fields:
            return
        assignments = ", ".join("%s = ?" % key for key in fields.keys())
        values = list(fields.values())
        values.extend([request_id, email])
        self._execute(
            "UPDATE participants SET %s WHERE request_id = ? AND email = ?" % assignments,
            values,
        )

    def clear_participant_acceptances(self, request_id: int) -> None:
        self._execute(
            """
            UPDATE participants
            SET response_status = 'pending', accepted_option_id = NULL
            WHERE request_id = ?
            """,
            (request_id,),
        )

    def add_email(
        self,
        request_id: int,
        direction: str,
        sender_email: str,
        recipients: List[str],
        cc: List[str],
        subject: str,
        body: str,
        sent_at: str,
        intent: Optional[str] = None,
        extracted_windows: Optional[List[Dict[str, Any]]] = None,
        provider: str = "local",
        external_message_id: Optional[str] = None,
        external_thread_id: Optional[str] = None,
        internet_message_id: Optional[str] = None,
    ) -> int:
        return self._execute(
            """
            INSERT INTO email_messages(
                request_id, direction, sender_email, recipients_json, cc_json,
                provider, external_message_id, external_thread_id, internet_message_id,
                subject, body, intent, extracted_windows_json, sent_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                request_id,
                direction,
                sender_email,
                json.dumps(recipients),
                json.dumps(cc),
                provider,
                external_message_id,
                external_thread_id,
                internet_message_id,
                subject,
                body,
                intent,
                json.dumps(extracted_windows or []),
                sent_at,
            ),
        )

    def list_emails(self, request_id: int) -> List[sqlite3.Row]:
        return self._fetchall(
            "SELECT * FROM email_messages WHERE request_id = ? ORDER BY sent_at ASC, id ASC",
            (request_id,),
        )

    def update_email(self, email_id: int, fields: Dict[str, Any]) -> None:
        if not fields:
            return
        assignments = ", ".join("%s = ?" % key for key in fields.keys())
        values = list(fields.values())
        values.append(email_id)
        self._execute("UPDATE email_messages SET %s WHERE id = ?" % assignments, values)

    def get_email_by_external_message_id(self, external_message_id: str) -> Optional[sqlite3.Row]:
        return self._fetchone(
            "SELECT * FROM email_messages WHERE external_message_id = ?",
            (external_message_id,),
        )

    def get_request_by_external_thread_id(self, external_thread_id: str) -> Optional[sqlite3.Row]:
        return self._fetchone(
            """
            SELECT mr.*
            FROM meeting_requests mr
            JOIN email_messages em ON em.request_id = mr.id
            WHERE em.external_thread_id = ?
            ORDER BY em.id DESC
            LIMIT 1
            """,
            (external_thread_id,),
        )

    def add_ignored_inbox_message(
        self,
        provider: str,
        external_message_id: str,
        created_at: str,
        reason: Optional[str] = None,
    ) -> None:
        if not provider or not external_message_id:
            return
        self._execute(
            """
            INSERT OR IGNORE INTO ignored_inbox_messages(provider, external_message_id, reason, created_at)
            VALUES(?,?,?,?)
            """,
            (provider, external_message_id, reason, created_at),
        )

    def add_ignored_inbox_thread(
        self,
        provider: str,
        external_thread_id: str,
        created_at: str,
        reason: Optional[str] = None,
    ) -> None:
        if not provider or not external_thread_id:
            return
        self._execute(
            """
            INSERT OR IGNORE INTO ignored_inbox_threads(provider, external_thread_id, reason, created_at)
            VALUES(?,?,?,?)
            """,
            (provider, external_thread_id, reason, created_at),
        )

    def is_ignored_inbox_message(self, provider: str, external_message_id: str) -> bool:
        if not provider or not external_message_id:
            return False
        return (
            self._fetchone(
                "SELECT 1 FROM ignored_inbox_messages WHERE provider = ? AND external_message_id = ?",
                (provider, external_message_id),
            )
            is not None
        )

    def is_ignored_inbox_thread(self, provider: str, external_thread_id: str) -> bool:
        if not provider or not external_thread_id:
            return False
        return (
            self._fetchone(
                "SELECT 1 FROM ignored_inbox_threads WHERE provider = ? AND external_thread_id = ?",
                (provider, external_thread_id),
            )
            is not None
        )

    def replace_options(self, request_id: int, options: List[Dict[str, Any]]) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE meeting_requests SET selected_option_id = NULL WHERE id = ?",
                (request_id,),
            )
            connection.execute("DELETE FROM meeting_options WHERE request_id = ?", (request_id,))
            connection.executemany(
                """
                INSERT INTO meeting_options(
                    request_id, starts_at, ends_at, mode, location_label, location_address,
                    meeting_link, travel_minutes, score, rationale, status, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        request_id,
                        option["starts_at"],
                        option["ends_at"],
                        option["mode"],
                        option.get("location_label"),
                        option.get("location_address"),
                        option.get("meeting_link"),
                        option.get("travel_minutes", 0),
                        option.get("score", 0.0),
                        option.get("rationale"),
                        option.get("status", "proposed"),
                        option["created_at"],
                    )
                    for option in options
                ],
            )
            connection.commit()

    def list_options(self, request_id: int) -> List[sqlite3.Row]:
        return self._fetchall(
            "SELECT * FROM meeting_options WHERE request_id = ? ORDER BY starts_at ASC, id ASC",
            (request_id,),
        )

    def set_selected_option(self, request_id: int, option_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE meeting_requests SET selected_option_id = ? WHERE id = ?",
                (option_id, request_id),
            )
            connection.execute(
                """
                UPDATE meeting_options
                SET status = CASE WHEN id = ? THEN 'selected' ELSE 'expired' END
                WHERE request_id = ?
                """,
                (option_id, request_id),
            )
            connection.commit()

    def create_meeting(self, fields: Dict[str, Any]) -> int:
        columns = ", ".join(fields.keys())
        placeholders = ", ".join("?" for _ in fields)
        return self._execute(
            "INSERT OR REPLACE INTO meetings(%s) VALUES(%s)" % (columns, placeholders),
            list(fields.values()),
        )

    def get_meeting_by_request(self, request_id: int) -> Optional[sqlite3.Row]:
        return self._fetchone("SELECT * FROM meetings WHERE request_id = ?", (request_id,))

    def delete_meeting(self, request_id: int) -> None:
        self._execute("DELETE FROM meetings WHERE request_id = ?", (request_id,))

    def delete_request(self, request_id: int) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM meetings WHERE request_id = ?", (request_id,))
            connection.execute("DELETE FROM email_messages WHERE request_id = ?", (request_id,))
            connection.execute("DELETE FROM participants WHERE request_id = ?", (request_id,))
            connection.execute("DELETE FROM meeting_options WHERE request_id = ?", (request_id,))
            connection.execute("DELETE FROM meeting_requests WHERE id = ?", (request_id,))
            connection.commit()

    def list_meetings(self) -> List[sqlite3.Row]:
        return self._fetchall("SELECT * FROM meetings ORDER BY starts_at ASC, id ASC")

    @staticmethod
    def row_to_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        return dict(row)

    @staticmethod
    def rows_to_dicts(rows: List[sqlite3.Row]) -> List[Dict[str, Any]]:
        return [dict(row) for row in rows]
