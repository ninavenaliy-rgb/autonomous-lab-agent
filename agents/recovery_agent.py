"""
Recovery Agent — coordinates all recovery subsystems.
Single entry point for the orchestrator to request recovery.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum

from loguru import logger

from core.config import get_config
from recovery.watchdog import Watchdog, WatchdogEvent, CircuitBreaker
from recovery.popup_handler import PopupHandler, get_popup_handler
from recovery.crash_recovery import (
    CrashRecoveryManager,
    get_crash_recovery,
    RecoveryAttempt,
)
import platform as _platform
_IS_WINDOWS = _platform.system() == "Windows"


class RecoveryTrigger(str, Enum):
    ACTION_FAILED = "action_failed"
    HANG_DETECTED = "hang_detected"
    APP_CRASHED = "app_crashed"
    POPUP_BLOCKING = "popup_blocking"
    CIRCUIT_OPEN = "circuit_open"
    MANUAL = "manual"


@dataclass
class RecoveryDecision:
    trigger: RecoveryTrigger
    strategy: str
    succeeded: bool
    details: str = ""
    retry_from_step: int = -1  # -1 = don't retry, >= 0 = retry from step N


class RecoveryAgent:
    """
    Coordinates recovery from all failure modes.
    Called by the orchestrator when something goes wrong.
    """

    def __init__(self) -> None:
        cfg = get_config()
        self._crash = get_crash_recovery()
        self._popups = get_popup_handler()
        if _IS_WINDOWS:
            from vision.screenshot_engine import get_screenshot_engine
            self._screen = get_screenshot_engine()
        else:
            self._screen = None
        self._circuit = CircuitBreaker(
            threshold=cfg.recovery.circuit_breaker_threshold,
            reset_seconds=cfg.recovery.circuit_breaker_reset_seconds,
        )
        self._watchdog: Watchdog | None = None

    def setup_watchdog(
        self,
        hang_callback=None,
        memory_callback=None,
    ) -> Watchdog:
        cfg = get_config()

        def on_hang(event: WatchdogEvent) -> None:
            logger.error(f"Watchdog triggered: {event.details}")
            if self._screen:
                self._screen.capture_failure("watchdog_hang")
            if hang_callback:
                hang_callback(event)

        self._watchdog = Watchdog(
            on_hang=on_hang,
            on_memory_exceeded=memory_callback,
        )
        return self._watchdog

    def heartbeat(self, context: str = "") -> None:
        if self._watchdog:
            self._watchdog.heartbeat(context)

    def start_background_services(self) -> None:
        if self._watchdog:
            self._watchdog.start()
        self._popups.start()
        logger.info("Recovery background services started")

    def stop_background_services(self) -> None:
        if self._watchdog:
            self._watchdog.stop()
        self._popups.stop()
        logger.info("Recovery background services stopped")

    def recover(
        self,
        trigger: RecoveryTrigger,
        context: dict | None = None,
    ) -> RecoveryDecision:
        """Main recovery dispatch."""
        ctx = context or {}
        app = ctx.get("application", "word")
        session_id = ctx.get("session_id", "")
        task_id = ctx.get("task_id", "")
        step_index = ctx.get("step_index", 0)
        file_path = ctx.get("file_path", "")
        error = ctx.get("error", "")

        logger.warning(f"Recovery triggered: {trigger} | app={app} | error={error[:100]}")
        if self._screen:
            self._screen.capture_failure(f"recovery_{trigger.value}")

        if trigger == RecoveryTrigger.POPUP_BLOCKING:
            dismissed = self._popups.check_once()
            if dismissed:
                return RecoveryDecision(
                    trigger=trigger,
                    strategy="dismiss_popup",
                    succeeded=True,
                    details=f"Dismissed {len(dismissed)} popups",
                    retry_from_step=step_index,
                )
            return RecoveryDecision(
                trigger=trigger,
                strategy="dismiss_popup",
                succeeded=False,
                details="No popups found to dismiss",
            )

        elif trigger in (RecoveryTrigger.APP_CRASHED, RecoveryTrigger.HANG_DETECTED):
            result = self._crash.recover_application(app, file_path, session_id, task_id)
            if result.success:
                return RecoveryDecision(
                    trigger=trigger,
                    strategy=f"restart_{app}",
                    succeeded=True,
                    details=f"Restarted in {result.duration_seconds:.1f}s",
                    retry_from_step=max(0, step_index - 1),
                )
            return RecoveryDecision(
                trigger=trigger,
                strategy=f"restart_{app}",
                succeeded=False,
                details="Failed to restart application",
            )

        elif trigger == RecoveryTrigger.ACTION_FAILED:
            # Check for popups first
            dismissed = self._popups.check_once()
            if dismissed:
                return RecoveryDecision(
                    trigger=trigger,
                    strategy="dismiss_popup_then_retry",
                    succeeded=True,
                    retry_from_step=step_index,
                )
            # Check if app is still alive
            if not self._crash.is_application_alive(app):
                result = self._crash.recover_application(app, file_path)
                return RecoveryDecision(
                    trigger=trigger,
                    strategy=f"restart_{app}",
                    succeeded=result.success,
                    retry_from_step=step_index if result.success else -1,
                )
            # App is alive, just retry
            return RecoveryDecision(
                trigger=trigger,
                strategy="retry_action",
                succeeded=True,
                retry_from_step=step_index,
            )

        elif trigger == RecoveryTrigger.CIRCUIT_OPEN:
            # Too many failures — pause and try to clear state
            logger.error("Circuit breaker open — pausing 30s before retry")
            time.sleep(30.0)
            self._circuit.reset()
            # Clear any dialogs
            self._popups.check_once()
            return RecoveryDecision(
                trigger=trigger,
                strategy="circuit_break_reset",
                succeeded=True,
                retry_from_step=step_index,
            )

        return RecoveryDecision(
            trigger=trigger,
            strategy="unknown",
            succeeded=False,
            details=f"No recovery strategy for trigger: {trigger}",
        )

    def record_success(self) -> None:
        self._circuit.record_success()

    def record_failure(self, context: str = "") -> bool:
        """Returns True if circuit breaker just opened."""
        return self._circuit.record_failure(context)

    @property
    def circuit_is_open(self) -> bool:
        return self._circuit.is_open


# Module-level singleton
_agent: RecoveryAgent | None = None


def get_recovery_agent() -> RecoveryAgent:
    global _agent
    if _agent is None:
        _agent = RecoveryAgent()
    return _agent
