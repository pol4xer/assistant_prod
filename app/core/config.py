from pydantic_settings import BaseSettings, SettingsConfigDict


class LiveIntegrationsDisabled(ValueError):
    """Raised when a live provider is requested from the offline demo."""


class Settings(BaseSettings):
    app_name: str = "Scheduling Assistant"
    app_env: str = "demo"
    app_demo_mode: bool = True
    app_host: str = "127.0.0.1"
    app_port: int = 8000

    state_db_path: str = "./var/assistant_state.db"
    app_secrets_key_path: str = "./var/assistant_secrets.key"
    log_level: str = "INFO"
    log_dir: str = "./var/log"
    log_json: bool = True
    log_max_bytes: int = 5000000
    log_backup_count: int = 5
    log_recent_default_lines: int = 100

    assistant_name: str = "Marlow"
    assistant_email: str = "assistant@example.com"
    owner_name: str = "Alex Morgan"
    owner_email: str = "owner@example.com"
    default_timezone: str = "UTC"
    default_duration_minutes: int = 45
    meeting_buffer_minutes: int = 15
    min_notice_hours: int = 2
    confirmation_lead_hours: int = 24
    online_provider: str = "google_meet"
    google_calendar_sync_days: int = 21
    google_calendar_sync_ttl_minutes: int = 5
    automation_polling_enabled: bool = False
    automation_polling_interval_seconds: int = 10
    dashboard_auto_refresh_seconds: int = 10
    openai_default_model: str = "gpt-5.4-mini"
    openai_timeout_seconds: float = 20.0

    google_oauth_client_id: str | None = None
    google_oauth_client_secret: str | None = None
    google_oauth_redirect_uri: str | None = "http://localhost:8000/auth/google/callback"
    oauth_client_id: str | None = None
    oauth_client_secret: str | None = None
    oauth_redirect_uri: str | None = None
    google_workspace_scopes: str = (
        "openid,"
        "https://www.googleapis.com/auth/userinfo.email,"
        "https://www.googleapis.com/auth/userinfo.profile,"
        "https://www.googleapis.com/auth/gmail.readonly,"
        "https://www.googleapis.com/auth/gmail.modify,"
        "https://www.googleapis.com/auth/gmail.send,"
        "https://www.googleapis.com/auth/calendar,"
        "https://www.googleapis.com/auth/calendar.events"
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


settings = Settings()
