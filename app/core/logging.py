from __future__ import annotations

import json
import logging
import logging.handlers
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

REQUEST_ID_CTX: ContextVar[str] = ContextVar("request_id", default="-")


class RequestContextFilter(logging.Filter):
    def __init__(self, *, app_env: str):
        super().__init__()
        self.app_env = app_env

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = REQUEST_ID_CTX.get("-")
        record.app_env = self.app_env
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
            "env": getattr(record, "app_env", "-"),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def set_request_id(request_id: str) -> object:
    return REQUEST_ID_CTX.set(request_id or "-")


def reset_request_id(token: object) -> None:
    REQUEST_ID_CTX.reset(token)


def get_request_id() -> str:
    return REQUEST_ID_CTX.get("-")


def setup_logging(
    *,
    log_level: str = "INFO",
    app_env: str = "dev",
    log_dir: str = "./var/log",
    json_logs: bool = True,
    max_bytes: int = 5_000_000,
    backup_count: int = 5,
) -> None:
    root_logger = logging.getLogger()
    if getattr(root_logger, "_assistant_logging_configured", False):
        return

    level = getattr(logging, str(log_level or "INFO").upper(), logging.INFO)
    root_logger.setLevel(level)
    root_logger.handlers.clear()

    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)

    context_filter = RequestContextFilter(app_env=app_env)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s [req=%(request_id)s] - %(message)s")
    )
    console_handler.addFilter(context_filter)
    root_logger.addHandler(console_handler)

    app_file_handler = logging.handlers.RotatingFileHandler(
        directory / "app.log",
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    app_file_handler.setLevel(level)
    app_file_handler.setFormatter(
        JsonFormatter()
        if json_logs
        else logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s [req=%(request_id)s] - %(message)s"
        )
    )
    app_file_handler.addFilter(context_filter)
    root_logger.addHandler(app_file_handler)

    error_file_handler = logging.handlers.RotatingFileHandler(
        directory / "error.log",
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    error_file_handler.setLevel(logging.ERROR)
    error_file_handler.setFormatter(
        JsonFormatter()
        if json_logs
        else logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s [req=%(request_id)s] - %(message)s"
        )
    )
    error_file_handler.addFilter(context_filter)
    root_logger.addHandler(error_file_handler)

    # googleapiclient logs the same API capability errors before raising HttpError.
    logging.getLogger("googleapiclient.http").setLevel(logging.ERROR)
    # httpx logs every OpenAI request/response at INFO, which drowns useful app logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    root_logger._assistant_logging_configured = True  # type: ignore[attr-defined]
