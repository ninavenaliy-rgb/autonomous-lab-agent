"""
Watchdog timer system.
Runs in a background thread, monitors execution heartbeat,
and triggers recovery when the agent hangs or crashes.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from loguru import logger

from core.config import get_config


class WatchdogStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    HANG_DETECTED = "hang_detected"
    STOPPED = "stopped"


@dataclass
class WatchdogEvent:
    event_type: str  # "hang" | "heartbeat_missed" | "memory_exceeded"
    timestamp: float = field(default_factory=time.time)
    details: str = ""
    pid: int = 0


class Watchdog:
    """
    Background heartbeat monitor.
    The main execution loop must call heartbeat() regularly.
    If heartbeat is not called within hang_threshold seconds, on_hang is triggered.
    """

    def __init__(
        self,
        hang_threshold: float | None = None,
        on_hang: Callable[[WatchdogEvent], None] | None = None,
        on_memory_exceeded: Callable[[WatchdogEvent], None] | None = None,
        max_memory_mb: float = 2048.0,
    ) -> None:
        cfg = get_config()
        self._threshold = hang_threshold or cfg.recovery.hang_threshold_seconds
        self._interval = cfg.recovery.watchdog_interval_seconds
        self._on_hang = on_hang
        self._on_memory = on_memory_exceeded
        self._max_memory = max_memory_mb

        self._last_heartbeat = time.time()
        self._status = WatchdogStatus.IDLE
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._monitored_pid: int | None = None
        self._lock = threading.Lock()

    @property
    def status(self) -> WatchdogStatus:
        return self._status

    def heartbeat(self, context: str = "") -> None:
        """Called by the main loop to signal it's alive."""
        with self._lock:
            self._last_heartbeat = time.time()
        if context:
            logger.trace(f"Watchdog heartbeat: {context}")

    def monitor_pid(self, pid: int) -> None:
        """Also monitor a specific process for memory usage."""
        self._monitored_pid = pid

    def start(self) -> None:
        if self._status == WatchdogStatus.RUNNING:
            return
        self._stop_event.clear()
        self._status = WatchdogStatus.RUNNING
        self._thread = threading.Thread(
            target=self._loop, name="watchdog", daemon=True
        )
        self._thread.start()
        logger.info(f"Watchdog started (threshold={self._threshold}s, interval={self._interval}s)")

    def stop(self) -> None:
        self._stop_event.set()
        self._status = WatchdogStatus.STOPPED
        if self._thread:
            self._thread.join(timeout=5.0)
        logger.info("Watchdog stopped")

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._check()
            except Exception as exc:
                logger.error(f"Watchdog loop error: {exc}")
            self._stop_event.wait(self._interval)

    def _check(self) -> None:
        now = time.time()

        # Hang detection
        with self._lock:
            elapsed = now - self._last_heartbeat
        if elapsed > self._threshold:
            event = WatchdogEvent(
                event_type="hang",
                details=f"No heartbeat for {elapsed:.1f}s (threshold={self._threshold}s)",
            )
            logger.error(f"WATCHDOG: Hang detected! {event.details}")
            self._status = WatchdogStatus.HANG_DETECTED
            if self._on_hang:
                try:
                    self._on_hang(event)
                except Exception as exc:
                    logger.error(f"Watchdog on_hang callback failed: {exc}")
            # Reset so we don't fire continuously
            with self._lock:
                self._last_heartbeat = time.time()

        # Memory monitoring
        if self._monitored_pid and self._on_memory:
            try:
                import psutil
                proc = psutil.Process(self._monitored_pid)
                mem_mb = proc.memory_info().rss / 1024 / 1024
                if mem_mb > self._max_memory:
                    event = WatchdogEvent(
                        event_type="memory_exceeded",
                        details=f"Process {self._monitored_pid} using {mem_mb:.0f}MB (limit={self._max_memory}MB)",
                        pid=self._monitored_pid,
                    )
                    logger.warning(f"WATCHDOG: {event.details}")
                    self._on_memory(event)
            except Exception:
                pass


class OperationTimer:
    """
    Context manager that enforces a per-operation timeout.
    Raises TimeoutError if operation exceeds limit.
    """

    def __init__(self, timeout_seconds: float, operation_name: str = "operation") -> None:
        self._timeout = timeout_seconds
        self._name = operation_name
        self._thread: threading.Thread | None = None
        self._timed_out = threading.Event()
        self._done = threading.Event()

    def __enter__(self) -> "OperationTimer":
        self._timed_out.clear()
        self._done.clear()
        self._thread = threading.Thread(
            target=self._timer_loop, daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self._done.set()
        if self._timed_out.is_set() and exc_type is None:
            raise TimeoutError(f"Operation '{self._name}' timed out after {self._timeout}s")
        return False

    def _timer_loop(self) -> None:
        if not self._done.wait(self._timeout):
            self._timed_out.set()


class CircuitBreaker:
    """
    Circuit breaker pattern for repeated failures.
    Opens after threshold failures, auto-resets after reset_seconds.
    """

    def __init__(self, threshold: int = 10, reset_seconds: float = 60.0) -> None:
        self._threshold = threshold
        self._reset_seconds = reset_seconds
        self._failure_count = 0
        self._open_since: float | None = None
        self._lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        with self._lock:
            if self._open_since is None:
                return False
            if time.time() - self._open_since >= self._reset_seconds:
                self._failure_count = 0
                self._open_since = None
                logger.info("CircuitBreaker: auto-reset to closed")
                return False
            return True

    def record_success(self) -> None:
        with self._lock:
            self._failure_count = max(0, self._failure_count - 1)

    def record_failure(self, context: str = "") -> bool:
        """Record a failure. Returns True if circuit just opened."""
        with self._lock:
            self._failure_count += 1
            logger.debug(
                f"CircuitBreaker: failure {self._failure_count}/{self._threshold} {context}"
            )
            if self._failure_count >= self._threshold and self._open_since is None:
                self._open_since = time.time()
                logger.error(
                    f"CircuitBreaker OPEN: {self._failure_count} failures. "
                    f"Will reset in {self._reset_seconds}s"
                )
                return True
        return False

    def reset(self) -> None:
        with self._lock:
            self._failure_count = 0
            self._open_since = None
