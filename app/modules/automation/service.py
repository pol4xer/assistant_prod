from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import suppress
from typing import Any, Dict, Optional

from app.models import isoformat_minute

logger = logging.getLogger(__name__)


class AutomationRuntimeService:
    """Background automation runner that periodically syncs Gmail and processes request flows."""

    def __init__(self, assistant: Any, *, disable_during_tests: bool = True):
        self.assistant = assistant
        self._disable_during_tests = disable_during_tests
        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None

        self._started_at: Optional[str] = None
        self._last_run_at: Optional[str] = None
        self._last_success_at: Optional[str] = None
        self._last_error: Optional[str] = None
        self._last_result: Optional[Dict[str, Any]] = None
        self._last_trigger: Optional[str] = None
        self._last_duration_ms: Optional[int] = None
        self._run_count: int = 0
        self._active_run: bool = False

    @property
    def enabled(self) -> bool:
        if bool(getattr(self.assistant.settings, "app_demo_mode", True)):
            return False
        configured = bool(getattr(self.assistant.settings, "automation_polling_enabled", False))
        if not configured:
            return False
        if self._disable_during_tests and os.getenv("PYTEST_CURRENT_TEST"):
            return False
        return True

    @property
    def interval_seconds(self) -> int:
        value = int(
            getattr(self.assistant.settings, "automation_polling_interval_seconds", 10) or 10
        )
        return max(5, value)

    async def start(self) -> None:
        if not self.enabled:
            logger.info("Background automation is disabled")
            return
        if self._task and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._started_at = isoformat_minute(self.assistant._now())
        self._task = asyncio.create_task(self._loop(), name="scheduling-assistant-automation")
        logger.info(
            "Background automation started interval_seconds=%s",
            self.interval_seconds,
        )

    async def stop(self) -> None:
        if not self._task:
            return
        if self._stop_event:
            self._stop_event.set()
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        self._stop_event = None
        self._active_run = False
        logger.info("Background automation stopped")

    async def run_now(self, trigger: str = "manual") -> Dict[str, Any]:
        return await self._run_cycle(trigger)

    def snapshot(self) -> Dict[str, Any]:
        last_result = dict(self._last_result or {})
        return {
            "enabled": self.enabled,
            "task_running": bool(self._task and not self._task.done()),
            "active_run": self._active_run,
            "interval_seconds": self.interval_seconds,
            "started_at": self._started_at,
            "last_run_at": self._last_run_at,
            "last_success_at": self._last_success_at,
            "last_error": self._last_error,
            "last_trigger": self._last_trigger,
            "last_duration_ms": self._last_duration_ms,
            "run_count": self._run_count,
            "last_result": last_result,
        }

    def _compact_result(self, result: Dict[str, Any]) -> Dict[str, Any]:
        keys = {
            "ok",
            "run_at",
            "imported_inbox_messages",
            "follow_ups_sent",
            "confirmations_sent",
            "repaired_events",
            "skipped",
            "reason",
            "error",
        }
        compact = {key: result.get(key) for key in keys if key in result}
        return compact

    async def _loop(self) -> None:
        await self._run_cycle("startup")
        while self._stop_event and not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                await self._run_cycle("background")

    async def _run_cycle(self, trigger: str) -> Dict[str, Any]:
        started_monotonic = time.monotonic()
        self._active_run = True
        self._last_trigger = trigger
        try:
            result = await asyncio.to_thread(self.assistant.run_automation)
            duration_ms = int((time.monotonic() - started_monotonic) * 1000)
            self._last_run_at = result.get("run_at") or isoformat_minute(self.assistant._now())
            self._last_success_at = self._last_run_at
            self._last_error = None
            self._last_result = self._compact_result(result)
            self._last_duration_ms = duration_ms
            self._run_count += 1
            if (
                result.get("imported_inbox_messages")
                or result.get("follow_ups_sent")
                or result.get("confirmations_sent")
                or result.get("repaired_events")
                or result.get("skipped")
            ):
                logger.info(
                    "Background automation cycle trigger=%s imported=%s follow_ups=%s confirmations=%s repaired=%s skipped=%s",
                    trigger,
                    result.get("imported_inbox_messages", 0),
                    result.get("follow_ups_sent", 0),
                    result.get("confirmations_sent", 0),
                    result.get("repaired_events", 0),
                    bool(result.get("skipped")),
                )
            return result
        except Exception as exc:
            self._last_run_at = isoformat_minute(self.assistant._now())
            self._last_error = str(exc)
            self._last_result = self._compact_result(
                {
                    "ok": False,
                    "run_at": self._last_run_at,
                    "error": str(exc),
                }
            )
            self._last_duration_ms = int((time.monotonic() - started_monotonic) * 1000)
            logger.exception("Background automation cycle failed trigger=%s", trigger)
            return {
                "ok": False,
                "run_at": self._last_run_at,
                "error": str(exc),
            }
        finally:
            self._active_run = False
